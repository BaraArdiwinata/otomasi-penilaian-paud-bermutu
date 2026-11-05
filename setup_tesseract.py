"""Siapkan Tesseract portable di bin/tesseract tanpa mengubah PATH Windows."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = BASE_DIR / "bin" / "tesseract"

IND_URL = "https://github.com/tesseract-ocr/tessdata/raw/main/ind.traineddata"
INSTALLER_URL = (
    "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.5.0.20241111.exe"
)

# Hanya runtime yang dibutuhkan; alat training dan dokumentasi tidak disalin.
RUNTIME_DATA = ("eng.traineddata", "osd.traineddata", "pdf.ttf")

COMMON_SOURCES = (
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Tesseract-OCR",
)


def log(message: str) -> None:
    print(f"[setup] {message}")


def find_installed() -> Path | None:
    env = os.environ.get("TESSERACT_SOURCE")
    if env and (Path(env) / "tesseract.exe").exists():
        return Path(env)
    for candidate in COMMON_SOURCES:
        if candidate and (candidate / "tesseract.exe").exists():
            return candidate
    which = shutil.which("tesseract")
    return Path(which).parent if which else None


def copy_runtime(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    tessdata = target / "tessdata"
    tessdata.mkdir(exist_ok=True)

    shutil.copy2(source / "tesseract.exe", target / "tesseract.exe")
    for dll in source.glob("*.dll"):
        shutil.copy2(dll, target / dll.name)

    for name in RUNTIME_DATA:
        origin = source / "tessdata" / name
        if origin.exists():
            shutil.copy2(origin, tessdata / name)
    log(f"runtime disalin dari {source}")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def ensure_ind(target: Path, force: bool = False) -> None:
    trained = target / "tessdata" / "ind.traineddata"
    if trained.exists() and trained.stat().st_size > 100_000 and not force:
        log(f"ind.traineddata sudah ada ({trained.stat().st_size / 1_048_576:.1f} MB)")
        return
    log("mengunduh ind.traineddata dari tessdata resmi")
    download(IND_URL, trained)
    log(f"ind.traineddata siap ({trained.stat().st_size / 1_048_576:.1f} MB)")


def install_via_installer(target: Path) -> bool:
    log("mencari sumber lokal tidak berhasil, mencoba installer UB Mannheim")
    target.mkdir(parents=True, exist_ok=True)
    installer = Path(tempfile.mkdtemp(prefix="tesseract-setup-")) / "tesseract-setup.exe"
    try:
        download(INSTALLER_URL, installer)
        # Installer UB Mannheim memakai Inno Setup: /DIR harus argumen terakhir.
        result = subprocess.run(
            [
                str(installer),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/SP-",
                f"/DIR={target}",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception as exc:
        log(f"installer gagal dijalankan: {exc}")
        return False

    if (target / "tesseract.exe").exists():
        log("installer selesai")
        return True

    log(f"installer tidak menghasilkan tesseract.exe (exit {result.returncode})")
    log("cara manual: pasang Tesseract, lalu jalankan skrip ini lagi tanpa --download")
    return False


def verify(target: Path) -> bool:
    exe = target / "tesseract.exe"
    if not exe.exists():
        log(f"tesseract.exe tidak ditemukan di {target}")
        return False

    env = dict(os.environ, TESSDATA_PREFIX=str(target / "tessdata"))
    try:
        version = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=60, env=env
        )
        langs = subprocess.run(
            [str(exe), "--list-langs"], capture_output=True, text=True, timeout=60, env=env
        )
    except Exception as exc:
        log(f"gagal menjalankan tesseract: {exc}")
        return False

    first = (version.stdout or version.stderr).strip().splitlines()
    log(first[0] if first else "versi tidak terbaca")
    log(langs.stdout.strip().replace("\n", " | "))
    return "ind" in langs.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Siapkan Tesseract portable untuk proyek ini")
    parser.add_argument("--dir", default=str(DEFAULT_TARGET), help="folder tujuan")
    parser.add_argument("--source", help="folder Tesseract yang sudah terpasang")
    parser.add_argument("--download", action="store_true", help="unduh installer bila tidak ada sumber")
    parser.add_argument("--force", action="store_true", help="salin ulang dan unduh ulang ind")
    args = parser.parse_args(argv)

    target = Path(args.dir).expanduser().resolve()

    if (target / "tesseract.exe").exists() and not args.force:
        ensure_ind(target, force=args.force)
        return 0 if verify(target) else 1

    source = Path(args.source).expanduser().resolve() if args.source else find_installed()
    if source and (source / "tesseract.exe").exists():
        copy_runtime(source, target)
    elif args.download:
        if not install_via_installer(target):
            return 1
    else:
        log("Tesseract tidak ditemukan di folder umum.")
        log("Pasang Tesseract lalu ulangi, atau jalankan: python setup_tesseract.py --download")
        return 1

    ensure_ind(target, force=args.force)
    if not verify(target):
        log("verifikasi gagal: pastikan tessdata berisi ind.traineddata")
        return 1

    log(f"selesai. Tesseract portable aktif di {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
