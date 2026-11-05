"""Konfigurasi terpusat, dibaca dari .env di direktori proyek."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Nilai default mengikuti dokumen kerja Surabaya 2026.
DEFAULT_SHEET_ID = "1XEzU4gPyJcZPobEFwKeSehmwczQ2jRQS"
DEFAULT_RUBRIK_GID = "1218752796"


class ConfigError(RuntimeError):
    """Nilai .env tidak valid atau wajib belum diisi."""


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} harus bilangan bulat, dapat: {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} harus angka, dapat: {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "ya", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "tidak", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} harus true/false, dapat: {raw!r}")


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    # SIBUNDA
    base_url: str
    verify_path: str
    tahun: int
    username: str
    password: str
    headless: bool
    slow_mo_ms: int
    action_timeout_ms: int
    nav_timeout_ms: int
    persist_session: bool

    # Daftar NPSN (CSV lokal) dan rubrik (Google Sheets)
    npsn_csv: Path
    sheet_id: str
    gid_rubrik: str
    google_creds_file: Path | None

    # DeepSeek
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_temperature: float
    deepseek_max_tokens: int
    deepseek_timeout_s: int
    deepseek_max_retries: int
    max_doc_chars: int

    # Dokumen dan OCR
    log_dir: Path
    state_dir: Path
    report_dir: Path
    debug_dir: Path
    tesseract_dir: Path
    ocr_lang: str
    ocr_dpi: int
    min_text_chars: int
    page_text_min_chars: int
    max_pdf_pages: int
    max_download_mb: int
    http_timeout_s: int

    # Perilaku verifikasi
    match_threshold: float
    skip_filled: bool
    save_wait_ms: int
    rerender_wait_ms: int

    # Web UI dan penyimpanan
    app_password: str
    database_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=_get("SIBUNDA_BASE_URL", "https://sibunda.surabaya.go.id").rstrip("/"),
            verify_path=_get(
                "SIBUNDA_VERIFY_PATH", "/paud_mutu/paudmutu/halaman_verifikasi"
            ),
            tahun=_get_int("SIBUNDA_TAHUN", 2026),
            username=_get("SIBUNDA_USERNAME"),
            password=_get("SIBUNDA_PASSWORD"),
            headless=_get_bool("SIBUNDA_HEADLESS", False),
            slow_mo_ms=_get_int("SIBUNDA_SLOW_MO_MS", 500),
            action_timeout_ms=_get_int("SIBUNDA_ACTION_TIMEOUT_MS", 20_000),
            nav_timeout_ms=_get_int("SIBUNDA_NAV_TIMEOUT_MS", 60_000),
            persist_session=_get_bool("SIBUNDA_PERSIST_SESSION", True),
            npsn_csv=_get_path("NPSN_CSV_PATH", BASE_DIR / "npsn_list.csv"),
            sheet_id=_get("GSHEET_ID", DEFAULT_SHEET_ID),
            gid_rubrik=_get("GSHEET_GID_RUBRIK", DEFAULT_RUBRIK_GID),
            google_creds_file=(
                _get_path("GOOGLE_CREDENTIALS_FILE", Path())
                if _get("GOOGLE_CREDENTIALS_FILE")
                else None
            ),
            deepseek_api_key=_get("DEEPSEEK_API_KEY"),
            deepseek_base_url=_get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            deepseek_model=_get("DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_temperature=_get_float("DEEPSEEK_TEMPERATURE", 0.0),
            deepseek_max_tokens=_get_int("DEEPSEEK_MAX_TOKENS", 700),
            deepseek_timeout_s=_get_int("DEEPSEEK_TIMEOUT_S", 90),
            deepseek_max_retries=_get_int("DEEPSEEK_MAX_RETRIES", 3),
            max_doc_chars=_get_int("MAX_DOC_CHARS", 60_000),
            log_dir=_get_path("LOG_DIR", BASE_DIR / "logs"),
            state_dir=_get_path("STATE_DIR", BASE_DIR / "state"),
            report_dir=_get_path("REPORT_DIR", BASE_DIR / "reports"),
            debug_dir=_get_path("DEBUG_DIR", BASE_DIR / "debug"),
            tesseract_dir=_get_path("TESSERACT_DIR", BASE_DIR / "bin" / "tesseract"),
            ocr_lang=_get("OCR_LANG", "ind"),
            ocr_dpi=_get_int("OCR_DPI", 300),
            min_text_chars=_get_int("MIN_TEXT_CHARS", 80),
            page_text_min_chars=_get_int("PAGE_TEXT_MIN_CHARS", 40),
            max_pdf_pages=_get_int("MAX_PDF_PAGES", 40),
            max_download_mb=_get_int("MAX_DOWNLOAD_MB", 60),
            http_timeout_s=_get_int("HTTP_TIMEOUT_S", 120),
            match_threshold=_get_float("MATCH_THRESHOLD", 0.52),
            skip_filled=_get_bool("SKIP_FILLED", True),
            save_wait_ms=_get_int("SAVE_WAIT_MS", 2_500),
            rerender_wait_ms=_get_int("RERENDER_WAIT_MS", 1_000),
            app_password=_get("APP_PASSWORD"),
            database_path=_get_path("DATABASE_PATH", BASE_DIR / "database.db"),
        )

    def url_login(self) -> str:
        return f"{self.base_url}/login"

    @property
    def tesseract_exe(self) -> Path:
        return self.tesseract_dir / "tesseract.exe"

    @property
    def tessdata_dir(self) -> Path:
        return self.tesseract_dir / "tessdata"

    def url_verifikasi(self, npsn: str) -> str:
        query = urlencode({"tahun": self.tahun, "npsn": str(npsn)})
        return f"{self.base_url}{self.verify_path}?{query}"

    def ensure_dirs(self) -> None:
        for path in (
            self.log_dir,
            self.state_dir,
            self.report_dir,
            self.debug_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def require_sibunda(self) -> None:
        if not self.username or not self.password:
            raise ConfigError(
                "SIBUNDA_USERNAME dan SIBUNDA_PASSWORD wajib diisi di .env"
            )

    def require_deepseek(self) -> None:
        if not self.deepseek_api_key:
            raise ConfigError("DEEPSEEK_API_KEY wajib diisi di .env")


def load_settings() -> Settings:
    return Settings.from_env()


settings = load_settings()
