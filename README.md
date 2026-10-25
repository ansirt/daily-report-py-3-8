# DC Report Scheduler (Python)

Rewrite dari `cmdDailyReport` (C#): cronjob yang menjalankan query ke Oracle/MySQL/SQL Server,
membuat file **.xlsx**, lalu mengirimnya lewat email. Dilengkapi UI admin (FastAPI + Vue).

## Fitur
- Multi koneksi database: Oracle, MySQL, SQL Server (password terenkripsi di SQLite)
- Database aplikasi: SQLite (`data/app.db`)
- Admin login (cookie HttpOnly), ganti password
- Pantau job: status, jadwal berikutnya, riwayat + log tiap run, unduh file hasil
- Jalankan manual (dengan/tanpa email) dan **kirim ulang email** bila file sudah jadi tapi email gagal
- Edit query tiap job (tombol "Uji query" menampilkan 20 baris), edit To/CC/subject/isi email/nama file
- Edit From, SMTP host/port, user, password (menu Email / SMTP) + email uji coba
- Vue di-host lokal (`static/vendor/vue.global.prod.js`), tanpa CDN

## Instalasi
```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
py seed.py                      # membuat tabel database + user admin (tanya password)
py run.py                       # http://127.0.0.1:8000
```
`seed.py` hanya membuat tabel dan user admin (tidak membaca config.xml). Opsi: `--username admin --password RAHASIA123`,
dan `--reset` untuk mengganti password user yang sudah ada. Aman dijalankan ulang. Koneksi database, SMTP, dan job dibuat lewat UI.

Jalankan **satu proses saja** (scheduler hidup di dalam proses); di Windows daftarkan `python run.py` sebagai service
(mis. NSSM) atau Task Scheduler "At startup".

## Environment variable (opsional)
| Nama | Fungsi |
|---|---|
| `HOST`, `PORT` | alamat server (default 127.0.0.1:8000) |
| `DATA_DIR` | lokasi `app.db`, `secret.key`, folder `output/` dan `csv/` |
| `ENABLE_PYTHON_SCRIPTS=1` | mengaktifkan node Skrip Python (kode Python penuh; default mati) |
| `MYSQLDUMP_PATH`, `EXPDP_PATH` | lokasi mysqldump / expdp jika tidak ada di PATH |
| `CSV_DIR` | folder file CSV (default `DATA_DIR/csv`): upload dan hasil Export CSV |
| `APP_SECRET_KEY` | kunci enkripsi password & JWT (default: dibuat otomatis di `data/secret.key`; **backup file ini**) |
| `APP_TIMEZONE` | default `Asia/Jakarta` |
| `COOKIE_SECURE=1` | aktifkan jika diakses lewat HTTPS |
| `ORACLE_CLIENT_LIB_DIR` | folder Oracle Instant Client, hanya untuk Oracle lama (< 12.1) yang butuh thick mode |

## Catatan migrasi dari C#
- Placeholder `sqlParam` tetap dipakai. `date` = tanggal sesuai "Rentang data" job
  (kemarin; hari Senin otomatis Sabtu+Minggu seperti versi lama), `list` = daftar nilai dari query sebelumnya
  (menggantikan alur `booking_fresh` -> `po_pts_fresh`).
- Variabel di subject/isi email/nama file: `{hari}`, `{tgl}` (16-Oct-26), `{tgl_long}` (16-Oct-2026).
- Layout **Summary Order** memakai rumus Excel yang sama dengan versi lama. Nilainya dihitung saat file dibuka di Excel
  (openpyxl tidak menghitung rumus), jadi pratinjau email/HP bisa menampilkan kosong.
- Query hanya boleh `SELECT`/`WITH` satu statement. Tetap gunakan akun database read-only.
- Jika Exchange hanya menerima NTLM (login `domain\user`), pilih metode login **NTLM** di menu Email / SMTP,
  lalu cek dengan email uji coba.

## Langkah turunan & staging (gabung data antar database)
Setiap job terdiri dari beberapa **langkah** (urut dari atas). Jenis langkah:

| Jenis | Fungsi | Untuk data |
|---|---|---|
| Query SQL | SELECT ke satu koneksi | berapa pun |
| Join | join/lookup dua hasil langkah sebelumnya (left/inner/right/outer) | kecil |
| Pivot | baris x header, fungsi sum/count/avg/min/max, kolom Total | kecil |
| Simpan ke database (staging) | hasil query disalin ke tabel `STG_...` di database tujuan | besar |

- Gabung/Pivot dihitung di aplikasi dan menolak sumber di atas **batas baris** (default 1000, bisa diubah per langkah).
- Untuk data besar: langkah *staging* menyalin hasil ke database tujuan, lalu tambah langkah *Query SQL* pada
  koneksi tujuan yang meng-join tabel `STG_...` dengan tabel di sana (bisa juga dibuat `GROUP BY`/`PIVOT` di SQL).
- Tabel staging **wajib berawalan `STG_`**, dibuat ulang (DROP + CREATE) tiap run; akun tujuan perlu hak CREATE/DROP/INSERT.
  Pakai nama tabel berbeda untuk tiap job. Nama kolom dijadikan huruf besar dan maksimal 30 karakter.
- Matikan "Jadikan sheet" pada langkah pembantu supaya Excel hanya berisi hasil akhir.
- Database SQLite lama otomatis dimigrasi saat aplikasi start (tidak perlu menghapus data).

## Alur node (mirip KNIME, berurutan)
Setiap job = daftar **node** yang dijalankan dari atas ke bawah. Node mengambil data dari node sebelumnya
lewat pilihan "Data sumber" (#nomor). Jenis node:

| Node | Fungsi |
|---|---|
| Get Data | query SELECT ke koneksi pilihan (Oracle / MySQL / SQL Server); boleh banyak node dengan koneksi berbeda |
| Join | pilih kolom kunci (boleh banyak), kolom yang diambil dari kiri dan kanan, jenis inner / left / right / full outer. Output **satu hasil**, atau **terpisah**: *cocok*, *hanya kiri*, *hanya kanan* (node berikutnya memilih salah satunya) |
| Pivot | pengelompokan / tabel silang; **tiap kolom nilai punya fungsi sendiri** (sum, count, count distinct, avg, min, max, pertama, gabung teks) |
| Staging | salin hasil ke tabel `STG_...` untuk data besar |
| Tulis ke database | **insert**, **update**, **upsert**, atau **SQL bebas** (`UPDATE t SET s=:status WHERE id=:id`) dengan nilai dari data sumber |

Report Excel dan email tetap diatur di tingkat job: node yang dicentang "Jadikan sheet" menjadi sheet, lalu file
dikirim ke penerima. **Job tanpa sheet (hanya menulis ke database) tidak membuat file dan tidak mengirim email.**

### Keamanan node tulis
- Koneksi harus dicentang **"Izinkan penulisan"** di menu Koneksi database; tanpa itu node tulis ditolak.
- Nilai dikirim sebagai **bind parameter**, tidak digabung ke teks SQL. SQL bebas hanya boleh satu statement INSERT/UPDATE/MERGE.
- Update/upsert **wajib punya kolom kunci**. Nama tabel/kolom divalidasi (huruf, angka, `_`, `$`, `#`).
- Ada batas baris per node (default 10.000).
- **Dry-run**: tombol Jalankan pada job yang menulis defaultnya dry-run. Semua node dijalankan, jumlah baris yang akan berubah dicatat di log, lalu **rollback**; email tidak dikirim.
- Perubahan di-commit hanya setelah seluruh alur (termasuk pembuatan file) berhasil; jika ada node gagal, semuanya di-rollback.
  Commit dilakukan per koneksi, jadi bila job menulis ke dua database berbeda, keduanya tidak benar-benar atomik.
- Upsert MySQL memakai `ON DUPLICATE KEY UPDATE` (butuh PRIMARY KEY/UNIQUE); jumlah baris terpengaruh MySQL menghitung 2 untuk baris yang di-update.
- Node `merge` dari versi sebelumnya otomatis diubah menjadi `join` saat aplikasi start.

## Variabel tanggal & rentang manual
- **Rentang data** job: *Hari ini* atau *Kemarin* (Senin = Sabtu+Minggu). Di dialog **Jalankan** bisa diisi **tanggal awal/akhir manual**
  untuk mengulang laporan hari tertentu; berlaku juga untuk kirim ulang email dari run tersebut.
- Di **SQL** (Get Data, SQL bebas node tulis), **URL/body/header API**: `{tgl_awal}` `{tgl_akhir}` (2026-10-17) dan
  `{tgl_awal_compact}` `{tgl_akhir_compact}` (20261017). Contoh: `WHERE tgl BETWEEN DATE '{tgl_awal}' AND DATE '{tgl_akhir}'`.
- `sqlParam` tetap tersedia; dengan tanggal manual ia memakai tanggal absolut (mis. `date '2026-10-01'`).
- Di subject, isi email, dan nama file: `{hari}` `{tgl}` `{tgl_long}` (ikut rentang manual juga).

## Migrasi data besar (> 1 juta baris): stream + CSV
Alur: **Get Data (mode stream) -> Export ke CSV -> Import CSV -> Tulis ke database (insert/upsert)**.
- *Mode stream* pada node Get Data: query baru dijalankan saat dipakai node berikutnya dan dibaca bertahap (5000 baris per potongan),
  jadi tidak memenuhi memori. MySQL memakai cursor unbuffered.
- **Export ke CSV** menulis file di `CSV_DIR/exports/<kode job>/` (pemisah, encoding, header bisa diatur) dan mencatat tipe kolom di `.meta.json`.
- **Import CSV** membaca hasil Export (pilih nodenya) atau file di folder CSV (bisa diunggah dari UI). Tipe kolom bisa dipaksa:
  `tgl=date:%d/%m/%Y`, `qty=int`, `harga=num`. File dibaca bertahap.
- **Tulis ke database** menerima sumber stream/CSV tanpa batas baris, dengan **commit tiap N baris** (mis. 50000). Data yang sudah
  di-commit tidak ikut rollback jika gagal di tengah, jadi gunakan *upsert* agar aman diulang.
- **Dry-run** pada sumber besar hanya memproses 1000 baris pertama (file export `.dryrun.csv`), lalu rollback.
- Node stream/CSV tidak bisa dijadikan sheet Excel. Node Join/Pivot yang memakai sumber stream memuatnya ke memori sesuai "Batas baris".
- Nilai NULL ditulis sebagai kolom kosong di CSV dan dibaca kembali sebagai NULL (string kosong dan NULL tidak dibedakan).
- Performa: Oracle dan MySQL (insert/upsert) memakai executemany per potongan; SQL Server dan UPDATE di MySQL berjalan per baris sehingga lebih lambat untuk jutaan baris.

## Node Get Data dari API
URL (GET/POST, boleh memakai variabel tanggal), autentikasi Bearer / Basic / API key (rahasia disimpan terenkripsi), header tambahan, body JSON,
path data (`data.items`), pemetaan kolom opsional, pagination (`{page}`, `{offset}`, `{limit}` atau URL halaman berikutnya dari respons),
batas halaman dan baris. Tombol **Uji API** mengambil 1 halaman dan menampilkan 20 baris. Respons harus JSON; hasil dimuat di memori.

## Pivot dengan fungsi per kolom nilai
Kolom baris (pengelompokan) dipisah koma; kolom header opsional. Setiap **kolom nilai** punya fungsinya sendiri:
Sum, Avg, Min, Max (butuh angka), Count (jumlah baris), Count distinct, Pertama, Gabung teks. Dengan beberapa kolom nilai, nama kolom hasil
berbentuk `header - kolom (fungsi)`. Tanpa kolom header, hasilnya ringkasan per kelompok (`kolom (fungsi)`). Node Pivot lama otomatis terbaca.
Node **Bandingkan data sudah dihapus**; job lama yang masih memilikinya akan ditolak saat disimpan atau dijalankan sampai node itu dihapus.

## Node Skrip Python (pandas / numpy)
Pengolahan data bebas dengan DataFrame. **Default nonaktif**: aktifkan dengan `ENABLE_PYTHON_SCRIPTS=1` lalu `pip install -r requirements-scripting.txt`.
- Tiap input node menjadi DataFrame: `inputs[0]`, `inputs[1]`, ...; `df` = input pertama. Tersedia `pd`, `np`, `params` (tgl_awal, tgl_akhir, hari, tgl, dry_run, ...).
- Wajib mengisi `result` (DataFrame). Opsional `extra = {"nama": DataFrame}` untuk port tambahan (daftarkan namanya di "Port tambahan"; node berikutnya memilihnya).
- Skrip berjalan di **proses Python terpisah** dengan batas waktu (proses dihentikan beserta turunannya) dan tanpa rahasia aplikasi di lingkungannya.
  **Ini bukan sandbox**: skrip tetap bisa membaca file dan mengakses jaringan sebagai user yang menjalankan aplikasi. Gunakan hanya oleh admin tepercaya.
- `print()` masuk ke log run. Data masuk/keluar lewat memori (dibatasi "Batas baris", default 500.000).

## Node Dump database (seperti DBeaver)
Menjalankan alat dump bawaan database dengan opsi yang dipilih dari katalog (tanpa shell, argumen tidak bebas):
- **MySQL**: `mysqldump` (di server aplikasi). Pilih tabel dari daftar (atau kosong = seluruh database), opsi seperti `--single-transaction`, `--routines`,
  `--no-data`, `--where`, `--set-gtid-purged`, dll. Hasil: `.sql` atau `.sql.gz` di folder CSV aplikasi, dapat diunduh dari menu **File**.
- **Oracle**: `expdp` (Data Pump) lewat parfile sementara. File `.dmp` ditulis di **server database** (folder directory object), bukan di server aplikasi.
  Profil ini belum diuji terhadap server Oracle sungguhan; gunakan tombol **Lihat perintah** untuk memeriksanya.
- **SQL Server tidak didukung** (tidak ada alat dump sisi klien yang aman); gunakan Export ke CSV.
- Password tidak ditaruh di argumen perintah (MySQL: `MYSQL_PWD`; Oracle: parfile 0600 yang dihapus setelah selesai) dan disamarkan di tampilan.
- Dry-run hanya menampilkan perintah. Batas waktu menghentikan proses dump. Akun database yang dipakai butuh hak baca tabel terkait (MySQL 8 biasanya perlu
  opsi `--no-tablespaces` bila akun tidak punya hak PROCESS).
- File dump dihapus otomatis sesuai masa simpan (menu Email / SMTP).
