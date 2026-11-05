"""Unduh berkas bukti (Google Drive) lalu ekstrak teksnya, termasuk OCR."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import requests

from config import BASE_DIR, Settings

log = logging.getLogger(__name__)

DEFAULT_TESSERACT_DIR = BASE_DIR / "bin" / "tesseract"

try:
    import pdfplumber
except ImportError:  # pragma: no cover - dicek saat runtime
    pdfplumber = None

try:
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover
    pytesseract = None
    Image = None

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif"}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

_CONTENT_TYPE_EXT = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "text/plain": ".txt",
}

_DRIVE_ID = re.compile(r"/(?:file|document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})")
_DRIVE_QUERY_ID = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})")


class DownloadError(RuntimeError):
    """Berkas tidak bisa diunduh dari sumbernya."""


@dataclass
class DownloadedFile:
    path: Path
    source_url: str
    filename: str = ""
    mime: str = ""
    size_bytes: int = 0


@dataclass
class Extraction:
    text: str
    method: str = "unknown"
    pages: int = 0
    ok: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class DocumentResult:
    extraction: Extraction
    filename: str = ""
    source_url: str = ""
    in_memory: bool = False


@dataclass(frozen=True)
class DriveRef:
    kind: str
    file_id: str


def parse_drive_ref(url: str) -> DriveRef | None:
    if not url:
        return None

    match = re.search(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})", url)
    if match:
        return DriveRef("folder", match.group(1))

    match = _DRIVE_ID.search(url)
    if match:
        if "/document/d/" in url:
            return DriveRef("document", match.group(1))
        if "/spreadsheets/d/" in url:
            return DriveRef("spreadsheets", match.group(1))
        if "/presentation/d/" in url:
            return DriveRef("presentation", match.group(1))
        return DriveRef("file", match.group(1))

    match = _DRIVE_QUERY_ID.search(url)
    if match and "google.com" in url:
        return DriveRef("file", match.group(1))

    return None


def sanitize_stem(value: str, fallback: str = "berkas") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    stem = re.sub(r"_+", "_", stem).strip("._-")
    return (stem or fallback)[:80]


def tesseract_status(directory: Path | None = None) -> tuple[bool, str]:
    if pytesseract is None:
        return False, "paket pytesseract tidak terpasang"

    target = Path(directory or DEFAULT_TESSERACT_DIR)
    if (target / "tesseract.exe").exists():
        if configure_tesseract(target) is None:
            return False, "tesseract portable tidak bisa dipakai"
        try:
            version = str(pytesseract.get_tesseract_version()).splitlines()[0]
        except Exception as exc:
            return False, f"tesseract portable gagal dijalankan: {exc}"
        return True, f"menggunakan versi portable ({version})"

    found = shutil.which("tesseract")
    if found:
        try:
            version = str(pytesseract.get_tesseract_version()).splitlines()[0]
        except Exception as exc:
            return False, f"tesseract dari PATH gagal dijalankan: {exc}"
        return True, f"menggunakan tesseract dari PATH ({version})"

    return False, "portable belum ada, jalankan: python setup_tesseract.py"


def configure_tesseract(directory: Path | None = None) -> Path | None:
    """Arahkan pytesseract ke biner dan tessdata di dalam proyek."""
    if pytesseract is None:
        return None

    target = Path(directory or DEFAULT_TESSERACT_DIR)
    exe = target / "tesseract.exe"
    if not exe.exists():
        return None

    pytesseract.pytesseract.tesseract_cmd = str(exe)
    os.environ["TESSDATA_PREFIX"] = str(target / "tessdata")
    return exe


def poppler_status() -> tuple[bool, str]:
    try:
        import subprocess

        result = subprocess.run(["pdftoppm", "-v"], capture_output=True, text=True, timeout=10)
        version = (result.stderr or result.stdout).strip().splitlines()
        return True, version[0] if version else "pdftoppm tersedia"
    except Exception:
        pass
    try:
        import pypdfium2  # noqa: F401

        return True, "pdftoppm tidak ada, memakai pypdfium2 sebagai perender"
    except ImportError:
        return False, "butuh poppler (pdftoppm) atau pypdfium2"


class DocumentProcessor:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": _UA})
        self._ocr_langs: set[str] | None = None
        self._poppler_ok: bool | None = None
        self._tesseract_ok: bool | None = None
        configure_tesseract(self.settings.tesseract_dir)

    def process_url(
        self, url: str, stem: str, cookies: dict[str, str] | None = None
    ) -> DocumentResult:
        """Ambil teks dari tautan tanpa meninggalkan berkas di disk."""
        if cookies:
            # Endpoint berkas SIBUNDA bisa membutuhkan sesi login yang sama.
            self._session.cookies.update(cookies)

        ref = parse_drive_ref(url)
        if ref and ref.kind == "folder":
            raise DownloadError(
                "link mengarah ke folder Drive, bukan berkas. Minta lembaga mengunggah berkas langsung"
            )

        if ref and ref.kind == "document":
            text, method = self._google_doc_text(ref.file_id)
            cleaned = self._clean(text)
            return DocumentResult(
                extraction=Extraction(
                    text=cleaned,
                    method=method,
                    pages=1,
                    ok=len(cleaned) >= self.settings.min_text_chars,
                ),
                filename=f"{ref.file_id}.txt",
                source_url=url,
                in_memory=True,
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="sibunda-berkas-"))
        filename = ""
        try:
            downloaded = self._download_to(url, ref, sanitize_stem(stem), temp_dir)
            filename = downloaded.filename
            extraction = self.extract(downloaded.path)
        finally:
            self._cleanup_temp(temp_dir)

        return DocumentResult(
            extraction=extraction,
            filename=filename,
            source_url=url,
        )

    @staticmethod
    def _cleanup_temp(directory: Path) -> None:
        for leftover in directory.rglob("*"):
            if leftover.is_file():
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        shutil.rmtree(directory, ignore_errors=True)

    def _google_doc_text(self, file_id: str) -> tuple[str, str]:
        """Ekspor Google Docs ke memori, tanpa menyentuh hardisk."""
        base = f"https://docs.google.com/document/d/{file_id}/export"
        errors: list[str] = []

        for fmt, method in (("txt", "google-docs-txt"), ("docx", "google-docs-docx")):
            try:
                response = self._session.get(
                    f"{base}?format={fmt}", timeout=self.settings.http_timeout_s
                )
            except requests.RequestException as exc:
                errors.append(f"{fmt}: {exc}")
                continue

            if response.status_code != 200:
                errors.append(f"{fmt}: HTTP {response.status_code}")
                continue

            content = response.content
            if not content:
                errors.append(f"{fmt}: isi kosong")
                continue

            if fmt == "txt":
                if self._looks_like_html_bytes(content):
                    errors.append("txt: Drive mengembalikan halaman HTML (akses ditolak)")
                    continue
                return content.decode("utf-8-sig", errors="replace"), method

            try:
                import docx

                document = docx.Document(io.BytesIO(content))
                return self._docx_document_text(document), method
            except Exception as exc:
                errors.append(f"docx: {exc}")

        raise DownloadError("gagal mengekspor Google Docs (" + "; ".join(errors) + ")")

    def _download_to(
        self, url: str, ref: DriveRef | None, stem: str, dest_dir: Path
    ) -> DownloadedFile:
        target_url, kind = self._resolve_target(url, ref)
        response = self._request(target_url, kind, ref)
        try:
            filename = self._filename_from_headers(response, fallback=stem)
            mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            suffix = Path(filename).suffix.lower()
            if not suffix:
                suffix = _CONTENT_TYPE_EXT.get(mime, ".bin")
                filename = f"{filename}{suffix}"

            dest = dest_dir / f"{stem}__{sanitize_stem(filename, 'berkas')}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_suffix(dest.suffix + ".part")

            expected = int(response.headers.get("Content-Length") or 0)
            limit = self.settings.max_download_mb * 1024 * 1024
            written = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > limit:
                        raise DownloadError(
                            f"berkas melebihi batas {self.settings.max_download_mb} MB"
                        )
                    handle.write(chunk)

            if written == 0:
                raise DownloadError("berkas kosong (0 byte)")
            if expected and written != expected:
                log.warning("ukuran unduhan %s byte, header menyebut %s byte", written, expected)
            if written < 2048 and self._looks_like_html(partial):
                raise DownloadError(
                    "Drive mengembalikan halaman HTML, bukan berkas. Cek izin akses link "
                    "(gunakan 'Anyone with the link')"
                )

            partial.replace(dest)
            log.info("unduh selesai: %s (%.1f KB)", dest.name, written / 1024)
            return DownloadedFile(
                path=dest,
                source_url=url,
                filename=filename,
                mime=mime,
                size_bytes=written,
            )
        finally:
            response.close()

    def _resolve_target(self, url: str, ref: DriveRef | None) -> tuple[str, str]:
        if ref is None:
            if not re.match(r"https?://", url):
                raise DownloadError(f"link berkas tidak valid: {url!r}")
            return url, "direct"

        if ref.kind == "document":
            # Google Docs diekspor sebagai teks biasa agar tidak perlu OCR.
            return (
                f"https://docs.google.com/document/d/{ref.file_id}/export?format=txt",
                "document",
            )
        if ref.kind == "spreadsheets":
            return (
                f"https://docs.google.com/spreadsheets/d/{ref.file_id}/export?format=xlsx",
                "spreadsheets",
            )
        if ref.kind == "presentation":
            return f"https://docs.google.com/presentation/d/{ref.file_id}/export/pdf", "presentation"
        return f"https://drive.google.com/uc?export=download&id={ref.file_id}", "file"

    def _request(self, target_url: str, kind: str, ref: DriveRef | None):
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._session.get(
                    target_url,
                    timeout=self.settings.http_timeout_s,
                    stream=True,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 * attempt)
                continue

            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = DownloadError(f"HTTP {response.status_code}")
                response.close()
                time.sleep(2 * attempt)
                continue

            if response.status_code >= 400:
                response.close()
                raise DownloadError(f"HTTP {response.status_code} saat mengunduh berkas")

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type or kind != "file" or ref is None:
                return response

            # Drive menyisipkan halaman konfirmasi untuk berkas besar; tokennya
            # harus dikirim ulang ke endpoint usercontent.
            body = response.text
            response.close()
            token = re.search(r"confirm=([0-9A-Za-z_-]+)", body)
            if not token:
                raise DownloadError(
                    "Drive meminta izin akses (halaman HTML). Ubah sharing link menjadi "
                    "'Anyone with the link can view'"
                )
            target_url = (
                "https://drive.usercontent.google.com/download"
                f"?id={ref.file_id}&export=download&confirm={token.group(1)}"
            )

        raise DownloadError(f"gagal mengunduh setelah beberapa percobaan: {last_error}")

    @staticmethod
    def _filename_from_headers(response, fallback: str) -> str:
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
        if match:
            from urllib.parse import unquote

            return unquote(match.group(1)).strip().strip('"')
        match = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return fallback

    @staticmethod
    def _looks_like_html(path: Path) -> bool:
        try:
            head = path.read_bytes()[:512].lstrip().lower()
        except OSError:
            return False
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")

    @staticmethod
    def _looks_like_html_bytes(content: bytes) -> bool:
        head = content[:512].lstrip().lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")

    def extract(self, path: Path) -> Extraction:
        suffix = path.suffix.lower()
        kind = self._detect_kind(path, suffix)

        try:
            if kind == "pdf":
                return self._extract_pdf(path)
            if kind == "image":
                return self._extract_image(path)
            if kind == "docx":
                return self._extract_docx(path)
            if kind == "xlsx":
                return self._extract_xlsx(path)
            if kind == "text":
                return self._extract_plain(path)
        except Exception as exc:
            log.warning("ekstraksi %s gagal: %s", path.name, exc)
            return Extraction(
                text="", method=kind, ok=False, warnings=[f"ekstraksi gagal: {exc}"]
            )

        return Extraction(
            text="",
            method=kind,
            ok=False,
            warnings=[f"tipe berkas tidak didukung: {path.suffix or 'tanpa ekstensi'}"],
        )

    @staticmethod
    def _detect_kind(path: Path, suffix: str) -> str:
        if suffix == ".pdf":
            return "pdf"
        if suffix in IMAGE_SUFFIXES:
            return "image"
        if suffix in {".docx"}:
            return "docx"
        if suffix in {".xlsx", ".xlsm"}:
            return "xlsx"
        if suffix in {".txt", ".csv", ".md", ".json"}:
            return "text"
        if suffix in {".zip", ".bin", ".doc", ".xls"}:
            # Drive kadang tidak mengirim Content-Disposition, ekstensi ditebak dari isi.
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()[:50]
            except (zipfile.BadZipFile, OSError):
                names = []
            if any(name.startswith("word/") for name in names):
                return "docx"
            if any(name.startswith("xl/") for name in names):
                return "xlsx"
            with path.open("rb") as handle:
                if handle.read(5) == b"%PDF-":
                    return "pdf"
        return "unknown"

    def _extract_pdf(self, path: Path) -> Extraction:
        if pdfplumber is None:
            return Extraction(
                text="",
                method="pdf-text",
                ok=False,
                warnings=["pdfplumber tidak terpasang"],
            )

        warnings: list[str] = []
        texts: list[str] = []
        total_pages = 0
        with pdfplumber.open(str(path)) as pdf:
            total_pages = len(pdf.pages)
            limit = min(total_pages, self.settings.max_pdf_pages)
            if total_pages > limit:
                warnings.append(
                    f"hanya {limit} dari {total_pages} halaman pertama yang dibaca"
                )
            for page in pdf.pages[:limit]:
                try:
                    texts.append(page.extract_text(x_tolerance=1.5, y_tolerance=2) or "")
                except Exception as exc:
                    texts.append("")
                    warnings.append(f"halaman {len(texts)} gagal dibaca: {exc}")

        plain_chars = sum(len(t.strip()) for t in texts)
        sparse_pages = [
            index
            for index, text in enumerate(texts)
            if len(text.strip()) < self.settings.page_text_min_chars
        ]

        ocr_pages: dict[int, str] = {}
        if sparse_pages:
            ocr_pages = self._ocr_pdf_pages(path, sparse_pages, warnings)
            for index, text in ocr_pages.items():
                if 0 <= index < len(texts):
                    texts[index] = text

        joined = self._clean("\n\n".join(t for t in texts if t.strip()))
        if ocr_pages and plain_chars >= self.settings.min_text_chars:
            method = "pdf-mixed"
        elif ocr_pages:
            method = "pdf-ocr"
        else:
            method = "pdf-text"

        return Extraction(
            text=joined,
            method=method,
            pages=len(texts),
            ok=len(joined) >= self.settings.min_text_chars,
            warnings=warnings,
        )

    def _ocr_pdf_pages(
        self, path: Path, page_indexes: Sequence[int], warnings: list[str]
    ) -> dict[int, str]:
        if not page_indexes:
            return {}
        if pytesseract is None:
            warnings.append("OCR dilewati: pytesseract tidak terpasang")
            return {}
        if not self._tesseract_available():
            warnings.append("OCR dilewati: biner tesseract tidak ditemukan di PATH")
            return {}

        indexes = list(dict.fromkeys(page_indexes))[: self.settings.max_pdf_pages]
        images = self._render_pdf_pages(path, indexes, warnings)
        if not images:
            return {}

        lang = self._resolve_lang(warnings)
        result: dict[int, str] = {}
        for index, image in sorted(images.items()):
            try:
                result[index] = pytesseract.image_to_string(image, lang=lang, config="--psm 3")
            except Exception as exc:
                warnings.append(f"OCR halaman {index + 1} gagal: {exc}")
        return result

    def _render_pdf_pages(
        self, path: Path, page_indexes: Sequence[int], warnings: list[str]
    ) -> dict[int, object]:
        """Render halaman PDF jadi gambar untuk OCR.

        Poppler dipakai lebih dulu bila ada; pypdfium2 (sudah ikut pdfplumber)
        menjadi cadangan agar OCR tetap jalan tanpa instalasi tambahan.
        """
        images: dict[int, object] = {}

        if self._has_poppler():
            try:
                from pdf2image import convert_from_path

                first = min(page_indexes) + 1
                last = max(page_indexes) + 1
                rendered = convert_from_path(
                    str(path),
                    dpi=self.settings.ocr_dpi,
                    first_page=first,
                    last_page=last,
                    fmt="png",
                    thread_count=1,
                )
                wanted = set(page_indexes)
                for offset, image in enumerate(rendered):
                    index = first - 1 + offset
                    if index in wanted:
                        images[index] = image
                if images:
                    return images
            except Exception as exc:
                warnings.append(f"render via poppler gagal: {exc}")

        try:
            import pypdfium2 as pdfium
        except ImportError:
            warnings.append("render PDF gagal: poppler dan pypdfium2 tidak tersedia")
            return {}

        document = None
        try:
            document = pdfium.PdfDocument(str(path))
            for index in page_indexes:
                try:
                    page = document[index]
                except Exception as exc:
                    warnings.append(f"halaman {index + 1} tidak bisa dirender: {exc}")
                    continue
                try:
                    bitmap = page.render(scale=self.settings.ocr_dpi / 72)
                    images[index] = bitmap.to_pil()
                except Exception as exc:
                    warnings.append(f"halaman {index + 1} tidak bisa dirender: {exc}")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        except Exception as exc:
            warnings.append(f"render via pypdfium2 gagal: {exc}")
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass
        return images

    def _extract_image(self, path: Path) -> Extraction:
        warnings: list[str] = []
        if pytesseract is None or Image is None:
            return Extraction(
                text="",
                method="image-ocr",
                ok=False,
                warnings=["pytesseract/Pillow tidak terpasang"],
            )
        lang = self._resolve_lang(warnings)
        try:
            with Image.open(path) as image:
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                text = pytesseract.image_to_string(image, lang=lang, config="--psm 3")
        except Exception as exc:
            return Extraction(
                text="",
                method="image-ocr",
                ok=False,
                warnings=warnings + [f"OCR gambar gagal: {exc}"],
            )

        cleaned = self._clean(text)
        return Extraction(
            text=cleaned,
            method="image-ocr",
            pages=1,
            ok=len(cleaned) >= self.settings.min_text_chars,
            warnings=warnings,
        )

    @staticmethod
    def _docx_document_text(document) -> str:
        parts: list[str] = [p.text for p in document.paragraphs if p.text and p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)

    def _extract_docx(self, path: Path) -> Extraction:
        try:
            import docx
        except ImportError:
            return Extraction(
                text="", method="docx", ok=False, warnings=["python-docx tidak terpasang"]
            )

        document = docx.Document(str(path))
        text = self._clean(self._docx_document_text(document))
        return Extraction(
            text=text,
            method="docx",
            pages=1,
            ok=len(text) >= self.settings.min_text_chars,
        )

    def _extract_xlsx(self, path: Path) -> Extraction:
        try:
            import openpyxl
        except ImportError:
            return Extraction(
                text="", method="xlsx", ok=False, warnings=["openpyxl tidak terpasang"]
            )

        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts: list[str] = []
        try:
            for sheet in workbook.worksheets[:5]:
                parts.append(f"[sheet: {sheet.title}]")
                for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if row_index >= 2000:
                        parts.append("[baris dipotong]")
                        break
                    cells = [str(cell) for cell in row if cell not in (None, "")]
                    if cells:
                        parts.append(" | ".join(cells))
        finally:
            workbook.close()

        text = self._clean("\n".join(parts))
        return Extraction(
            text=text,
            method="xlsx",
            pages=1,
            ok=len(text) >= self.settings.min_text_chars,
        )

    def _extract_plain(self, path: Path) -> Extraction:
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = self._clean(text)
        return Extraction(
            text=cleaned,
            method="text",
            pages=1,
            ok=len(cleaned) >= self.settings.min_text_chars,
        )

    def _tesseract_available(self) -> bool:
        if self._tesseract_ok is None:
            try:
                pytesseract.get_tesseract_version()
                self._tesseract_ok = True
            except Exception:
                self._tesseract_ok = False
        return self._tesseract_ok

    def _has_poppler(self) -> bool:
        if self._poppler_ok is None:
            self._poppler_ok = shutil.which("pdftoppm") is not None
        return self._poppler_ok

    def _resolve_lang(self, warnings: list[str]) -> str:
        requested = [part for part in self.settings.ocr_lang.split("+") if part]
        if self._ocr_langs is None:
            try:
                self._ocr_langs = set(pytesseract.get_languages(config="") if pytesseract else [])
            except Exception:
                self._ocr_langs = set()
        available = self._ocr_langs
        if not available:
            return self.settings.ocr_lang
        usable = [part for part in requested if part in available]
        if len(usable) != len(requested):
            missing = sorted(set(requested) - set(usable))
            warnings.append(f"bahasa OCR tidak tersedia: {', '.join(missing)}")
        return "+".join(usable) if usable else "eng"

    @staticmethod
    def _clean(text: str) -> str:
        text = (text or "").replace("\x00", " ")
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
