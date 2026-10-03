
# Context: Scraper Job Listing au.seek.com

Dokumen ini awalnya handoff dari diskusi sebelumnya. Sejak 3 Oktober 2026 sudah diperbarui dengan hasil discovery dan status implementasi. Cara pakai scraper ada di `README.md`.

## Tujuan

Mengambil data lowongan dari **au.seek.com** dengan kriteria:

1. **Kategori:** Accounting, subklasifikasi **Audit - External** dan **Audit - Internal** saja.
2. **Lokasi:** Sydney NSW dan Melbourne VIC saja.
3. **Date range:** bisa difilter berdasarkan tanggal posting.

## Keputusan user

- Target hanya **au.seek.com** (bukan JobStreet, meski nama folder/repo menyebut jobstreet).
- Kebutuhan: **snapshot data terbaru**, bukan data historis terakumulasi.

## Hasil discovery (terverifikasi dari request nyata, 3 Oktober 2026)

Discovery dilakukan dengan Playwright (Chromium) untuk menangkap traffic halaman listing, lalu setiap temuan diuji ulang dengan HTTP client biasa.

### Endpoint

- `GET https://au.seek.com/api/jobsearch/v5/search` mengembalikan JSON dengan status 200.
- Cukup header `User-Agent` browser dan `Accept: application/json`. **Tanpa cookie, tanpa token, tanpa browser.**
- Selama discovery dan pengujian (sekitar 25 request, jeda 2 sampai 5 detik) tidak ada 403 atau 429.
- Path endpoint tercantum di `window.SEEK_CONFIG` pada HTML halaman (`SEEK_API_V5_SEARCH_ENDPOINT_PUBLIC`).

### ID klasifikasi

| Nama | ID |
|---|---|
| Accounting (classification) | `1200` |
| Audit - External (subclassification) | `6144` |
| Audit - Internal (subclassification) | `6145` |

### Parameter query yang terbukti bekerja

| Parameter | Nilai | Catatan |
|---|---|---|
| `siteKey` | `AU-Main` | |
| `locale` | `en-AU` | |
| `where` | `All Sydney NSW`, `All Melbourne VIC` | `Sydney NSW` saja ditafsirkan sebagai suburb Sydney 2000 + radius 50 km |
| `classification` | `1200` | |
| `subclassification` | `6144`, `6145`, atau `6144,6145` | bisa digabung dalam satu request |
| `daterange` | jumlah hari, bebas | bukan hanya preset; `daterange=5` bekerja. Dihilangkan = tanpa filter |
| `sortmode` | `ListedDate` atau `KeywordRelevance` | |
| `page` | mulai dari 1 | halaman di luar jangkauan mengembalikan `data` kosong |
| `pageSize` | 22 (default frontend), 100 diterima | batas maksimum belum diuji |

### Bentuk response

- Top-level: `data` (daftar job), `totalCount`, `location`, `sortModes`, `solMetadata`, `searchParams`, dll.
- Field per job yang dipakai: `id`, `title`, `companyName`, `locations[].label`, `workArrangements.displayText`, `classifications[]`, `workTypes[]`, `salaryLabel`, `listingDate`.
- `listingDate` sudah ISO 8601 (contoh `2026-09-29T01:11:01.000Z`).
- URL job dibentuk dari ID: `https://au.seek.com/job/{id}` (terverifikasi 200).

### Jebakan yang ditemukan

- **`totalCount` menghitung iklan ganda.** Seek menyisipkan salinan `displayType: "promoted"` dari job yang sama, dan salinan itu ikut dihitung. Pagination harus dibandingkan dengan jumlah item mentah, bukan job unik.
- **Satu job bisa punya beberapa klasifikasi** (mis. Audit - Internal + Government - Federal). Subklasifikasi harus diambil dari entri yang ID-nya cocok, bukan entri pertama.
- **Lokasi versi pengiklan tidak selalu akurat.** Ada job berjudul "... Auckland New Zealand" yang dipasang di Sydney.
- **`salaryLabel` dan `workArrangements` sering kosong** (sekitar separuh job).

### Volume data (3 Oktober 2026, 31 hari terakhir)

| Lokasi | Audit - External | Audit - Internal |
|---|---|---|
| All Sydney NSW | 18 | 23 |
| All Melbourne VIC | 8 | 14 |

Tanpa filter tanggal hasilnya sama dengan 31 hari, jadi lowongan tampaknya hanya hidup sekitar 30 hari. Semua kombinasi muat dalam satu halaman.

### Fallback yang tersedia

- Halaman listing HTML juga bisa diambil dengan HTTP client biasa. Pola URL: `https://au.seek.com/jobs-in-accounting/audit-external/in-All-Sydney-NSW?daterange=7`.
- Data job tertanam di `window.SEEK_REDUX_DATA` (`results.results.jobs`), dengan jumlah yang cocok dengan API.
- Fallback ini belum diimplementasikan karena API langsung sudah bekerja.

## Yang belum terverifikasi

- Batas maksimum `pageSize` dan batas jumlah halaman.
- Perilaku rate limit saat request lebih sering atau volume lebih besar.
- Kestabilan endpoint dan ID dalam jangka panjang (endpoint internal bisa berubah tanpa pemberitahuan).
- Pagination multi-halaman dan jalur retry 403/429 hanya teruji lewat unit test, belum pernah terpicu terhadap Seek sungguhan.

## Batasan yang diketahui

- **Job yang sudah expired hilang dari listing.** Untuk data historis, solusinya scraper terjadwal yang mengakumulasi ke database (contoh cron ada di README).
- **Rate limit dan anti-bot:** jeda 2 sampai 5 detik dengan jitter, tanpa request paralel, header browser yang wajar, retry dengan backoff saat 403/429/5xx.

## Catatan legal

Terms of Service Seek melarang akses otomatis. User sudah diberi tahu risikonya. Jaga skala tetap kecil dan sopan (rate limit ketat, tidak membebani server). Jangan membuat fitur untuk menghindari proteksi secara agresif (rotating proxy, captcha solver, dll) kecuali user secara eksplisit memutuskan dan memahami risikonya.

## Status implementasi

Rencana awal punya empat tingkat (JSON API, JSON tertanam di HTML, Playwright, CSS selector). Tingkat pertama berhasil, jadi sisanya tidak dibutuhkan.

| File | Isi |
|---|---|
| `scraper.py` | CLI: fetch 4 kombinasi, pagination, retry, upsert SQLite, export CSV |
| `config.toml` | Lokasi, ID subklasifikasi, date range, jeda, retry, path database |
| `tests/test_scraper.py` | Unit test parsing, upsert, pagination, duplikat promoted, retry |
| `README.md` | Install, cara run, opsi, cron |

Stack: Python 3.11+, `httpx`, `sqlite3` dan `tomllib` bawaan.

### Skema output

Tabel `jobs`, primary key `job_id` (upsert):

- `job_id`
- `title`
- `company`
- `location` (label lokasi dari Seek, mis. "Macquarie Park, Sydney NSW")
- `search_location` (nilai `where` yang dicari, mis. "All Sydney NSW")
- `work_arrangement` (on-site/hybrid/remote, kalau ada)
- `subclassification` (Audit - External / Audit - Internal)
- `work_type` (full time, contract, dll)
- `salary` (kalau ditampilkan)
- `listed_at` (ISO 8601 UTC)
- `job_url`
- `scraped_at` (ISO 8601 UTC)

### Definition of done

1. Satu perintah menjalankan scrape untuk 4 kombinasi dengan date range yang bisa dipilih: **selesai**.
2. Pagination sampai habis, dengan peringatan jika batas halaman tercapai: **selesai** (teruji lewat unit test).
3. Tidak ada duplikat setelah dijalankan berkali-kali: **selesai** (63 baris, 63 `job_id` unik setelah tiga run).
4. Log jumlah job per kombinasi dan jumlah request gagal: **selesai**.
5. README: **selesai**.

## Cara kerja yang diharapkan untuk perubahan berikutnya

- Verifikasi setiap asumsi dengan request nyata sebelum membangun di atasnya.
- Kalau diblok atau ragu, laporkan apa adanya dan tanyakan ke user, jangan menebak ID atau endpoint.
- Jika API mulai menolak request, coba fallback `SEEK_REDUX_DATA` di HTML sebelum beralih ke Playwright.
