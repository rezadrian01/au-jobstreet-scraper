# Seek Audit Job Scraper

Mengambil lowongan **Accounting > Audit - External / Audit - Internal** di **Sydney** dan **Melbourne**
dari au.seek.com, menyimpannya ke SQLite, dengan opsi export CSV.

Sumber data adalah JSON API yang dipakai frontend Seek (`/api/jobsearch/v5/search`), bukan parsing HTML.

## Install

Butuh Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Menjalankan

```bash
# 7 hari terakhir (default dari config.toml), simpan ke data/seek_jobs.db
.venv/bin/python scraper.py

# 14 hari terakhir + export hasil run ini ke CSV
.venv/bin/python scraper.py --daterange 14 --csv data/jobs.csv

# Semua lowongan yang masih aktif (tanpa filter tanggal)
.venv/bin/python scraper.py --daterange 0 --csv data/jobs.csv
```

| Opsi | Arti |
|---|---|
| `--daterange N` | Lowongan yang diposting N hari terakhir. `0` = tanpa filter. |
| `--csv FILE` | Export job dari run ini ke CSV. |
| `--csv-all` | Bersama `--csv`: export seluruh isi database, bukan hanya run ini. |
| `--config FILE` | Pakai file config lain. |
| `-v` | Log detail tiap request. |

Satu perintah menjalankan 4 kombinasi (2 lokasi x 2 subklasifikasi). Exit code `1` jika ada kombinasi yang gagal.

## Konfigurasi

Semua di `config.toml`: lokasi, ID subklasifikasi, date range default, ukuran halaman, jeda antar request, retry.

- Lokasi memakai nilai `where` Seek. `All Sydney NSW` = seluruh wilayah Sydney; `Sydney NSW` saja berarti
  suburb Sydney 2000 + radius 50 km.
- ID: Accounting = `1200`, Audit - External = `6144`, Audit - Internal = `6145`.

## Output

Tabel `jobs` (primary key `job_id`, di-upsert sehingga run berulang tidak membuat duplikat):

`job_id`, `title`, `company`, `location`, `search_location`, `work_arrangement`, `subclassification`,
`work_type`, `salary`, `listed_at` (ISO 8601 UTC), `job_url`, `scraped_at` (ISO 8601 UTC).

`salary` dan `work_arrangement` kosong jika pengiklan tidak mencantumkannya.

## Hal yang perlu diketahui

- **Database terakumulasi.** Job yang sudah hilang dari Seek tetap ada di database. `--csv` tanpa `--csv-all`
  hanya berisi job yang terlihat di run tersebut (snapshot terbaru). Untuk snapshot bersih, hapus
  `data/seek_jobs.db` sebelum menjalankan.
- **Lowongan hanya hidup sekitar 30 hari** di Seek, jadi `--daterange` di atas ~31 tidak menambah hasil.
- **Duplikat promoted.** Seek menyisipkan salinan "promoted" dari job yang sama; scraper membuangnya dan
  mencatat jumlahnya di log.
- **Peringatan data terpotong** muncul jika `max_pages` tercapai sebelum semua hasil terambil.
- **Sopan terhadap server:** jeda acak 2-5 detik antar request, tanpa request paralel, retry dengan backoff
  saat 403/429/5xx. Terms of Service Seek melarang akses otomatis; jaga skala tetap kecil.

## Menjadwalkan (jika butuh data historis)

Contoh cron harian pukul 07:00, mengakumulasi ke database yang sama:

```cron
0 7 * * * cd "/path/ke/project" && .venv/bin/python scraper.py --daterange 3 >> data/scrape.log 2>&1
```

## Test

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```
