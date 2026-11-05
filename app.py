"""Web UI Streamlit untuk verifikasi PAUD Bermutu.

Menyediakan login, unggah daftar NPSN, tombol mulai/hentikan, log langsung,
dan unduhan hasil CSV. Proses verifikasi tetap dijalankan oleh `main.py`
sebagai subprocess terpisah agar UI tidak ikut macet.
"""

from __future__ import annotations

import hmac
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

from config import BASE_DIR, load_settings

settings = load_settings()

LOG_FILE = settings.log_dir / "ui_run.log"
PID_FILE = settings.state_dir / "run.pid"
CSV_FILE = settings.npsn_csv

st.set_page_config(
    page_title="Verifikasi PAUD Bermutu",
    page_icon="📋",
    layout="wide",
)


def tail(path: Path, limit: int = 400) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(belum ada log)"
    return "\n".join(lines[-limit:])


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except OSError:
            return False
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
    except OSError:
        return False

    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return "main.py" in cmdline.read_text(errors="replace")
        except OSError:
            return True
    return True


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def start_run(options: list[str]) -> tuple[bool, str]:
    if running_pid():
        return False, "Proses verifikasi masih berjalan. Hentikan dulu."
    if not CSV_FILE.exists():
        return False, "npsn_list.csv belum ada. Unggah daftar NPSN terlebih dahulu."
    if not settings.username or not settings.password:
        return False, "Kredensial SIBUNDA belum diisi di .env."

    settings.ensure_dirs()
    LOG_FILE.write_text(
        f"=== mulai {datetime.now():%Y-%m-%d %H:%M:%S} ===\n", encoding="utf-8"
    )
    command = [sys.executable, str(BASE_DIR / "main.py"), "--headless", *options]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with LOG_FILE.open("ab", buffering=0) as handle:
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return True, f"Verifikasi dimulai (PID {process.pid}). Pantau tab Log."


def stop_run() -> str:
    pid = running_pid()
    if not pid:
        PID_FILE.unlink(missing_ok=True)
        return "Tidak ada proses yang berjalan."
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError as exc:
        return f"Gagal menghentikan PID {pid}: {exc}"
    PID_FILE.unlink(missing_ok=True)
    return f"Proses {pid} dihentikan."


def save_csv(raw: bytes) -> tuple[bool, str]:
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False, "Berkas kosong."
    header = [part.strip().strip('"').lower() for part in lines[0].split(",")]
    if "npsn" not in header:
        return False, "Kolom 'NPSN' tidak ditemukan pada baris pertama."

    valid = sum(1 for line in lines[1:] if re.match(r"^\s*\"?\d{6,12}", line))
    if not valid:
        return False, "Tidak ada NPSN valid di dalam berkas."
    CSV_FILE.write_bytes(raw)
    return True, f"{valid} NPSN tersimpan ke {CSV_FILE.name}."


def db_summary() -> dict | None:
    if not settings.database_path.exists():
        return None
    try:
        connection = sqlite3.connect(str(settings.database_path), timeout=5)
        connection.row_factory = sqlite3.Row
        total = connection.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
        by_status = connection.execute(
            "SELECT status, COUNT(*) AS jumlah FROM progress GROUP BY status"
        ).fetchall()
        sekolah = connection.execute("SELECT COUNT(DISTINCT npsn) FROM progress").fetchone()[0]
        recent = connection.execute(
            "SELECT npsn, kode, skor, status, waktu FROM progress "
            "ORDER BY waktu DESC LIMIT 8"
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return None
    return {
        "total": total,
        "sekolah": sekolah,
        "by_status": [(row["status"], row["jumlah"]) for row in by_status],
        "recent": [dict(row) for row in recent],
    }


def latest_report() -> Path | None:
    files = list(settings.report_dir.glob("hasil_*.csv"))
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def login_screen() -> None:
    st.title("Verifikasi PAUD Bermutu")
    st.caption("Masuk untuk mengelola proses verifikasi SIBUNDA.")

    if not settings.app_password:
        st.error("APP_PASSWORD belum diisi di .env. Isi dulu sebelum memakai web UI.")
        return

    with st.form("login"):
        password = st.text_input("Kata sandi", type="password")
        submitted = st.form_submit_button("Masuk", type="primary")

    if submitted:
        if hmac.compare_digest(password, settings.app_password):
            st.session_state["auth"] = True
            st.rerun()
        st.error("Kata sandi salah.")


if not st.session_state.get("auth"):
    login_screen()
    st.stop()


# ---------------------------------------------------------------- UI utama

st.title("Verifikasi PAUD Bermutu")
st.caption("SIBUNDA · Playwright · DeepSeek")

pid = running_pid()
status_text = f"Berjalan (PID {pid})" if pid else "Tidak ada proses"

with st.sidebar:
    st.subheader("Status")
    st.write(status_text)
    st.caption(f"Database: {settings.database_path.name}")

    summary = db_summary()
    if summary:
        st.metric("Indikator tersimpan", summary["total"])
        st.caption(f"{summary['sekolah']} lembaga")
        for status, jumlah in summary["by_status"]:
            st.caption(f"{status or '-'}: {jumlah}")
    else:
        st.caption("Database belum berisi data.")

    st.divider()
    if st.button("Keluar", use_container_width=True):
        st.session_state["auth"] = False
        st.rerun()

tab_run, tab_log, tab_result = st.tabs(["Jalankan", "Log", "Hasil"])

with tab_run:
    left, right = st.columns([1, 1])

    with left:
        st.subheader("1. Daftar NPSN")
        uploaded = st.file_uploader("Unggah npsn_list.csv", type=["csv"])
        if uploaded is not None:
            ok, message = save_csv(uploaded.getvalue())
            if ok:
                st.success(message)
            else:
                st.error(message)
        if CSV_FILE.exists():
            jumlah = max(0, len(CSV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()) - 1)
            st.caption(f"Berkas aktif: {CSV_FILE.name} ({jumlah} baris)")
        else:
            st.warning("npsn_list.csv belum tersedia.")

    with right:
        st.subheader("2. Opsi")
        dry_run = st.checkbox("Dry-run (tanpa menyimpan ke SIBUNDA)", value=True)
        force = st.checkbox("Paksa ulang indikator yang sudah ada", value=False)
        max_indicators = st.number_input(
            "Maksimal indikator per lembaga (0 = semua)", min_value=0, max_value=200, value=0
        )
        only = st.text_input("Filter indikator (regex, opsional)")

    st.divider()
    options: list[str] = []
    if dry_run:
        options.append("--dry-run")
    if force:
        options.append("--force")
    if max_indicators:
        options += ["--max-indicators", str(int(max_indicators))]
    if only.strip():
        options += ["--only", only.strip()]

    col_start, col_stop = st.columns([1, 1])
    with col_start:
        if st.button("Mulai Verifikasi", type="primary", disabled=bool(pid), use_container_width=True):
            ok, message = start_run(options)
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
    with col_stop:
        if st.button("Hentikan", disabled=not pid, use_container_width=True):
            st.warning(stop_run())
            st.rerun()

    st.caption("Perintah yang dijalankan: `python main.py --headless " + " ".join(options) + "`")

with tab_log:
    st.subheader("Log langsung")
    st.caption(f"Membaca {LOG_FILE.name} setiap 2 detik.")

    @st.fragment(run_every=2)
    def live_panel() -> None:
        current = running_pid()
        st.write(f"Status: {'berjalan (PID ' + str(current) + ')' if current else 'idle'}")
        st.code(tail(LOG_FILE), language="text")

    live_panel()

with tab_result:
    st.subheader("Hasil verifikasi")
    report = latest_report()
    if report is None:
        st.info("Belum ada laporan. Jalankan verifikasi terlebih dahulu.")
    else:
        st.write(f"Laporan terbaru: `{report.name}`")
        st.download_button(
            "Unduh hasil CSV",
            data=report.read_bytes(),
            file_name=report.name,
            mime="text/csv",
            type="primary",
        )

    summary = db_summary()
    if summary and summary["recent"]:
        st.subheader("Aktivitas terakhir")
        st.dataframe(summary["recent"], use_container_width=True, hide_index=True)
