"""Rubrik dari Google Sheets, daftar NPSN dari CSV lokal, dan pencocokan indikator."""

from __future__ import annotations

import csv
import difflib
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import requests

from config import Settings

log = logging.getLogger(__name__)

_CSV_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


class SheetsError(RuntimeError):
    """Sheet tidak bisa dibaca atau formatnya tidak sesuai."""


class NpsnListError(RuntimeError):
    """File daftar NPSN tidak ada atau formatnya tidak sesuai."""


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_text(value: str) -> str:
    text = (value or "").lower()
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_STOPWORDS = {
    "memiliki", "mempunyai", "dan", "atau", "yang", "di", "ke", "dari", "untuk",
    "pada", "dengan", "serta", "bagi", "adalah", "ini", "itu", "telah", "sudah",
    "data", "dokumen", "berkas", "secara",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _norm_text(text).split() if len(t) > 2 and t not in _STOPWORDS}


@dataclass(frozen=True)
class School:
    npsn: str
    nama: str = ""
    jenjang: str = ""
    status: str = ""
    alamat: str = ""
    kelurahan: str = ""
    kecamatan: str = ""

    @property
    def label(self) -> str:
        return f"{self.npsn} - {self.nama}" if self.nama else self.npsn


def load_schools_from_csv(path: Path) -> list[School]:
    """Baca daftar NPSN dari CSV lokal (header wajib: NPSN)."""
    if not path.exists():
        raise NpsnListError(f"file daftar NPSN tidak ditemukan: {path}")

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise NpsnListError(f"{path.name} kosong, butuh header 'NPSN'")

    # Header dicocokkan tanpa peduli huruf besar/kecil ("NPSN", "npsn", " Npsn ").
    headers = {_norm_header(name): name for name in reader.fieldnames if name}
    npsn_key = headers.get("npsn")
    if npsn_key is None:
        raise NpsnListError(
            f"kolom 'NPSN' tidak ditemukan pada {path.name}. "
            f"Header terbaca: {', '.join(reader.fieldnames)}"
        )
    nama_key = headers.get("nama lembaga") or headers.get("nama")

    schools: list[School] = []
    seen: set[str] = set()
    skipped = 0
    for row in reader:
        raw = (row.get(npsn_key) or "").strip()
        if not raw:
            continue
        npsn = re.sub(r"\.0$", "", raw)
        if not re.fullmatch(r"\d{6,12}", npsn):
            skipped += 1
            continue
        if npsn in seen:
            continue
        seen.add(npsn)
        nama = (row.get(nama_key) or "").strip() if nama_key else ""
        schools.append(School(npsn=npsn, nama=nama))

    if skipped:
        log.warning("%d baris NPSN tidak valid dan dilewati", skipped)
    if not schools:
        raise NpsnListError(f"tidak ada NPSN valid pada {path.name}")
    return schools


@dataclass(frozen=True)
class RubricItem:
    kode: str
    tahap: str
    kelompok: str
    indikator: str
    bukti: str
    skor: dict[int, str] = field(default_factory=dict)
    dasar_hukum: str = ""
    empiris: str = ""

    @property
    def key(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", _norm_text(self.indikator))[:60].strip("-")
        return f"{self.tahap or '-'}:{self.kode or '-'}:{slug}"


@dataclass(frozen=True)
class RubricMatch:
    item: RubricItem
    score: float
    runner_up: tuple[RubricItem, float] | None = None

    @property
    def confident(self) -> bool:
        return self.score >= 0.75 or (
            self.runner_up is not None and self.score - self.runner_up[1] >= 0.12
        )


class SheetsManager:
    RUBRIK_ALIASES = {
        "tahap": "tahap",
        "kode": "kode",
        "kode indikator": "kode",
        "kelompok": "kelompok",
        "indikator resmi": "indikator",
        "indikator": "indikator",
        "nama indikator": "indikator",
        "bukti yang diharapkan": "bukti",
        "bukti": "bukti",
        "skor 1": "skor_1",
        "skor 2": "skor_2",
        "skor 3": "skor_3",
        "skor 4": "skor_4",
        "dasar hukum deskripsi": "dasar_hukum",
        "dasar hukum": "dasar_hukum",
        "indikator empiris": "empiris",
    }

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self._session = session or requests.Session()
        self._client = None
        self._spreadsheet = None

    def _gspread_client(self):
        if self._client is not None:
            return self._client

        import gspread

        raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
        if raw_json:
            self._client = gspread.service_account_from_dict(json.loads(raw_json))
            return self._client

        creds_file = self.settings.google_creds_file
        if creds_file and creds_file.exists():
            self._client = gspread.service_account(filename=str(creds_file))
            return self._client

        return None

    def _records_via_gspread(self, gid: str) -> list[dict[str, str]]:
        client = self._gspread_client()
        if client is None:
            raise SheetsError("kredensial gspread tidak tersedia")

        if self._spreadsheet is None:
            self._spreadsheet = client.open_by_key(self.settings.sheet_id)

        worksheet = None
        for ws in self._spreadsheet.worksheets():
            if str(ws.id) == str(gid):
                worksheet = ws
                break
        if worksheet is None:
            raise SheetsError(f"worksheet dengan gid {gid} tidak ditemukan")

        values = worksheet.get_all_values()
        return self._rows_to_records(values)

    def _records_via_csv(self, gid: str) -> list[dict[str, str]]:
        url = _CSV_EXPORT.format(sheet_id=self.settings.sheet_id, gid=gid)
        response = self._session.get(
            url,
            timeout=self.settings.http_timeout_s,
            headers={"User-Agent": "Mozilla/5.0 (compatible; paud-verifier/1.0)"},
        )
        if response.status_code != 200:
            raise SheetsError(f"gagal mengunduh CSV sheet (HTTP {response.status_code})")
        if "text/csv" not in response.headers.get("Content-Type", ""):
            raise SheetsError(
                "sheet tidak dapat diakses publik. Unduh service account JSON lalu isi "
                "GOOGLE_CREDENTIALS_FILE, dan pastikan sheet dibagikan ke email service account"
            )

        text = response.content.decode("utf-8-sig", errors="replace")
        values = list(csv.reader(io.StringIO(text)))
        return self._rows_to_records(values)

    def _rows_to_records(self, values: Sequence[Sequence[str]]) -> list[dict[str, str]]:
        if not values:
            return []
        header = [str(v).strip() for v in values[0]]
        records: list[dict[str, str]] = []
        for row in values[1:]:
            record: dict[str, str] = {}
            for index, name in enumerate(header):
                if not name or name in record:
                    continue
                cell = row[index] if index < len(row) else ""
                record[name] = str(cell).strip()
            if any(record.values()):
                records.append(record)
        return records

    def _records(self, gid: str) -> list[dict[str, str]]:
        try:
            client = self._gspread_client()
        except Exception as exc:  # kredensial rusak tidak boleh mematikan fallback CSV
            log.warning("gspread tidak dapat dipakai (%s), memakai export CSV", exc)
            client = None

        if client is not None:
            try:
                return self._records_via_gspread(gid)
            except Exception as exc:
                log.warning("baca via gspread gagal (%s), memakai export CSV", exc)
        return self._records_via_csv(gid)

    @staticmethod
    def _canonical(record: dict[str, str], aliases: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in record.items():
            canonical = aliases.get(_norm_header(key))
            if canonical and canonical not in result and value:
                result[canonical] = value
        return result

    def fetch_rubric(self) -> list[RubricItem]:
        raw = self._records(self.settings.gid_rubrik)
        items: list[RubricItem] = []
        seen: set[str] = set()
        for record in raw:
            row = self._canonical(record, self.RUBRIK_ALIASES)
            indikator = (row.get("indikator") or "").strip()
            kode = (row.get("kode") or "").strip()
            if not indikator and not kode:
                continue
            skor = {
                n: (row.get(f"skor_{n}") or "").strip()
                for n in (1, 2, 3, 4)
            }
            item = RubricItem(
                kode=kode,
                tahap=(row.get("tahap") or "").strip(),
                kelompok=(row.get("kelompok") or "").strip(),
                indikator=indikator,
                bukti=(row.get("bukti") or "").strip(),
                skor=skor,
                dasar_hukum=(row.get("dasar_hukum") or "").strip(),
                empiris=(row.get("empiris") or "").strip(),
            )
            if item.key in seen:
                continue
            seen.add(item.key)
            items.append(item)
        if not items:
            raise SheetsError("rubrik kosong: periksa gid dan nama kolom sheet rubrik")
        return items


class RubricMatcher:
    """Cocokkan judul indikator di SIBUNDA dengan baris rubrik.

    Nama di web sering dipotong atau diubah susunannya, jadi kemiripan dihitung
    dari rasio urutan karakter, irisan kata, dan relasi mengandung.
    """

    def __init__(self, items: Iterable[RubricItem], threshold: float = 0.52) -> None:
        self.items = list(items)
        self.threshold = threshold
        self._prepared = [
            (_norm_text(item.indikator), _tokens(item.indikator), item) for item in self.items
        ]

    @staticmethod
    def _code_tokens(text: str) -> set[str]:
        return {m.lower() for m in re.findall(r"\b[A-Za-z]{0,3}\.?\d{1,2}(?:\.\d{1,2})?\b", text or "")}

    def score_pair(self, web_name: str, item: RubricItem) -> float:
        a = _norm_text(web_name)
        b = _norm_text(item.indikator)
        if not a or not b:
            return 0.0

        seq = difflib.SequenceMatcher(None, a, b).ratio()
        ta, tb = _tokens(web_name), _tokens(item.indikator)
        jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
        containment = 1.0 if (a in b or b in a) else 0.0
        score = 0.50 * seq + 0.35 * jaccard + 0.15 * containment

        # Judul di web sering hanya potongan judul rubrik ("Profil Satuan PAUD");
        # bila semua kata web ada di rubrik, itu bukti kuat. Bonus naik seiring
        # jumlah kata yang cocok agar judul 2 kata tidak menang mudah.
        if len(ta) >= 2 and ta <= tb:
            score += 0.12 + 0.05 * min(len(ta), 4)

        # Kode indikator (mis. 1.1) hanya jadi penguat, bukan penentu, karena
        # beberapa kode dipakai ulang pada tahap berbeda.
        if item.kode and item.kode.lower() in self._code_tokens(web_name) and seq >= 0.20:
            score += 0.20
        return min(score, 1.0)

    def find(self, web_name: str) -> RubricMatch | None:
        scored = sorted(
            ((self.score_pair(web_name, item), item) for _, _, item in self._prepared),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not scored or scored[0][0] < self.threshold:
            return None
        best_score, best_item = scored[0]
        runner = (scored[1][1], scored[1][0]) if len(scored) > 1 else None
        return RubricMatch(item=best_item, score=best_score, runner_up=runner)
