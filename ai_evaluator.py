"""Evaluasi dokumen memakai DeepSeek API. AI hanya memberi skor dan catatan.

Status verifikasi tidak pernah ditentukan oleh AI: semua hasil dipaksa "Disetujui"
di lapisan Python sesuai aturan bisnis V4.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from config import Settings
from document_processor import Extraction
from sheets_manager import RubricItem

log = logging.getLogger(__name__)

DISETUJUI = "Disetujui"

_FALLBACK_SCORE = 1
_MIN_DOC_CHARS = 25
_MAX_NOTE_CHARS = 500

SYSTEM_PROMPT = """\
Kamu adalah verifikator dokumen PAUD Bermutu Kota Surabaya yang adil dan bijaksana.

Tugas: nilai satu dokumen bukti terhadap satu indikator rubrik, lalu pilih skor 1-4 yang paling tepat.

Pedoman skor:
- Skor 4: dokumen lengkap, valid, sesuai format, dan memenuhi seluruh kriteria rubrik. Kekurangan yang sangat minor, seperti salah ketik, posisi stempel, atau satu detail kecil, tetap diberi skor 4.
- Skor 3: dokumen inti ada dan sesuai, tetapi ada kekurangan administratif kecil, misalnya hanya satu tanda tangan, kop surat kurang jelas, atau 1-2 kolom data tidak diisi. JANGAN turunkan ke skor 2 bila intinya sudah benar.
- Skor 2: dokumen ada, tetapi setengah kriteria utama tidak terpenuhi atau formatnya berantakan sehingga substansinya sulit dinilai.
- Skor 1: dokumen kosong, tidak relevan, tidak terbaca, atau salah jenis dokumen total.

Prinsip benefit of the doubt:
Bersikaplah sedikit longgar (lenient). Jika dokumen sudah memuat informasi inti yang diminta rubrik, jangan potong skornya hanya karena masalah teknis kecil atau kekurangan data yang tidak signifikan. Fokuslah pada ada atau tidaknya substansi dokumen.

Aturan lain:
1. Nilai HANYA berdasarkan isi dokumen yang diberikan dan definisi skor pada rubrik. Jangan menebak isi dokumen yang tidak ada.
2. Bila teks dokumen berasal dari OCR, abaikan salah baca karakter yang tidak mengubah makna; jangan menilai buruk hanya karena noise OCR.
3. "catatan" maksimal dua kalimat, bahasa Indonesia, sebutkan bukti yang ditemukan atau kekurangan yang tersisa. Jangan mengarang nomor surat, tanggal, atau nama yang tidak ada pada teks.
4. Kamu TIDAK menentukan status verifikasi. Jangan menyertakan status pada jawaban.

Balas HANYA dengan satu objek JSON valid, tanpa markdown dan tanpa teks lain:
{"skor": 1, "catatan": "..."}

Nilai "skor" harus bilangan bulat 1, 2, 3, atau 4."""


@dataclass
class Evaluation:
    skor: int
    status: str
    catatan: str
    model: str = ""
    raw: dict = field(default_factory=dict)
    error: str | None = None
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "skor": self.skor,
            "status": self.status,
            "catatan": self.catatan,
            "model": self.model,
            "error": self.error,
        }


class AIError(RuntimeError):
    """DeepSeek tidak dapat dihubungi atau jawabannya tidak bisa dipakai."""


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.7)
    tail = limit - head
    clipped = (
        text[:head]
        + "\n\n[... teks dokumen dipotong karena melebihi batas ...]\n\n"
        + text[-tail:]
    )
    return clipped, True


def parse_json_object(content: str) -> dict:
    """Ambil objek JSON dari jawaban model, termasuk bila dibungkus markdown."""
    text = (content or "").strip()
    if not text:
        raise ValueError("jawaban model kosong")

    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : position + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    raise ValueError("tidak ada objek JSON valid pada jawaban model")


def normalize_score(value) -> int:
    if isinstance(value, bool):
        raise ValueError("skor tidak valid")
    if isinstance(value, (int, float)):
        score = int(round(float(value)))
        if score not in (1, 2, 3, 4):
            raise ValueError(f"skor di luar rentang 1-4: {value!r}")
        return score
    match = re.search(r"[1-4]", str(value or ""))
    if not match:
        raise ValueError(f"skor tidak valid: {value!r}")
    return int(match.group(0))


class AIEvaluator:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            }
        )

    def check_api(self) -> tuple[bool, str]:
        if not self.settings.deepseek_api_key:
            return False, "DEEPSEEK_API_KEY belum diisi"
        try:
            response = self._session.get(
                f"{self.settings.deepseek_base_url}/models",
                timeout=20,
            )
        except requests.RequestException as exc:
            return False, f"tidak dapat menghubungi DeepSeek: {exc}"
        if response.status_code == 200:
            return True, "DeepSeek API dapat diakses"
        if response.status_code == 401:
            return False, "API key DeepSeek ditolak (401)"
        return False, f"DeepSeek mengembalikan HTTP {response.status_code}"

    def evaluate(
        self,
        indicator_name: str,
        rubric: RubricItem,
        extraction: Extraction,
    ) -> Evaluation:
        text = (extraction.text or "").strip()
        if len(text) < _MIN_DOC_CHARS:
            reason = "Dokumen tidak terbaca atau kosong"
            if extraction.warnings:
                reason += f" ({extraction.warnings[0]})"
            return self._fallback(reason, model=self.settings.deepseek_model)

        clipped, truncated = _clip(text, self.settings.max_doc_chars)
        prompt = self._build_user_prompt(indicator_name, rubric, extraction, clipped, truncated)
        payload = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.settings.deepseek_temperature,
            "max_tokens": self.settings.deepseek_max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

        try:
            content, usage = self._chat(payload)
        except AIError as exc:
            log.error("DeepSeek gagal: %s", exc)
            return self._fallback(f"Verifikasi otomatis gagal: {exc}", error=str(exc))

        try:
            data = parse_json_object(content)
            return self._from_payload(data, model=self.settings.deepseek_model, usage=usage)
        except (ValueError, TypeError) as exc:
            log.warning("jawaban DeepSeek tidak bisa dipakai: %s | isi: %s", exc, content[:400])
            return self._fallback(
                f"Verifikasi otomatis gagal membaca jawaban AI ({exc})", error=str(exc)
            )

    def _chat(self, payload: dict) -> tuple[str, dict]:
        url = f"{self.settings.deepseek_base_url}/chat/completions"
        last_error = "tidak diketahui"
        retry_without_format = True

        for attempt in range(1, self.settings.deepseek_max_retries + 1):
            try:
                response = self._session.post(
                    url, json=payload, timeout=self.settings.deepseek_timeout_s
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(min(2 ** attempt, 15))
                continue

            if response.status_code == 200:
                body = response.json()
                choices = body.get("choices") or []
                if not choices:
                    last_error = "respons tanpa choices"
                    time.sleep(2)
                    continue
                message = choices[0].get("message") or {}
                return (message.get("content") or "").strip(), body.get("usage", {})

            if response.status_code == 401:
                raise AIError("API key DeepSeek ditolak (401)")
            if response.status_code == 400 and retry_without_format:
                # Beberapa endpoint kompatibel menolak response_format.
                retry_without_format = False
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                log.warning("DeepSeek menolak response_format, mencoba tanpa mode JSON")
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = f"HTTP {response.status_code}"
                time.sleep(min(2 ** attempt, 15))
                continue
            raise AIError(f"HTTP {response.status_code}: {response.text[:200]}")

        raise AIError(f"gagal setelah {self.settings.deepseek_max_retries} percobaan ({last_error})")

    def _from_payload(self, data: dict, model: str, usage: dict) -> Evaluation:
        score = normalize_score(data.get("skor", data.get("score")))
        note = re.sub(r"\s+", " ", str(data.get("catatan") or data.get("note") or "")).strip()
        note = note.strip("\"'â€œâ€").strip()
        if not note:
            note = "Penilaian otomatis berdasarkan rubrik."

        return Evaluation(
            skor=score,
            status=DISETUJUI,
            catatan=note[:_MAX_NOTE_CHARS],
            model=model,
            raw=data,
            usage=usage,
        )

    def _fallback(self, note: str, model: str = "", error: str | None = None) -> Evaluation:
        return Evaluation(
            skor=_FALLBACK_SCORE,
            status=DISETUJUI,
            catatan=note[:_MAX_NOTE_CHARS],
            model=model,
            error=error,
        )

    def _build_user_prompt(
        self,
        indicator_name: str,
        rubric: RubricItem,
        extraction: Extraction,
        text: str,
        truncated: bool,
    ) -> str:
        score_lines = []
        for number in (1, 2, 3, 4):
            definition = (rubric.skor.get(number) or "").strip()
            if definition:
                score_lines.append(f"  Skor {number}: {definition}")

        context = [f"Nama indikator pada sistem SIBUNDA: {indicator_name}"]
        if rubric.kode:
            context.append(f"Kode indikator rubrik: {rubric.kode}")
        if rubric.tahap:
            context.append(f"Tahap: {rubric.tahap}")
        if rubric.kelompok:
            context.append(f"Kelompok: {rubric.kelompok}")

        rubric_block = [f"Indikator resmi: {rubric.indikator}"]
        if rubric.bukti:
            rubric_block.append(f"Bukti yang diharapkan: {rubric.bukti}")
        if score_lines:
            rubric_block.append("Definisi skor:")
            rubric_block.extend(score_lines)
        if rubric.dasar_hukum:
            rubric_block.append(f"Dasar hukum: {rubric.dasar_hukum}")

        doc_meta = f"Metode ekstraksi: {extraction.method}"
        if extraction.pages:
            doc_meta += f", {extraction.pages} halaman"
        if truncated:
            doc_meta += ", teks dipotong"
        if extraction.warnings:
            doc_meta += f". Catatan teknis: {'; '.join(extraction.warnings[:3])}"
        if not extraction.ok:
            doc_meta += ". Teks sangat sedikit, curigai dokumen tidak terbaca"

        return (
            "\n".join(context)
            + "\n\nRUBRIK:\n"
            + "\n".join(rubric_block)
            + f"\n\nDOKUMEN ({doc_meta}):\n<<<DOKUMEN\n"
            + text
            + "\nDOKUMEN>>>\n\n"
            + "Tentukan skor 1-4 untuk indikator di atas dengan pedoman di atas. "
            + 'Balas hanya JSON: {"skor": <1-4>, "catatan": "<alasan singkat>"}'
        )

