
# Context: Scraper Job Listing au.seek.com dan id.jobstreet.com

Dokumen ini awalnya handoff dari diskusi sebelumnya. Sejak 3 Oktober 2026 sudah diperbarui dengan hasil discovery dan status implementasi; pada 4 Oktober 2026 cakupannya diperluas (UI web, JobStreet Indonesia, deskripsi lowongan, filter software). Cara pakai scraper ada di `README.md`.

## Tujuan

Mengambil data lowongan **Accounting** dari **au.seek.com** dan **id.jobstreet.com** lewat UI web lokal, dengan kriteria yang bisa diganti:

1. **Situs:** Seek Australia atau JobStreet Indonesia.
2. **Subklasifikasi:** semua subklasifikasi di bawah Accounting bisa dipilih (default Audit - External dan Audit - Internal).
3. **Lokasi:** default Sydney, Melbourne, Brisbane untuk Australia; untuk Indonesia default **semua kota** (permintaan user). Lokasi lain bisa dipilih atau diketik.
4. **Date range:** N hari terakhir, maksimal sekitar 30.
5. **Software:** daftar software opsional; jika diisi, hanya lowongan yang menyebut salah satunya yang diambil.

6. **Requirement (AI):** kolom `requirements` berisi poin-poin requirement yang diekstrak Gemini dari deskripsi lowongan.

Data per lowongan yang diminta: job title, job description, requirements, subclass, company, location, salary, tanggal, software.

## Keputusan user

- **UI:** Streamlit, dijalankan lokal (bukan di-hosting).
- **Tanpa SQLite.** Setiap tarikan mengambil semua data dari situs, tanpa peduli pernah diambil atau belum. Konsekuensinya tidak ada riwayat: lowongan yang sudah hilang dari situs tidak bisa dilihat lagi.
- **Filter software:** cocok jika menyebut **salah satu** software (OR), dengan kolom yang menunjukkan mana yang cocok.
- **AI:** Gemini di Vertex AI, ditagih ke project GCP yang diatur user di `.env`. Ekstrak poin requirement termasuk "desirable qualifications".
- **Cakupan subklasifikasi:** semua di bawah Accounting. Ini asumsi; user belum menjawab apakah klasifikasi lain juga perlu.

## Hasil discovery (terverifikasi dari request nyata, 3-4 Oktober 2026)

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

### Deskripsi lowongan (GraphQL)

- Hasil pencarian hanya berisi `teaser` dan `bulletPoints`. Isi lengkap lowongan diambil lewat `POST {base_url}/graphql`:
  `query jobDetails($id: ID!) { jobDetails(id: $id) { job { id content } } }`
- `content` berupa HTML; scraper mengubahnya menjadi teks polos.
- Bekerja di kedua situs tanpa cookie atau token. Satu request per lowongan.
- Objek `job` juga punya `isExpired` dan `expiresAt` (belum dipakai).

### JobStreet Indonesia

- Platform yang sama dengan Seek: endpoint pencarian dan GraphQL identik di `https://id.jobstreet.com`.
- `siteKey=ID-Main`, `locale=en-ID` (nama subklasifikasi dalam bahasa Inggris) atau `id-ID`.
- ID klasifikasi dan subklasifikasi **sama** dengan Seek (1200, 6144, 6145, dst).
- Lokasi yang terverifikasi: `Jakarta Raya`, `Jawa Barat`, `Jawa Timur`, `Banten`, `Bali`, `Surabaya`, `Bandung`. `where` dihilangkan = seluruh Indonesia.
- **Halaman HTML diblok Cloudflare (403)** untuk HTTP client biasa, jadi fallback `SEEK_REDUX_DATA` tidak tersedia untuk JobStreet. API dan GraphQL tidak diblok.
- Volume jauh lebih besar: seluruh Accounting di Jakarta Raya 1.899 lowongan, seluruh Indonesia 4.226 (Australia: 462).

### Lokasi dan subklasifikasi tambahan

- Lokasi Seek yang terverifikasi: `All Sydney NSW`, `All Melbourne VIC`, `All Brisbane QLD`, `All Perth WA`, `All Adelaide SA`, `All Canberra ACT`, `All Australia`.
- Daftar 25 subklasifikasi Accounting beserta ID-nya diambil dari halaman listing Seek dan disimpan di `config.toml`.
- `subclassification` dihilangkan = semua subklasifikasi di bawah klasifikasi.

### Ekstraksi requirement (Vertex AI, 4 Oktober 2026)

- SDK `google-genai` dengan `vertexai=True`, `location="global"`, output JSON terstruktur (`response_schema`), `temperature=0`.
- Model yang tersedia di project saat itu antara lain `gemini-3.8-flash` dan `gemini-3.5-flash-lite`. Keduanya diuji pada lowongan yang sama: `gemini-3.8-flash` lebih rapi (default); `gemini-3.5-flash-lite` lebih cepat (1,7 vs 4,7 detik) tetapi ikut mengambil satu poin yang bukan requirement.
- Bekerja untuk deskripsi berbahasa Inggris dan Indonesia; poin dikembalikan dalam bahasa aslinya.
- Poin opsional diberi awalan `(Desirable) `.
- Pengaturan GCP ada di `.env` (tidak di-commit; contoh di `.env.example`): `GCP_PROJECT_ID`, `GCP_LOCATION`, `GCP_CLIENT_EMAIL`, `GCP_PRIVATE_KEY`.
- Urutan autentikasi: service account dari `.env`; jika `GCP_CLIENT_EMAIL` kosong, Application Default Credentials; jika itu juga belum ada, token `gcloud auth print-access-token`. Yang teruji live baru jalur token gcloud; jalur service account hanya teruji lewat unit test dengan key buatan.
- Jebakan: setelah `pip install` yang meng-upgrade library (mis. `websockets` saat memasang `google-genai`), proses Streamlit yang masih berjalan harus di-restart, kalau tidak muncul `ImportError` dari modul lama yang masih termuat.
- Harga per panggilan belum dicek.

### LinkedIn dan Glints (4 Oktober 2026)

**LinkedIn** ditambahkan sebagai sumber ketiga (`jobspy_sources.py`, situs `linkedin_id` di `config.toml`):

- Diambil lewat library `python-jobspy` (MIT), yang memakai endpoint tamu LinkedIn tanpa login:
  `/jobs-guest/jobs/api/seeMoreJobPostings/search` (10 lowongan per halaman, berhenti di 1.000) dan
  `/jobs-guest/jobs/api/jobPosting/{id}` untuk deskripsi. Endpoint itu juga diuji langsung dan bekerja.
- Tidak ada taksonomi subklasifikasi; penyaringan lewat keyword, yang dicocokkan ke seluruh isi lowongan
  sehingga hasilnya berisik (mis. "Manufacturing Quality Technician" muncul untuk "audit").
- Tanggal posting hanya tingkat hari; gaji kosong di semua hasil uji.
- Pencarian halaman LinkedIn melaporkan 439 hasil untuk 7 hari.
- Rancangan pertama (satu panggilan JobSpy per keyword, deskripsi ikut diambil) membuat progress bar diam
  berpuluh menit dan dilaporkan user sebagai "stuck". Sekarang pencarian dipecah per batch 25 hasil
  (`offset`) tanpa deskripsi, lalu deskripsi diambil sendiri dari endpoint tamu hanya untuk lowongan yang
  lolos filter judul. Uji live: "audit", 3 hari: 135 lowongan, 9 lolos filter judul, selesai 106 detik
  termasuk deskripsi dan AI.
- Opsi "Keyword harus ada di judul" (default aktif) dan keyword ganda ditambahkan atas permintaan user.
- `joeyism/linkedin_scraper` ditolak: wajib login akun, pencarian lowongan tanpa filter tanggal atau
  pagination, GPL-3.0.

**Indeed Indonesia** ditambahkan sebagai sumber (situs `indeed_id`), atas pilihan user setelah survei portal
gratis pengganti Glints:

- Lewat JobSpy (`site_name=["indeed"]`, `country_indeed="Indonesia"`), tanpa login; hasil pencarian sudah
  termasuk deskripsi, jadi tidak ada tahap deskripsi terpisah. Paging lewat `offset`, 50 per batch.
- Uji live lewat UI: "audit", 7 hari: 150 lowongan dalam 42 detik, 15 lolos filter judul, semuanya mendapat
  requirement AI. Gaji terisi di sebagian kecil lowongan.
- Jebakan: koneksi dari jaringan user ke Indeed sering ter-reset (`ConnectionResetError`), dan JobSpy
  menelan error itu lalu mengembalikan hasil kosong. `jobspy_sources.py` mendeteksinya lewat log JobSpy dan
  mencoba ulang batch sampai 6 kali; jika tetap gagal, hasil parsial dipertahankan dengan peringatan.
- Portal lain yang diuji aksesnya (belum dipasang): Kalibrr (JSON API `/kjs/job_board/search`, ada kolom
  `qualifications`, 55 lowongan "audit"), Dealls (`api.sejutacita.id`, 6 lowongan), Karirhub Kemnaker
  (`api.kemnaker.go.id`, 31 lowongan, agregator termasuk Glints dan Kalibrr), Loker.id dan KitaLulus (HTML).
  Jora diblok Cloudflare.

**Glints** ditambahkan sebagai sumber lewat Apify (`glints.py`, situs `glints_id`):

- Tanpa login, API pencarian (`/api/v2-alc/graphql?op=searchJobsV3`) hanya mengembalikan 4 lowongan yang
  tidak terkait keyword, lalu muncul dialog login.
- Semua scraper open-source yang ditemukan memakai login (cookie atau email/password).
- Pilihan yang diambil: actor Apify `truefetch~glints-job-listing` ($0,005 per hasil + $0,01 per run,
  tanpa login Glints). Token di `.env` sebagai `APIFY_TOKEN`; akun user paket FREE ($5 per bulan).
- Input actor: `keyword`, `country`, `max_results` (wajib), `location`, `posted_since`, `remote_only`,
  `job_type`, `currency`. Hasil sudah termasuk deskripsi, gaji, dan `job_function`.
- Jebakan: `posted_since` tidak menyaring berdasarkan tanggal posting. Uji live "audit", 7 hari, 20 hasil:
  hanya 5 yang benar-benar diposting dalam 7 hari; 15 sisanya lebih lama (sampai Juni) dan dibuang di sisi
  kita, tetapi tetap ditagih.
- Jebakan: koneksi dari jaringan user ke `api.apify.com` sering ter-reset (kira-kira 2 dari 3 percobaan);
  klien mencoba ulang otomatis.

### Fallback yang tersedia

- Halaman listing HTML juga bisa diambil dengan HTTP client biasa. Pola URL: `https://au.seek.com/jobs-in-accounting/audit-external/in-All-Sydney-NSW?daterange=7`.
- Data job tertanam di `window.SEEK_REDUX_DATA` (`results.results.jobs`), dengan jumlah yang cocok dengan API.
- Fallback ini belum diimplementasikan karena API langsung sudah bekerja, dan hanya berlaku untuk Seek (lihat JobStreet di atas).

## Revisi 6 Oktober 2026 (masukan klien)

- **Filter software dihapus sepenuhnya** (keputusan user, membalik rancangan awal). Kolom `software`
  sekarang diisi AI dalam panggilan yang sama dengan requirement (`ai.py`, skema JSON berisi
  `requirements` dan `software`), bisa lebih dari satu per lowongan, dengan nama produk dinormalkan
  (mis. "Ms. Excel" menjadi "Microsoft Excel"). `match_software`, `apply_software`, `parse_terms`, dan
  opsi CLI `--software` dibuang.
- **Autocomplete lokasi Seek/JobStreet:** query GraphQL `searchLocationsSuggest` di `{base_url}/graphql`
  (variabel `query`, `count`, `recentLocation`, `locale`, `country`), tanpa login, bekerja di kedua situs.
  Di UI memakai komponen `streamlit-searchbox`; lokasi yang dipilih masuk ke multiselect "Lokasi terpilih".
- **Kolom LinkedIn yang kosong:** saat pencarian LinkedIn dipecah per batch, pengambilan detail diganti
  versi sendiri yang hanya mengambil deskripsi, sehingga jenis pekerjaan hilang (bug). Sekarang
  `jobspy_sources.parse_details` juga mengambil Employment type, Seniority level, Industries, dan jenis
  lamaran (Easy Apply vs external) dari halaman tamu yang sama.
- **Kolom baru:** `seniority`, `industry`, `apply_type`. Semua parser memakai `scraper.new_row()` supaya
  kolomnya selalu lengkap. UI dan file unduhan menyembunyikan kolom yang kosong di semua baris.
- **Easy Apply vs external:** dari 16 halaman tamu yang diperiksa (8 dan 8), kolom yang tersedia sama;
  deskripsi lowongan external rata-rata lebih panjang (3.450 vs 1.300 karakter).
- **Pemakaian token nyata** per lowongan (6 sampel): sekitar 550 input, 130 output, 330 token "thinking".

## Yang belum terverifikasi

- Batas maksimum `pageSize` dan batas jumlah halaman.
- Perilaku rate limit saat request lebih sering atau volume lebih besar. Tarikan terbesar yang pernah dijalankan sekitar 20 lowongan dengan deskripsi; tarikan ratusan sampai ribuan lowongan belum pernah diuji.
- Kestabilan endpoint dan ID dalam jangka panjang (endpoint internal bisa berubah tanpa pemberitahuan).
- Jalur retry 403/429 hanya teruji lewat unit test, belum pernah terpicu terhadap situs sungguhan. Pagination multi-halaman sudah teruji live (JobStreet, 272 lowongan dalam 3 halaman).

## Batasan yang diketahui

- **Job yang sudah expired hilang dari listing**, dan lowongan hanya hidup sekitar 30 hari. Karena user memilih tanpa database, backdate tidak mungkin. Kalau kelak butuh data historis, solusinya scraper terjadwal yang mengakumulasi ke penyimpanan sendiri.
- **Deskripsi butuh 1 request per lowongan**, jadi waktu tarikan sebanding dengan jumlah lowongan (kira-kira 2,5 detik per lowongan).
- **Filter software hanya bisa dilakukan setelah deskripsi diambil**, dan hanya menemukan software yang disebut namanya. Nama yang juga kata umum (mis. "Accurate") bisa cocok dengan kata biasa.
- **Rate limit dan anti-bot:** jeda 2 sampai 5 detik dengan jitter, tanpa request paralel, header browser yang wajar, retry dengan backoff saat 403/429/5xx.

## Catatan legal

Terms of Service Seek melarang akses otomatis. User sudah diberi tahu risikonya. Jaga skala tetap kecil dan sopan (rate limit ketat, tidak membebani server). Jangan membuat fitur untuk menghindari proteksi secara agresif (rotating proxy, captcha solver, dll) kecuali user secara eksplisit memutuskan dan memahami risikonya.

## Status implementasi

Rencana awal punya empat tingkat (JSON API, JSON tertanam di HTML, Playwright, CSS selector). Tingkat pertama berhasil, jadi sisanya tidak dibutuhkan.

| File | Isi |
|---|---|
| `app.py` | UI Streamlit: form kriteria, progress, tabel hasil, filter software, download CSV/Excel |
| `ai.py` | Klien Vertex AI, prompt, dan ekstraksi requirement paralel dengan retry |
| `jobspy_sources.py` | Pencarian LinkedIn dan Indeed lewat JobSpy (per batch, dengan coba-ulang), deskripsi LinkedIn dari endpoint tamu |
| `glints.py` | Klien Apify dan pemetaan hasil actor Glints ke kolom scraper |
| `scraper.py` | Logika inti (pencarian, pagination, retry, deskripsi, pencocokan software) + CLI yang menulis CSV |
| `config.toml` | Situs, lokasi, ID subklasifikasi, default, jeda, retry |
| `tests/test_scraper.py` | Unit test parsing, pagination, duplikat promoted, retry, deskripsi, filter software |
| `README.md` | Install, cara pakai UI dan CLI |

Stack: Python 3.11+, `httpx`, `streamlit`, `pandas`, `openpyxl`, `google-genai`, `python-jobspy`.

### Kolom output

`job_id`, `title`, `company`, `location`, `search_location`, `subclassification`, `work_type`, `work_arrangement`, `salary`, `listed_at`, `software`, `description`, `requirements`, `job_url`, `scraped_at`.

### Yang sudah diuji terhadap situs sungguhan (4 Oktober 2026)

- UI, Seek: Sydney + Melbourne + Brisbane, Audit, 7 hari: 21 lowongan, semua dengan deskripsi; filter software menyaring dengan benar.
- UI, JobStreet: Jakarta Raya, Audit, 3 hari: 17 lowongan, semua dengan deskripsi.
- CLI, JobStreet dengan filter software: 3 dari 12 lowongan cocok.
- UI dengan AI, Seek: 11 lowongan, semuanya mendapat requirement. JobStreet dengan filter software: 10 dari 38 lowongan diproses AI, 28 sisanya ditawarkan lewat tombol.
- Tidak ada request yang gagal, jadi jalur retry tetap hanya teruji lewat unit test.

## Cara kerja yang diharapkan untuk perubahan berikutnya

- Verifikasi setiap asumsi dengan request nyata sebelum membangun di atasnya.
- Kalau diblok atau ragu, laporkan apa adanya dan tanyakan ke user, jangan menebak ID atau endpoint.
- Jika API Seek mulai menolak request, coba fallback `SEEK_REDUX_DATA` di HTML sebelum beralih ke Playwright. Untuk JobStreet, HTML sudah diblok, jadi alternatifnya langsung Playwright.
