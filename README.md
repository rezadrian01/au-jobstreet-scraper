# Job Scraper: Seek Australia & JobStreet Indonesia

Mengambil lowongan **Accounting** dari **au.seek.com** dan **id.jobstreet.com** lewat UI web lokal, dengan
kriteria yang bisa diganti-ganti: situs, lokasi, subklasifikasi, rentang hari, dan filter software.

Sumber data adalah JSON API yang dipakai frontend kedua situs (pencarian lewat `/api/jobsearch/v5/search`,
deskripsi lewat `/graphql`), bukan parsing HTML. Tidak ada database: setiap tarikan mengambil data terbaru
dari situs.

## Install

Butuh Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Menjalankan UI

```bash
.venv/bin/streamlit run app.py
```

Browser akan terbuka di `http://localhost:8501`.

1. Di panel kiri, atur semua kriteria: **sumber data**, **lokasi**, **subklasifikasi**, **rentang hari**,
   dan **software**.
   - Lokasi dikosongkan = semua lokasi. Lokasi lain bisa diketik langsung.
   - Default Australia: Sydney, Melbourne, Brisbane. Default Indonesia: semua kota.
   - Subklasifikasi dikosongkan = semua subklasifikasi Accounting.
   - Software opsional (mis. `Xero, MYOB`). Jika diisi, hanya lowongan yang menyebut salah satunya yang
     ditampilkan, dan kolom **Software** menunjukkan mana yang cocok. Bisa diubah kapan saja tanpa menarik ulang.
2. Klik **Tarik data**.
3. **Download CSV** atau **Download Excel** berisi baris yang sedang ditampilkan.

## Data yang diambil

| Kolom | Isi |
|---|---|
| `title` | Judul lowongan |
| `description` | Isi lengkap lowongan (deskripsi dan requirement), teks polos |
| `subclassification` | Mis. Audit - External |
| `company` | Nama perusahaan / pengiklan |
| `location` | Lokasi versi situs, mis. "Macquarie Park, Sydney NSW" |
| `salary` | Kosong jika pengiklan tidak mencantumkan |
| `listed_at` | Tanggal posting. Di UI dan hasil download: waktu lokal situs; di CLI: ISO 8601 UTC |
| `software` | Software dari filter yang disebut di judul atau deskripsi |
| `work_type`, `work_arrangement` | Full time / contract, on-site / hybrid / remote |
| `job_id`, `search_location`, `job_url`, `scraped_at` | Pelengkap |

## Hal yang perlu diketahui

- **Tidak bisa backdate.** Kedua situs hanya menampilkan lowongan aktif, sekitar 30 hari ke belakang.
  Karena tidak ada database, lowongan yang sudah hilang dari situs tidak bisa diambil lagi.
- **Deskripsi butuh 1 request per lowongan** dengan jeda 1,5-3 detik. Kira-kira 2,5 menit per 60 lowongan.
  Kriteria yang lebar (mis. semua Accounting di Jakarta, hampir 2.000 lowongan) bisa lebih dari satu jam.
  Hilangkan centang **Ambil deskripsi lowongan** untuk melihat jumlahnya dulu, lalu ambil deskripsinya dengan
  tombol yang muncul di halaman hasil.
- **Filter software mencocokkan teks**, tanpa peduli huruf besar/kecil dan per kata utuh ("SAP" tidak cocok
  dengan "sapling"). Software yang tidak disebut namanya tidak akan ketemu, dan nama yang juga kata umum
  (mis. "Accurate", "Sage") bisa cocok dengan kata biasa.
- **Duplikat dibuang.** Situs menyisipkan salinan "promoted" dari job yang sama; job yang muncul di lebih
  dari satu lokasi juga hanya diambil sekali.
- **Peringatan data terpotong** muncul jika `max_pages` tercapai sebelum semua hasil terambil.
- **Sopan terhadap server:** jeda acak antar request, tanpa request paralel, retry dengan backoff saat
  403/429/5xx. Terms of Service Seek melarang akses otomatis; jaga skala tetap kecil dan jalankan lokal saja.

## Konfigurasi

Semua di `config.toml`: daftar situs dan lokasi, ID subklasifikasi, pilihan default, jeda antar request, retry.

## CLI (opsional)

Logika yang sama tanpa UI, menulis CSV:

```bash
.venv/bin/python scraper.py --site seek_au --daterange 7 --software "Xero, MYOB" --csv data/jobs.csv
.venv/bin/python scraper.py --site jobstreet_id --location "Jakarta Raya" --subclass "Taxation" --no-description
```

`--subclass all` = semua subklasifikasi Accounting. Lihat `--help` untuk semua opsi.

## Test

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```
