# Verifikasi Dokumen PAUD Bermutu (SIBUNDA)

Repositori: <https://github.com/BaraArdiwinata/otomasi-penilaian-paud-bermutu>

Otomasi verifikasi berkas PAUD Bermutu Kota Surabaya. Sistem membaca daftar NPSN dari
`npsn_list.csv` dan rubrik dari Google Sheets, membuka halaman verifikasi SIBUNDA dengan
Playwright, mengunduh setiap berkas bukti dari Google Drive, mengekstrak teksnya (PDF teks, OCR,
DOCX, atau ekspor Google Docs), meminta penilaian dari DeepSeek API, lalu mengisi skor, status,
dan catatan verifikator.

Semua keputusan tetap harus diperiksa manusia. Sistem ini menyiapkan isian, bukan menggantikan
verifikator.

## Alur eksekusi

```
npsn_list.csv â”€â”€â–º login SIBUNDA â”€â”€â–º buka halaman verifikasi per NPSN
                                        â”‚
                                        â–¼
                          deteksi kartu indikator ("Buka File")
                                        â”‚
                     â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                     â–¼                                      â–¼
        tidak ada rubrik (V2)                    ada rubrik â†’ unduh berkas
        skor 4 Disetujui                         â”‚
                                                 â”œâ”€ Drive PDF/gambar/DOCX â†’ tempfile â†’ hapus
                                                 â”œâ”€ Google Docs â†’ ekspor txt ke memori
                                                 â–¼
                                        ekstraksi teks / OCR (ind)
                                                 â”‚
                                       â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                                       â–¼                    â–¼
                              unduhan gagal (V2)      teks siap â†’ DeepSeek
                              skor 1 Disetujui        skor 1-4 + catatan
                                       â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                                 â–¼
                           isi skor â†’ tunggu render â†’ status Disetujui
                           â†’ isi catatan â†’ klik Simpan Hasil Verifikasi
                                                 â”‚
                                                 â–¼
                             database.db + reports/hasil_*.csv
```

Rincian tiap langkah:

1. `sheets_manager.load_schools_from_csv` membaca `npsn_list.csv`, lalu rubrik dibaca dari sheet.
2. `sibunda_bot.py` login, membuka `halaman_verifikasi?tahun=...&npsn=...`, dan mendeteksi
   setiap kartu indikator dari tombol "Buka File".
3. Judul indikator di web dicocokkan ke baris rubrik (kemiripan string + irisan kata + kode).
4. `document_processor.py` mengunduh berkas Drive, mengekstrak teks PDF/DOCX, dan memakai OCR
   bila halaman tidak punya lapisan teks.
5. `ai_evaluator.py` mengirim teks + definisi skor 1-4 ke DeepSeek dan memaksa jawaban JSON.
6. Bot mengisi skor, status, catatan, lalu menekan "Simpan Hasil Verifikasi".

## Prasyarat

- Python 3.10 atau lebih baru (diuji pada 3.13 Windows).
- Tesseract OCR. Di Windows tidak perlu mengatur `PATH` lagi: `setup_tesseract.py` menyalin
  runtime dan `ind.traineddata` ke `bin/tesseract/`, lalu kode menunjuk ke biner itu.
  - Linux: `sudo apt install tesseract-ocr tesseract-ocr-ind`, lalu arahkan `TESSERACT_DIR`
    ke folder instalasinya bila bukan lokasi standar.
- Poppler bersifat opsional. Bila tidak ada, perender `pypdfium2` (ikut terpasang lewat
  `pdfplumber`) dipakai untuk mengubah halaman PDF menjadi gambar.
  - Windows (opsional): unduh [poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases),
    ekstrak, lalu tambahkan folder `bin` ke `PATH`.
  - Linux: `sudo apt install poppler-utils`
- Akun SIBUNDA yang punya akses verifikasi.
- Git (untuk kloning repositori).

## Instalasi

```powershell
git clone https://github.com/BaraArdiwinata/otomasi-penilaian-paud-bermutu.git
cd otomasi-penilaian-paud-bermutu

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python setup_tesseract.py
```

Linux/macOS: `source .venv/bin/activate`.

Setelah dependensi terpasang, siapkan dua berkas lokal yang tidak ikut di-commit:

```powershell
Copy-Item .env.example .env
Copy-Item npsn_list.example.csv npsn_list.csv
# lalu isi SIBUNDA_USERNAME, SIBUNDA_PASSWORD, dan DEEPSEEK_API_KEY di .env
```

`setup_tesseract.py` mencari Tesseract di folder umum Windows, menyalin runtime ke
`bin/tesseract/`, mengunduh `ind.traineddata` dari repositori tessdata resmi, lalu menguji
`--list-langs`. Bila tidak ada sumber sama sekali, jalankan `python setup_tesseract.py --download`
untuk memakai installer UB Mannheim secara senyap ke folder proyek.

## Konfigurasi

Semua kredensial dibaca dari `.env`. Gunakan `.env.example` sebagai template; `.env` sudah masuk
`.gitignore` sehingga tidak ikut ter-commit.

| Variabel | Wajib | Keterangan |
| --- | --- | --- |
| `SIBUNDA_USERNAME` | ya | akun verifikator |
| `SIBUNDA_PASSWORD` | ya | kata sandi akun |
| `DEEPSEEK_API_KEY` | ya | key dari platform.deepseek.com |
| `SIBUNDA_HEADLESS` | tidak | `false` (default) agar browser terlihat saat bekerja |
| `SIBUNDA_SLOW_MO_MS` | tidak | jeda per aksi Playwright, default `500` ms; isi `0` untuk ngebut |
| `NPSN_CSV_PATH` | tidak | lokasi daftar NPSN, default `npsn_list.csv` |
| `TESSERACT_DIR` | tidak | folder Tesseract portable, default `bin/tesseract` |
| `OCR_LANG` | tidak | bahasa OCR, default `ind` |
| `GSHEET_ID` | tidak | default sheet rubrik |
| `GSHEET_GID_RUBRIK` | tidak | gid sheet rubrik |
| `GOOGLE_CREDENTIALS_FILE` | tidak | isi bila sheet rubrik tidak dibaca publik |
| `MATCH_THRESHOLD` | tidak | ambang minimal kemiripan indikator (default 0.52) |
| `MAX_DOC_CHARS` | tidak | batas teks yang dikirim ke AI (default 60000) |
| `APP_PASSWORD` | ya (Web UI) | kata sandi halaman Streamlit |
| `DATABASE_PATH` | tidak | lokasi SQLite, default `database.db` |

Sheet dapat dibaca tanpa kredensial selama dibagikan "Anyone with the link". Bila tidak, buat
service account Google, aktifkan Google Sheets API, bagikan sheet ke email service account
tersebut, lalu arahkan `GOOGLE_CREDENTIALS_FILE` ke file JSON-nya. `gspread` dipakai lebih dulu,
dan export CSV hanya menjadi cadangan.

## Daftar NPSN

Daftar NPSN tidak lagi dibaca dari Google Sheets. Siapkan `npsn_list.csv` di folder proyek:

```csv
NPSN
69903097
69948650
```

Header `NPSN` dicari tanpa peduli huruf besar/kecil (`NPSN`, `npsn`, atau ` Npsn `). Kolom lain
diabaikan; bila ada kolom `Nama Lembaga` nilainya hanya dipakai untuk label log. Baris dengan
NPSN kosong dilewati tanpa peringatan, sedangkan NPSN duplikat atau nilai yang bukan 6-12 digit
dilewati dengan peringatan. Lokasi file dapat diubah lewat `NPSN_CSV_PATH`. Salin
`npsn_list.example.csv` menjadi `npsn_list.csv` lalu ganti isinya; `npsn_list.csv` tidak ikut
di-commit agar daftar kerja Anda tetap lokal.

## Aturan bisnis otomatis

Dua kondisi ini diputuskan di `main.py` sebelum DeepSeek dipanggil, jadi tidak memakan kuota AI
dan tidak bergantung pada kualitas teks:

| Kondisi | Skor | Status | Catatan |
| --- | --- | --- | --- |
| Berkas gagal diunduh (403, akses ditolak, link rusak, tautan kosong) | 1 | Disetujui | `Akses drive tertutup` |
| Indikator di SIBUNDA tidak punya pasangan di rubrik | 4 | Disetujui | `Indikator tidak memiliki rubrik penilaian di master, disetujui otomatis.` |

Keduanya tetap dicatat di `reports/hasil_*.csv` dengan kolom `rubrik_kode` kosong (kasus rubrik
hilang) dan ditandai di `database.db` lewat kolom `aturan` (`akses_drive` atau
`tanpa_rubrik`). Sesuai kebijakan V4, seluruh hasil akhir selalu `Disetujui`.

## Menjalankan

```powershell
# cek dependensi, kredensial, dan akses sheet/AI
python main.py --preflight

# uji login saja
python main.py --login-only

# pratinjau satu lembaga tanpa menyimpan apa pun
python main.py --npsn 69903097 --dry-run

# jalankan tanpa jendela browser (untuk proses massal)
python main.py --headless

# proses semua lembaga dengan browser terlihat
python main.py

# hanya indikator tertentu
python main.py --npsn 69903097 --only "akreditasi|kurikulum"

# coba 2 lembaga, 3 indikator per lembaga
python main.py --limit 2 --max-indicators 3
```

Opsi lain: `--force` (abaikan state dan isian lama), `--no-ai` (uji alur tanpa DeepSeek),
`--dump-dom` (simpan HTML ke `debug/`), `--verbose`.

## Mode tampilan

Default sekarang headful dengan `slow_mo` 500 ms, jadi setiap klik dan ketik bisa diamati
langsung. Untuk proses massal, matikan jeda dengan `SIBUNDA_SLOW_MO_MS=0` di `.env` atau
jalankan `python main.py --headless`.

## Web UI (Streamlit)

Untuk pemakaian harian, tersedia antarmuka web:

```powershell
streamlit run app.py
```

Buka `http://localhost:8501`, masuk dengan `APP_PASSWORD` dari `.env`, lalu:

1. Unggah `npsn_list.csv` (header `NPSN`).
2. Pilih opsi: dry-run, paksa ulang, batas indikator, filter regex.
3. Klik **Mulai Verifikasi**; proses berjalan di subprocess `main.py` sehingga UI tetap responsif.
4. Pantau tab **Log** (diperbarui tiap 2 detik) dan hentikan dengan **Hentikan** bila perlu.
5. Unduh laporan CSV terbaru di tab **Hasil**.

Proses hanya boleh satu pada satu waktu (`state/run.pid`), dan tombol Mulai dinonaktifkan selama
proses berjalan.

## Deployment VPS

Panduan ini dipakai untuk VPS `43.156.71.169` (Ubuntu, Caddy, Docker). **Nginx tidak dipakai**
karena port 80/443 sudah dipegang Caddy yang melayani situs lain; menambahkan Nginx akan bentrok.

```bash
# 1. Dependensi sistem
sudo apt update
sudo apt install -y python3-venv python3-pip tesseract-ocr tesseract-ocr-ind \
  libgl1 libasound2t64 libnss3 libxkbcommon0 fonts-liberation

# 2. Kode dan venv
cd /home/ubuntu
git clone https://github.com/BaraArdiwinata/otomasi-penilaian-paud-bermutu.git otomasi-verifikasi-paud
cd otomasi-verifikasi-paud
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# 3. Konfigurasi
cp .env.example .env
cp npsn_list.example.csv npsn_list.csv
chmod 600 .env
# isi kredensial, APP_PASSWORD, dan set SIBUNDA_HEADLESS=true

# 4. Cek
python main.py --preflight
```

**Caddy** (subpath + Basic Auth), tambahkan ke blok `bara-ardiwinata.com` pada
`/etc/caddy/Caddyfile` sebelum `handle` situs utama:

```caddyfile
	handle /otomasi-verifikasi-paud* {
		basic_auth {
			verifikator <hash-bcrypt>
		}
		reverse_proxy 127.0.0.1:8501
	}

	handle {
		reverse_proxy 127.0.0.1:3000
	}
```

Hash dibuat dengan `caddy hash-password --plaintext 'password-anda'`, lalu
`sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.

**Systemd** `/etc/systemd/system/otomasi-verifikasi.service`:

```ini
[Unit]
Description=Otomasi Verifikasi PAUD Bermutu
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/otomasi-verifikasi-paud
Environment=HOME=/home/ubuntu
EnvironmentFile=/home/ubuntu/otomasi-verifikasi-paud/.env
ExecStart=/home/ubuntu/otomasi-verifikasi-paud/.venv/bin/streamlit run app.py \
  --server.port 8501 --server.address 127.0.0.1 \
  --server.baseUrlPath otomasi-verifikasi-paud --server.headless true --server.enableCORS false
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Aktifkan dengan `sudo systemctl daemon-reload && sudo systemctl enable --now otomasi-verifikasi`.
Streamlit hanya mendengarkan `127.0.0.1:8501`, jadi tidak perlu membuka port baru di firewall.

## Hasil dan pemulihan

- `logs/run_*.log` mencatat setiap langkah. Web UI menulis `logs/ui_run.log`.
- `database.db` (SQLite) mencatat indikator yang sudah tersimpan. Jalankan ulang perintah yang
  sama untuk melanjutkan; tambahkan `--force` bila ingin menilai ulang. State lama
  `state/progress.json` dimigrasikan otomatis saat pertama kali dibuka.
- `reports/hasil_*.csv` dan `.json` berisi ringkasan skor, status, catatan, dan galat. Kolom
  `dokumen` memuat nama berkas asli, bukan path.
- `debug/` berisi screenshot saat terjadi kegagalan.
- Tidak ada folder unduhan permanen. Berkas PDF/gambar/DOCX ditulis ke direktori sementara
  (`tempfile`), teksnya diekstrak, lalu berkasnya dihapus di blok `finally`. Google Docs
  diekspor ke memori dan tidak pernah menyentuh disk.

## Kebijakan status dan format jawaban AI

Aturan bisnis V4: sistem **tidak pernah** menghasilkan `Ditolak` atau `Pending`. Semua indikator
disimpan sebagai `Disetujui`, berapa pun skor dan catatannya. AI hanya menentukan skor.

DeepSeek diminta membalas JSON murni tanpa status:

```json
{"skor": 3, "catatan": "Dokumen KOSP lengkap dan bertanda tangan."}
```

Aturan yang dipaksakan di kode:

- `skor` selalu bilangan bulat 1-4; nilai di luar rentang membuat jawaban dianggap gagal dan
  dipakai skor fallback 1.
- `status` tidak dibaca dari jawaban AI. `_from_payload` menetapkan `Disetujui` secara tetap, dan
  `sibunda_bot.fill_and_save` mengabaikan status lain yang masuk.
- Dokumen kosong atau teks kurang dari 25 karakter tidak dikirim ke API, langsung diberi skor 1
  dengan catatan alasannya, status tetap `Disetujui`.
- Bila API gagal atau jawabannya bukan JSON, indikator tetap diisi skor 1 dan `Disetujui`,
  alasannya dicatat, lalu proses berlanjut.
- Aturan bisnis (akses drive dan rubrik hilang) juga hardcode `Disetujui` dan diputuskan sebelum
  API, sehingga tidak pernah tertimpa hasil AI.

### Pedoman skor AI (V5)

Persona AI diubah dari "verifikator ketat" menjadi "verifikator yang adil dan bijaksana". Prompt
memberi pedoman eksplisit:

| Skor | Kriteria |
| --- | --- |
| 4 | Lengkap, valid, sesuai format. Kekurangan sangat minor tetap 4. |
| 3 | Inti dokumen benar, hanya ada kekurangan administratif kecil (satu tanda tangan, kop kurang jelas, 1-2 kolom kosong). |
| 2 | Dokumen ada, tetapi setengah kriteria utama tidak terpenuhi atau format berantakan. |
| 1 | Kosong, tidak relevan, tidak terbaca, atau salah jenis dokumen. |

Prinsip *benefit of the doubt*: selama substansi inti ada, skor tidak dipotong karena masalah
teknis kecil atau kekurangan data yang tidak signifikan. Aturan ini hanya memengaruhi skor, bukan
status yang tetap `Disetujui`.

## Bila struktur halaman SIBUNDA berubah

`--dry-run` menampilkan indikator yang terdeteksi beserta skor hasil pencocokan rubrik. Bila
daftar indikator kosong atau salah, jalankan `--dump-dom` lalu periksa `debug/verifikasi_*.html`.

Markup yang dipakai saat ini (halaman verifikasi 2026):

- Setiap berkas adalah satu kartu `div.bg-white.rounded-2xl` berisi judul `<h3>` dan baris
  penilaian: tautan "Buka File" (langsung ke Drive), empat tombol skor
  `onclick="setDocScore(id, n)"`, `select#status-<id>` bernilai `Pending`/`Disetujui`/`Ditolak`,
  `input#notes-<id>`, dan `button[onclick="saveDocument(id)"]`.
- Tombol skor memanggil `renderDocuments()` yang membangun ulang seluruh daftar. Urutan bot:
  klik skor, tunggu `RERENDER_WAIT_MS`, cari ulang kartu berdasarkan judul (bukan indeks lama),
  pastikan skor masih aktif, baru isi status dan catatan.
- `saveDocument` mengirim AJAX lalu menampilkan SweetAlert; bot menekan tombol konfirmasi dan
  menunggu halaman dimuat ulang.

Titik penyesuaian ada di `sibunda_bot.py`:

- `_DISCOVER_JS` menentukan cara kartu indikator dikenali dari tombol "Buka File".
- `_confirm_score` memverifikasi skor bertahan setelah render ulang; bila hilang, skor diklik
  ulang sekali lagi sebelum menyerah.
- `_assert_card` memastikan kartu yang akan diisi memang milik indikator yang dimaksud.
- `_set_score`, `_set_status`, `_set_note`, `_click_save`, `_wait_save_result` menangani kontrol
  form dan dialog setelah simpan.

Pencocokan rubrik diatur lewat `MATCH_THRESHOLD`. Bila banyak pasangan meleset, naikkan ke
0.60-0.65; bila banyak indikator tidak dapat pasangan, turunkan ke 0.45. Pasangan dengan skor
rendah tetap dicatat di log sebagai peringatan.

## Troubleshooting

| Gejala | Penyebab dan solusi |
| --- | --- |
| `tesseract is not installed` | `bin/tesseract/tesseract.exe` belum ada; jalankan `python setup_tesseract.py` |
| OCR menghasilkan teks acak | resolusi scan rendah; naikkan `OCR_DPI`, atau kirim berkas versi asli |
| `poppler` tidak ditemukan | tidak wajib; `pypdfium2` dipakai sebagai perender cadangan |
| OCR tidak jalan | cek `--preflight`; pastikan `ind.traineddata` ada di `bin/tesseract/tessdata` |
| Login gagal terus | cek `.env`, atau hapus `state/auth.json` lalu `--login-only` |
| CAPTCHA muncul | jalankan `--headful`; sistem berhenti dan menunggu Anda menyelesaikannya |
| Drive mengembalikan HTML | ubah berbagi link menjadi "Anyone with the link can view" |
| Semua hasil jadi "Akses drive tertutup" | link Drive tidak publik atau ID berkas salah |
| File `.docx` terbaca kosong | dokumen berisi gambar saja; OCR tidak berlaku untuk DOCX, minta versi PDF/scan |
| Google Docs tidak terbaca | pastikan dokumen dibagikan publik agar ekspor `format=txt` berhasil |
| Indikator tidak terdeteksi | jalankan `--dump-dom`, sesuaikan `_DISCOVER_JS` |
| Skor salah pasangan | periksa log "pasangan rubrik lemah", naikkan `MATCH_THRESHOLD` |

## Status pengujian

Yang sudah diverifikasi saat pengembangan (5 November 2026):

- Login ke SIBUNDA dengan akun di `.env` berhasil (`--login-only`).
- Halaman verifikasi NPSN `69903097` memuat 46 indikator; seluruhnya terdeteksi dan 46/46
  berhasil dipasangkan ke baris rubrik tanpa peringatan pasangan lemah.
- Unduhan berkas asli dari Drive berhasil (PDF 2.2 MB), ekstraksi teks 1.749 karakter.
- Pengisian skor, status, dan catatan diuji pada halaman asli **tanpa menekan simpan**, sehingga
  tidak ada data verifikasi yang berubah.
- Alur penuh `main.py` diuji pada server tiruan: login, unduh, ekstraksi, isi form, dialog
  simpan, state, dan laporan CSV/JSON.
- Halaman tiruan juga menguji tombol skor `setDocScore` yang me-render ulang daftar serta radio
  skor tersembunyi.
- Kontrak DeepSeek diuji dengan dua dokumen contoh: dokumen relevan yang kurang satu berkas
  menghasilkan skor 1, dan teks acak juga menghasilkan skor 1.
- V2: pembacaan `npsn_list.csv` diuji dengan BOM, spasi, duplikat, dan baris tidak valid.
- V2: aturan bisnis diuji end-to-end pada server tiruan dengan tiga kartu. Hanya kartu normal
  memanggil AI; kartu tanpa rubrik mendapat skor 4 Disetujui, kartu dengan berkas 403 mendapat
  skor 1 Disetujui "Akses drive tertutup", dan total panggilan AI tetap satu.
- V2: ekstraksi `.docx` (paragraf + tabel) dan URL ekspor Google Docs `format=txt` diuji.
- V3: header CSV `npsn` dengan spasi dan kolom tambahan terbaca; baris NPSN kosong dilewati.
- V3: Google Docs diambil ke memori (`in_memory=True`) tanpa membuat direktori sementara, berkas
  PDF diunduh ke temp lalu direktori tempnya terhapus, termasuk saat unduhan gagal 403.
- V3: dry-run pada halaman asli berjalan headful dengan `slow_mo` 500 ms dan tidak meninggalkan
  folder `downloads/` maupun berkas `sibunda-berkas-*` di temp.
- OCR portable: Tesseract 5.4.0 disalin ke `bin/tesseract/` (52 berkas runtime + `ind.traineddata`
  7,9 MB), `--preflight` hijau tanpa mengandalkan `PATH`.
- OCR portable: gambar dummy dan PDF scan satu halaman terbaca dengan `lang=ind` tanpa
  `TesseractNotFoundError`.
- V4: jawaban AI berisi `Ditolak`, `Pending`, atau tanpa status tetap menghasilkan `Disetujui`;
  dokumen kosong dan jawaban rusak juga `Disetujui` skor 1.
- V4: server tiruan yang me-render ulang daftar (jeda 400 ms) memastikan skor 4 dan 2 bertahan,
  status terpilih `Disetujui`, catatan terisi, dan tidak ada skor kosong yang tersimpan.
- V4: urutan baru diuji pada halaman SIBUNDA asli tanpa menekan simpan: skor 4 bertahan setelah
  render ulang, status `Disetujui`, catatan terisi.
- Web UI: login salah ditolak, login benar masuk, unggah CSV tersimpan, tombol Mulai tampil, dan
  tab Hasil menyediakan unduhan CSV. Diuji dengan Playwright pada Streamlit lokal.
- SQLite: migrasi `progress.json` ke `database.db` menghasilkan 3 baris, `is_done`/`mark` tetap
  konsisten setelah database dibuka ulang, dan uji e2e menulis state ke SQLite.
- V5: dokumen profil A-F dengan kekurangan kecil (website kosong, belum ditandatangani) dinilai
  skor 3, bukan 2.
- V5: surat keterangan dengan kop, nomor, dan tujuan tetapi identitas kurang dinilai skor 3.
- V5: dokumen lengkap (SK + IJOP berlaku) tetap skor 4, IJOP kedaluwarsa tetap skor 2, dan invoice
  yang tidak relevan tetap skor 1. Pelonggaran tidak membuat skor melonjak.

Penyimpanan sungguhan ke SIBUNDA sudah dicoba pengguna (4 data tersimpan). Pengujian otomatis di
repo ini berhenti sebelum menekan simpan agar data verifikasi tidak berubah. Untuk mencoba versi
baru, pakai `--dry-run` lebih dulu, lalu satu lembaga dengan `--force`.

## Struktur berkas

```
config.py               pembacaan .env dan konstanta
sheets_manager.py       baca npsn_list.csv + rubrik, pencocokan indikator
document_processor.py   unduh Drive, ekstraksi PDF/OCR/DOCX/Google Docs
ai_evaluator.py         prompt dan klien DeepSeek dengan validasi JSON
sibunda_bot.py          Playwright: login, navigasi, isi form, simpan
main.py                 orkestrator, aturan bisnis, state, laporan, preflight
app.py                  Web UI Streamlit (login, unggah CSV, run, log, unduh)
.streamlit/config.toml  konfigurasi Streamlit (tanpa file watcher, toolbar minimal)
setup_tesseract.py      siapkan Tesseract portable di bin/tesseract
.env.example            template kredensial
npsn_list.example.csv   contoh daftar NPSN
npsn_list.csv           daftar NPSN kerja (lokal, tidak di-commit)
database.db             state indikator di SQLite (lokal, tidak di-commit)
bin/tesseract/          runtime Tesseract + tessdata (lokal, tidak di-commit)
```

## Keamanan dan data

- Kredensial hanya ada di `.env` dan tidak pernah masuk repo. `.env`, `state/`, `logs/`,
  `reports/`, `debug/`, `npsn_list.csv`, `database.db`, dan `bin/tesseract/` sudah diblokir
  `.gitignore`.
- Web UI punya dua lapis akses: `APP_PASSWORD` di aplikasi dan Basic Auth di reverse proxy.
  Ganti keduanya bila password pernah dibagikan.
- Sesi login SIBUNDA disimpan di `state/auth.json` (cookie). Hapus file itu bila ingin login
  ulang atau memakai akun lain.
- Berkas bukti tidak disimpan permanen, tetapi teks dokumen masuk ke `reports/`, `logs/`, dan
  `database.db`. Ketiganya data sensitif, jangan di-commit atau dibagikan.
- Jalankan hanya pada akun yang berwenang, dan tetap periksa hasil sebelum diserahkan.

## Batasan

- Isian otomatis bergantung pada markup SIBUNDA; perubahan tampilan dapat memerlukan penyesuaian
  selector seperti dijelaskan di atas.
- OCR tidak sempurna untuk dokumen buram, miring, atau tulisan tangan. Karena status selalu
  `Disetujui`, periksa skor 1 pada laporan sebagai sinyal berkas perlu ditelaah manusia.
- Patuhi ketentuan penggunaan SIBUNDA dan kebijakan pengelolaan data lembaga. Berkas bukti tidak
  disimpan permanen; teksnya tetap masuk ke laporan dan log, jadi perlakukan folder `reports/`
  dan `logs/` sebagai data sensitif.

