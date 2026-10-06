# Job Scraper: Seek Australia, JobStreet Indonesia, LinkedIn, Indeed & Glints

Mengambil lowongan **Accounting** dari **au.seek.com**, **id.jobstreet.com**, **LinkedIn**, **Indeed**, dan
**Glints** (tiga terakhir untuk Indonesia, lewat pencarian keyword) dengan UI web lokal, dengan
kriteria yang bisa diganti-ganti: situs, lokasi, subklasifikasi atau keyword, dan rentang hari.
Poin-poin requirement dan software yang disebut tiap lowongan diekstrak dari deskripsinya oleh Gemini (Vertex AI).

Sumber data adalah JSON API yang dipakai frontend kedua situs (pencarian lewat `/api/jobsearch/v5/search`,
deskripsi lewat `/graphql`), bukan parsing HTML. Tidak ada database: setiap tarikan mengambil data terbaru
dari situs.

## Install

Butuh Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Setelah memperbarui dependensi, hentikan (Ctrl+C) dan jalankan ulang Streamlit; proses yang masih berjalan
memakai versi library lama.

### Kredensial Google Cloud (untuk ekstraksi requirement dengan AI)

Salin `.env.example` menjadi `.env`, lalu isi:

| Variabel             | Isi                                                                              |
| -------------------- | -------------------------------------------------------------------------------- |
| `GCP_PROJECT_ID`   | Project yang Vertex AI API-nya aktif; panggilan Gemini ditagih ke sini           |
| `GCP_LOCATION`     | Region Vertex, mis.`global`                                                    |
| `GCP_CLIENT_EMAIL` | `client_email` dari file JSON key service account                              |
| `GCP_PRIVATE_KEY`  | `private_key` dari file yang sama, satu baris dengan `\n` di antara barisnya |

Service account-nya butuh role **Vertex AI User**. Jika `GCP_CLIENT_EMAIL` dikosongkan, aplikasi memakai
login gcloud (`gcloud auth application-default login`, atau `gcloud auth login` yang sudah ada).

Untuk sumber **Glints**, isi juga `APIFY_TOKEN` (buat di
<https://console.apify.com/settings/integrations>). Glints diambil lewat layanan berbayar Apify.

`.env` berisi private key dan token, dan sudah masuk `.gitignore`; jangan di-commit. Model yang dipakai diatur di
bagian `[ai]` pada `config.toml`.

## Menjalankan UI

```bash
.venv/bin/streamlit run app.py
```

Browser akan terbuka di `http://localhost:8501`.

1. Di panel kiri, atur semua kriteria: **sumber data**, **lokasi**, **subklasifikasi** atau **keyword**,
   dan **rentang hari**.
   - **Cari lokasi** (Seek dan JobStreet): ketik sebagian nama kota, provinsi, atau kecamatan; saran muncul
     sambil mengetik, sama seperti di situsnya. Pilih salah satu untuk menambahkannya ke **Lokasi terpilih**.
   - Lokasi dikosongkan = semua lokasi. Lokasi lain bisa diketik langsung.
   - Lokasi terpilih awalnya kosong di semua sumber, yang berarti seluruh negara.
   - Subklasifikasi dikosongkan = semua subklasifikasi Accounting.
   - **LinkedIn, Indeed, dan Glints** tidak punya subklasifikasi; sebagai gantinya isi **Keyword**. Bisa lebih dari satu
     (pilih dari saran atau ketik sendiri lalu Enter); tiap keyword dicari terpisah dan hasilnya digabung.
   - **Keyword harus ada di judul** (LinkedIn, Indeed, dan Glints; aktif secara default): hanya menampilkan lowongan yang
     judulnya memuat salah satu keyword. Bisa dimatikan tanpa menarik ulang data.
   - **Analisis AI: requirement dan software**: Gemini membaca deskripsi lalu mengisi kolom **Requirements**
     dan **Software** (software yang disebut di lowongan, bisa lebih dari satu). Hilangkan centang kalau
     tidak perlu (lebih cepat dan tanpa biaya); kedua kolom itu akan kosong.
2. Klik **Tarik data**.
3. **Download CSV** atau **Download Excel** berisi baris yang sedang ditampilkan.

## Data yang diambil

| Kolom                                                        | Isi                                                                                                          |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `title`                                                    | Judul lowongan                                                                                               |
| `description`                                              | Isi lengkap lowongan (deskripsi dan requirement), teks polos                                                 |
| `requirements`                                             | Poin-poin requirement hasil ekstraksi AI, satu poin per baris; yang bersifat opsional diawali`(Desirable)` |
| `subclassification`                                        | Mis. Audit - External                                                                                        |
| `company`                                                  | Nama perusahaan / pengiklan                                                                                  |
| `location`                                                 | Lokasi versi situs, mis. "Macquarie Park, Sydney NSW"                                                        |
| `salary`                                                   | Kosong jika pengiklan tidak mencantumkan                                                                     |
| `listed_at`                                                | Tanggal posting. Di UI dan hasil download: waktu lokal situs; di CLI: ISO 8601 UTC                           |
| `software` | Software yang disebut di lowongan, diekstrak AI; bisa lebih dari satu, dipisah koma |
| `seniority`, `industry`, `apply_type` | Tingkat senioritas, industri, dan cara melamar (terutama dari LinkedIn) |
| `work_type`, `work_arrangement`                          | Full time / contract, on-site / hybrid / remote                                                              |
| `job_id`, `search_location`, `job_url`, `scraped_at` | Pelengkap                                                                                                    |

## Hal yang perlu diketahui

- **Tidak bisa backdate.** Kedua situs hanya menampilkan lowongan aktif, sekitar 30 hari ke belakang.
  Karena tidak ada database, lowongan yang sudah hilang dari situs tidak bisa diambil lagi.
- **Deskripsi butuh 1 request per lowongan** dengan jeda 1,5-3 detik. Kira-kira 2,5 menit per 60 lowongan.
  Kriteria yang lebar (mis. semua Accounting di Jakarta, hampir 2.000 lowongan) bisa lebih dari satu jam.
  Hilangkan centang **Ambil deskripsi lowongan** untuk melihat jumlahnya dulu, lalu ambil deskripsinya dengan
  tombol yang muncul di halaman hasil.
- **Tidak ada filter software.** Kolom Software diisi AI dari isi lowongan. Hanya software yang disebut
  namanya yang tercatat ("accounting software" tanpa nama tidak dihitung), dan AI menulis nama produk
  resminya (mis. "Ms. Excel" menjadi "Microsoft Excel").
- **Requirement dan software diekstrak oleh AI, jadi bisa keliru.** Model diminta mengambil apa adanya dari
  deskripsi tanpa mengarang, tetapi sesekali ada yang terlewat atau salah masuk. Cek ke kolom deskripsi
  kalau ragu.
- **AI berbayar.** Setiap lowongan = 1 panggilan Gemini (requirement dan software sekaligus), ditagih ke
  project Google Cloud di `.env`. Hanya lowongan yang ditampilkan yang diproses.
- **Kolom pelengkap yang kosong disembunyikan.** Kategori, jenis pekerjaan, pola kerja, senioritas, industri,
  dan cara melamar hanya disediakan sebagian sumber; kalau kosong di semua baris, kolom itu tidak
  ditampilkan dan tidak ikut di file unduhan. Kolom utama (termasuk Software dan Salary) selalu tampil,
  walaupun kosong.
- **LinkedIn berbasis keyword, jadi hasilnya lebih berisik.** Keyword dicocokkan LinkedIn ke seluruh isi
  lowongan, sehingga pencarian "audit" bisa ikut memunculkan lowongan non-audit yang hanya menyebut kata itu.
  Opsi "Keyword harus ada di judul" menyaring ini, dengan risiko membuang lowongan relevan yang judulnya
  tidak memuat keyword.
  Tanggal posting LinkedIn hanya sampai tingkat hari, gaji hampir tidak pernah ada, dan lowongan yang sama
  di JobStreet tidak otomatis dianggap duplikat.
- **LinkedIn lebih mudah memblokir.** Pencarian diambil lewat library
  [JobSpy](https://github.com/speedyapply/JobSpy) tanpa login, 25 hasil per batch, maksimal 300 hasil per
  keyword (`max_results` di `config.toml`). Deskripsi hanya diambil untuk lowongan yang lolos filter judul.
- **Indeed** diambil lewat JobSpy juga, 50 hasil per batch, dan hasil pencarian sudah termasuk deskripsi.
  Sama berisiknya dengan LinkedIn, jadi filter judul aktif secara default. Tanggal posting hanya tingkat hari.
- **Koneksi yang ter-reset dicoba ulang otomatis.** Jika satu batch tetap gagal setelah beberapa percobaan,
  hasil yang sudah terambil tetap ditampilkan bersama peringatan.
- **Glints berbayar dan filter tanggalnya longgar.** Glints diambil lewat actor Apify
  [TrueFetch Glints Job Listing](https://apify.com/truefetch/glints-job-listing): $0,005 per lowongan +
  $0,01 per pencarian, ditagih ke akun Apify Anda (paket gratis: $5 per bulan). Filter tanggal Glints
  memakai waktu update, jadi lowongan lama yang diperbarui ikut terbawa; aplikasi membuangnya dan
  melaporkan jumlahnya, tetapi lowongan itu tetap ikut ditagih. Atur **Maks. hasil per keyword** untuk
  mengendalikan biaya.
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
.venv/bin/python scraper.py --site seek_au --daterange 7 --requirements --csv data/jobs.csv
.venv/bin/python scraper.py --site jobstreet_id --location "Jakarta Raya" --subclass "Taxation" --no-description
```

`--subclass all` = semua subklasifikasi Accounting. Lihat `--help` untuk semua opsi.

## Test

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```
