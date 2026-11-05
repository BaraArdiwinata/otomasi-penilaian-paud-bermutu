"""Orkestrator verifikasi dokumen PAUD Bermutu di SIBUNDA."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

import requests

from ai_evaluator import DISETUJUI, AIEvaluator, Evaluation
from config import ConfigError, Settings, load_settings
from document_processor import (
    DocumentProcessor,
    DownloadError,
    Extraction,
    poppler_status,
    tesseract_status,
)
from sheets_manager import (
    NpsnListError,
    RubricItem,
    RubricMatcher,
    School,
    SheetsManager,
    load_schools_from_csv,
)
from sibunda_bot import LoginError, NoDocumentError, SibundaBot, SibundaError, WebIndicator

log = logging.getLogger("main")

SAVED = "saved"
SKIPPED = "skipped"
FAILED = "error"
DRY = "dry_run"

# Aturan bisnis V2: dua kondisi ini tidak boleh memanggil DeepSeek.
DRIVE_BLOCKED_NOTE = "Akses drive tertutup"
NO_RUBRIC_NOTE = "Indikator tidak memiliki rubrik penilaian di master, disetujui otomatis."


class ProgressStore:
    """Menyimpan indikator yang sudah tersimpan agar run berikutnya bisa lanjut."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("state %s tidak terbaca (%s), mulai baru", path.name, exc)

    @staticmethod
    def _key(npsn: str, indicator_key: str) -> str:
        return f"{npsn}::{indicator_key}"

    def is_done(self, npsn: str, indicator_key: str) -> bool:
        return self._key(npsn, indicator_key) in self.data

    def mark(self, npsn: str, indicator_key: str, record: dict) -> None:
        self.data[self._key(npsn, indicator_key)] = record
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def setup_logging(settings: Settings, verbose: bool) -> Path:
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = settings.log_dir / f"run_{stamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verifikasi dokumen PAUD Bermutu SIBUNDA dibantu DeepSeek."
    )
    parser.add_argument("--npsn", help="batasi ke NPSN tertentu, pisahkan dengan koma")
    parser.add_argument("--limit", type=int, help="maksimal jumlah lembaga yang diproses")
    parser.add_argument("--only", help="regex nama indikator yang diproses")
    parser.add_argument("--max-indicators", type=int, help="maksimal indikator per lembaga")
    parser.add_argument(
        "--headless", dest="headless", action="store_true", default=None, help="jalankan tanpa UI"
    )
    parser.add_argument(
        "--headful", dest="headless", action="store_false", help="tampilkan browser"
    )
    parser.add_argument("--dry-run", action="store_true", help="nilai saja, tidak mengisi form")
    parser.add_argument("--no-ai", action="store_true", help="uji alur tanpa memanggil DeepSeek")
    parser.add_argument("--force", action="store_true", help="abaikan state dan isian yang sudah ada")
    parser.add_argument("--dump-dom", action="store_true", help="simpan HTML halaman verifikasi")
    parser.add_argument("--login-only", action="store_true", help="uji login lalu keluar")
    parser.add_argument("--preflight", action="store_true", help="cek dependensi dan kredensial")
    parser.add_argument("--verbose", "-v", action="store_true", help="log debug")
    return parser


def preflight(settings: Settings) -> bool:
    ok = True
    print("Pemeriksaan kesiapan:")

    def report(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'OK ' if passed else 'GAGAL'}] {label}: {detail}")

    report(
        "kredensial SIBUNDA",
        bool(settings.username and settings.password),
        f"user={settings.username or '(kosong)'}",
    )
    report(
        "DeepSeek key",
        bool(settings.deepseek_api_key),
        "terisi" if settings.deepseek_api_key else "DEEPSEEK_API_KEY kosong",
    )

    t_ok, t_detail = tesseract_status(settings.tesseract_dir)
    report("tesseract", t_ok, t_detail)
    p_ok, p_detail = poppler_status()
    report("poppler", p_ok, p_detail)

    try:
        schools = load_schools_from_csv(settings.npsn_csv)
        report("Daftar NPSN", True, f"{len(schools)} NPSN dari {settings.npsn_csv.name}")
    except NpsnListError as exc:
        report("Daftar NPSN", False, str(exc))

    try:
        rubric = SheetsManager(settings).fetch_rubric()
        report("Rubrik Google Sheets", True, f"{len(rubric)} indikator")
    except Exception as exc:  # noqa: BLE001 - laporan preflight
        report("Rubrik Google Sheets", False, str(exc))

    if settings.deepseek_api_key:
        evaluator = AIEvaluator(settings)
        api_ok, api_detail = evaluator.check_api()
        report("DeepSeek API", api_ok, api_detail)

    print("\nHasil:", "siap dijalankan" if ok else "ada yang perlu diperbaiki")
    return ok


class Runner:
    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.settings = settings
        self.args = args
        self.only = re.compile(args.only, re.I) if args.only else None
        self.progress = ProgressStore(settings.state_dir / "progress.json")
        self.results: list[dict] = []
        self.counts = {SAVED: 0, SKIPPED: 0, FAILED: 0, DRY: 0}

    def _wanted(self, indicator: WebIndicator) -> bool:
        return not self.only or bool(self.only.search(indicator.name))

    def _record(
        self,
        school: School,
        indicator: WebIndicator | None,
        action: str,
        *,
        rubric: RubricItem | None = None,
        match_score: float = 0.0,
        evaluation: Evaluation | None = None,
        extraction: Extraction | None = None,
        document: str = "",
        error: str = "",
    ) -> None:
        self.counts[action] = self.counts.get(action, 0) + 1
        self.results.append(
            {
                "npsn": school.npsn,
                "nama": school.nama,
                "indikator_web": indicator.name if indicator else "",
                "rubrik_kode": rubric.kode if rubric else "",
                "rubrik_indikator": rubric.indikator if rubric else "",
                "match_score": round(match_score, 3),
                "skor": evaluation.skor if evaluation else "",
                "status": evaluation.status if evaluation else "",
                "catatan": evaluation.catatan if evaluation else "",
                "metode_ekstraksi": extraction.method if extraction else "",
                "dokumen": document,
                "aksi": action,
                "error": error,
            }
        )

    async def run(self) -> int:
        try:
            schools = load_schools_from_csv(self.settings.npsn_csv)
        except NpsnListError as exc:
            log.error("%s", exc)
            return 1
        rubric = SheetsManager(self.settings).fetch_rubric()
        matcher = RubricMatcher(rubric, threshold=self.settings.match_threshold)

        if self.args.npsn:
            wanted = {n.strip() for n in self.args.npsn.split(",") if n.strip()}
            schools = [s for s in schools if s.npsn in wanted]
            missing = wanted - {s.npsn for s in schools}
            for npsn in sorted(missing):
                log.warning("NPSN %s tidak ada di %s", npsn, self.settings.npsn_csv.name)

        if self.args.limit:
            schools = schools[: self.args.limit]
        if not schools:
            log.error("tidak ada lembaga untuk diproses")
            return 1

        processor = DocumentProcessor(self.settings)
        evaluator = AIEvaluator(self.settings)
        log.info("memproses %d lembaga, %d indikator rubrik", len(schools), len(rubric))

        async with SibundaBot(self.settings) as bot:
            await bot.login()
            if self.args.login_only:
                log.info("login diuji, keluar tanpa memproses lembaga")
                return 0

            for position, school in enumerate(schools, start=1):
                log.info("[%d/%d] %s", position, len(schools), school.label)
                try:
                    await self._run_school(
                        bot, processor, evaluator, matcher, school
                    )
                except LoginError as exc:
                    log.error("sesi/login bermasalah: %s", exc)
                    await bot.capture(f"login-{school.npsn}")
                    raise
                except SibundaError as exc:
                    log.error("NPSN %s gagal dibuka: %s", school.npsn, exc)
                    self._record(school, None, FAILED, error=str(exc))
                    await bot.capture(f"gagal-{school.npsn}")
                except Exception as exc:  # noqa: BLE001 - satu lembaga tidak boleh menghentikan semua
                    log.exception("NPSN %s gagal tanpa terduga: %s", school.npsn, exc)
                    self._record(school, None, FAILED, error=str(exc))
                    await bot.capture(f"exception-{school.npsn}")

        return self._finish()

    async def _run_school(
        self,
        bot: SibundaBot,
        processor: DocumentProcessor,
        evaluator: AIEvaluator,
        matcher: RubricMatcher,
        school: School,
    ) -> None:
        indicators = await bot.open_verification(school.npsn)
        if self.args.dump_dom:
            await bot.dump_dom(f"verifikasi-{school.npsn}")

        if self.args.max_indicators:
            indicators = indicators[: self.args.max_indicators]

        pending: list[tuple[str, WebIndicator]] = [
            (ind.key, ind) for ind in indicators if self._wanted(ind)
        ]
        processed = 0
        guard = len(pending) * 3 + 10

        while pending and guard > 0:
            guard -= 1
            key, indicator = pending.pop(0)

            if not self.args.force and self.progress.is_done(school.npsn, key):
                log.info("lewati (sudah tersimpan): %s", indicator.name[:70])
                self._record(school, indicator, SKIPPED, error="sudah tersimpan di state")
                continue

            if (
                self.settings.skip_filled
                and not self.args.force
                and indicator.already_filled
            ):
                log.info("lewati (form sudah terisi): %s", indicator.name[:70])
                self._record(school, indicator, SKIPPED, error="form sudah terisi")
                continue

            processed += 1
            try:
                await self._process_one(bot, processor, evaluator, matcher, school, indicator)
            except Exception as exc:  # noqa: BLE001 - lanjut ke indikator berikutnya
                log.error("indikator gagal (%s): %s", indicator.name[:60], exc)
                self._record(school, indicator, FAILED, error=str(exc))
                await bot.capture(f"error-{school.npsn}-{processed}")

            fresh = await self._safe_discover(bot, school)
            by_key = {item.key: item for item in fresh}
            pending = [(k, by_key[k]) for k, _ in pending if k in by_key]

        if pending:
            log.warning(
                "NPSN %s berhenti dengan %d indikator belum diproses", school.npsn, len(pending)
            )
            self._record(
                school, None, FAILED, error=f"{len(pending)} indikator belum diproses"
            )
        log.info("NPSN %s selesai, %d indikator diproses", school.npsn, processed)

    async def _safe_discover(self, bot: SibundaBot, school: School) -> list[WebIndicator]:
        if await bot.redirect_check():
            log.warning("sesi habis saat memproses %s, login ulang", school.npsn)
            await bot.login(force=True)
            return await bot.open_verification(school.npsn)

        # Halaman bisa sedang reload setelah tombol simpan ditekan.
        for delay_ms in (600, 1500):
            await bot.pause(delay_ms)
            items = await bot.discover_indicators()
            if items:
                return items

        log.warning("daftar indikator kosong setelah menyimpan, memuat ulang halaman")
        return await bot.open_verification(school.npsn)

    async def _process_one(
        self,
        bot: SibundaBot,
        processor: DocumentProcessor,
        evaluator: AIEvaluator,
        matcher: RubricMatcher,
        school: School,
        indicator: WebIndicator,
    ) -> None:
        started = time.monotonic()
        extraction = Extraction(text="", method="none", ok=False, warnings=[])
        match = matcher.find(indicator.name)

        if match is None:
            log.warning(
                "rubrik tidak ditemukan untuk %r: skor 4 Disetujui tanpa memanggil AI",
                indicator.name[:80],
            )
            await self._finish_indicator(
                bot,
                school,
                indicator,
                Evaluation(skor=4, status=DISETUJUI, catatan=NO_RUBRIC_NOTE, model="rule"),
                extraction=extraction,
                match=None,
                rule="tanpa_rubrik",
            )
            return

        if not match.confident:
            runner = (
                f", kandidat lain {match.runner_up[0].indikator[:50]!r} "
                f"({match.runner_up[1]:.2f})"
                if match.runner_up
                else ""
            )
            log.warning(
                "pasangan rubrik lemah (%.2f): %r -> %r%s",
                match.score,
                indicator.name[:80],
                match.item.indikator[:80],
                runner,
            )

        try:
            drive_url = await bot.get_drive_url(indicator)
            stem = f"{school.npsn}_{match.item.kode or 'indikator'}_{indicator.index}"
            result = processor.process_url(drive_url, stem, cookies=await bot.cookies())
        except (NoDocumentError, DownloadError, requests.RequestException) as exc:
            log.warning("unduhan gagal (%s): skor 1 Disetujui tanpa memanggil AI", exc)
            extraction.warnings.append(str(exc))
            await self._finish_indicator(
                bot,
                school,
                indicator,
                Evaluation(skor=1, status=DISETUJUI, catatan=DRIVE_BLOCKED_NOTE, model="rule"),
                extraction=extraction,
                match=match,
                rule="akses_drive",
            )
            return

        extraction = result.extraction
        if extraction.warnings:
            log.info("catatan ekstraksi: %s", "; ".join(extraction.warnings[:2]))
        log.info(
            "teks %d karakter (%s) dari %s",
            len(extraction.text),
            extraction.method,
            result.filename or drive_url,
        )

        if self.args.no_ai:
            evaluation = Evaluation(
                skor=1,
                status=DISETUJUI,
                catatan="Mode --no-ai: evaluasi dilewati.",
                model="none",
            )
        else:
            evaluation = evaluator.evaluate(indicator.name, match.item, extraction)

        log.info(
            "skor=%s status=%s (%.1fs) | %s",
            evaluation.skor,
            evaluation.status,
            time.monotonic() - started,
            evaluation.catatan[:100],
        )

        await self._finish_indicator(
            bot,
            school,
            indicator,
            evaluation,
            extraction=extraction,
            match=match,
            document=result.filename,
        )

    async def _finish_indicator(
        self,
        bot: SibundaBot,
        school: School,
        indicator: WebIndicator,
        evaluation: Evaluation,
        *,
        extraction: Extraction,
        match,
        document: str = "",
        rule: str = "",
    ) -> None:
        save_result = await bot.fill_and_save(
            indicator, evaluation, dry_run=self.args.dry_run
        )
        if not save_result.ok:
            raise SibundaError(f"penyimpanan ditolak: {save_result.message}")

        action = DRY if self.args.dry_run else SAVED
        self._record(
            school,
            indicator,
            action,
            rubric=match.item if match else None,
            match_score=match.score if match else 0.0,
            evaluation=evaluation,
            extraction=extraction,
            document=document,
            error="",
        )
        if self.args.dry_run:
            return

        self.progress.mark(
            school.npsn,
            indicator.key,
            {
                "indikator": indicator.name,
                "kode": match.item.kode if match else "",
                "aturan": rule,
                "skor": evaluation.skor,
                "status": evaluation.status,
                "waktu": datetime.now().isoformat(timespec="seconds"),
            },
        )

    def _finish(self) -> int:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.settings.report_dir / f"hasil_{stamp}.csv"
        json_path = self.settings.report_dir / f"hasil_{stamp}.json"

        if self.results:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self.results[0].keys()))
                writer.writeheader()
                writer.writerows(self.results)
            json_path.write_text(
                json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        log.info(
            "selesai: %d tersimpan, %d dilewati, %d gagal, %d pratinjau",
            self.counts.get(SAVED, 0),
            self.counts.get(SKIPPED, 0),
            self.counts.get(FAILED, 0),
            self.counts.get(DRY, 0),
        )
        if self.results:
            log.info("laporan: %s", csv_path)
        return 0 if self.counts.get(FAILED, 0) == 0 else 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.headless is not None:
        settings = replace(settings, headless=args.headless)
    log_file = setup_logging(settings, args.verbose)
    log.info("log: %s", log_file)

    if args.preflight:
        return 0 if preflight(settings) else 1

    try:
        settings.require_sibunda()
        if not args.no_ai and not args.login_only:
            settings.require_deepseek()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    runner = Runner(settings, args)
    try:
        return await runner.run()
    finally:
        runner.progress.save()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        log.warning("dihentikan pengguna")
        return 130
    except ConfigError as exc:
        log.error("konfigurasi: %s", exc)
        return 1
    except LoginError as exc:
        log.error("login: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
