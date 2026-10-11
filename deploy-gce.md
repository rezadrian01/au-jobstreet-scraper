# Tutorial: Menjalankan Job Scraper di Google Compute Engine

Panduan ini memasang aplikasi di satu VM Google Cloud, diakses langsung lewat alamat IP (tanpa domain),
dengan log audit yang tersimpan di Cloud Logging.

**Status panduan:** disusun dari dokumentasi Google Cloud dan kode aplikasi, tetapi belum pernah
dijalankan dari awal sampai akhir. Di project `intervyou-2`, Compute Engine API bahkan belum aktif saat
panduan ini ditulis (11 Oktober 2026). Jalankan langkah demi langkah dan periksa hasil tiap langkah.

## Gambaran

```
Browser  --HTTP :8501-->  VM (Ubuntu, Streamlit sebagai service)  -->  Seek / JobStreet / LinkedIn / ...
                                   |                                \-->  Vertex AI (Gemini), Apify
                                   \-- log --> journald --> Ops Agent --> Cloud Logging
```

Yang perlu diketahui sebelum mulai:

- **Tanpa domain berarti tanpa HTTPS.** Lalu lintas antara browser dan VM tidak terenkripsi, jadi akses
  dibatasi hanya untuk alamat IP tertentu lewat firewall (Langkah 4). Jangan membuka port ke seluruh internet.
- **Belum diuji apakah Seek dan JobStreet menerima permintaan dari IP Compute Engine.** Langkah 8
  mengujinya sebelum aplikasi dipakai sungguhan.
- **VM menimbulkan biaya selama menyala.** Cek perkiraannya di <https://cloud.google.com/products/calculator>.

## Prasyarat

- `gcloud` terpasang dan sudah login (`gcloud auth login`).
- Project Google Cloud dengan billing aktif.
- Kode terbaru sudah di-push ke GitHub (termasuk `audit.py`), karena VM mengambil kode dari sana.

## Langkah 1: Siapkan variabel

Jalankan di terminal laptop. Semua perintah berikutnya memakai variabel ini.

```bash
export PROJECT_ID="intervyou-2"
export REGION="asia-southeast2"          # Jakarta
export ZONE="asia-southeast2-a"
export VM_NAME="job-scraper"
export SA_NAME="job-scraper-vm"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

## Langkah 2: Aktifkan layanan yang dibutuhkan

```bash
gcloud services enable compute.googleapis.com logging.googleapis.com \
  monitoring.googleapis.com aiplatform.googleapis.com
```

Tunggu satu sampai dua menit sebelum lanjut; Compute Engine perlu waktu saat pertama kali diaktifkan.

## Langkah 3: Buat service account untuk VM

VM memakai identitas ini untuk memanggil Gemini dan menulis log, jadi private key Google Cloud tidak
perlu disimpan di server.

```bash
gcloud iam service-accounts create "$SA_NAME" --display-name="Job Scraper VM"

for ROLE in roles/aiplatform.user roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$ROLE" --condition=None
done
```

## Langkah 4: Alamat IP tetap dan firewall

Pesan alamat IP tetap supaya alamat aplikasi tidak berubah saat VM di-restart:

```bash
gcloud compute addresses create "${VM_NAME}-ip" --region="$REGION"
export VM_IP=$(gcloud compute addresses describe "${VM_NAME}-ip" --region="$REGION" --format="value(address)")
echo "Alamat aplikasi nanti: http://${VM_IP}:8501"
```

Buka port 8501 hanya untuk alamat IP yang boleh mengakses. Cari IP publik tiap pengguna dengan membuka
<https://ifconfig.me> dari jaringan masing-masing, lalu masukkan dipisah koma:

```bash
export ALLOWED_IPS="203.0.113.10/32,198.51.100.25/32"   # ganti dengan IP Anda dan rekan Anda

gcloud compute firewall-rules create "${VM_NAME}-allow-web" \
  --network=default --direction=INGRESS --action=ALLOW --rules=tcp:8501 \
  --source-ranges="$ALLOWED_IPS" --target-tags="$VM_NAME"
```

IP rumah atau seluler biasanya berubah-ubah. Kalau aplikasi tiba-tiba tidak bisa dibuka, perbarui daftarnya:

```bash
gcloud compute firewall-rules update "${VM_NAME}-allow-web" --source-ranges="IP_BARU/32,IP_LAIN/32"
```

## Langkah 5: Buat VM

```bash
gcloud compute instances create "$VM_NAME" \
  --zone="$ZONE" --machine-type=e2-small \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --address="$VM_IP" --tags="$VM_NAME" \
  --service-account="$SA_EMAIL" --scopes=cloud-platform
```

`e2-small` (2 GB RAM) cukup untuk pemakaian beberapa orang. Masuk ke VM:

```bash
gcloud compute ssh "$VM_NAME" --zone="$ZONE"
```

Langkah 6 sampai 10 dijalankan **di dalam VM**.

## Langkah 6: Pasang aplikasi

```bash
sudo apt-get update && sudo apt-get install -y python3-venv git
sudo timedatectl set-timezone Asia/Jakarta        # supaya jam di log memakai WIB

sudo useradd --system --create-home --home-dir /opt/job-scraper --shell /usr/sbin/nologin jobscraper
sudo -u jobscraper git clone https://github.com/rezadrian01/au-jobstreet-scraper.git /opt/job-scraper/app
sudo -u jobscraper python3 -m venv /opt/job-scraper/venv
sudo -u jobscraper /opt/job-scraper/venv/bin/pip install -r /opt/job-scraper/app/requirements.txt
```

Jika repository GitHub-nya privat, `git clone` akan meminta login. Buat personal access token di GitHub
(izin baca repository) dan pakai sebagai password.

## Langkah 7: Isi file `.env`

```bash
sudo -u jobscraper tee /opt/job-scraper/app/.env > /dev/null <<'EOF'
GCP_PROJECT_ID="intervyou-2"
GCP_LOCATION="global"
GCP_CLIENT_EMAIL=""
GCP_PRIVATE_KEY=""
APIFY_TOKEN="ISI_TOKEN_APIFY_ANDA"
EOF
sudo chmod 600 /opt/job-scraper/app/.env
```

`GCP_CLIENT_EMAIL` sengaja dikosongkan: aplikasi otomatis memakai service account VM dari Langkah 3.
`APIFY_TOKEN` hanya perlu jika sumber Glints dipakai.

## Langkah 8: Uji dulu dari VM

Pastikan situs sumber menerima permintaan dari IP VM sebelum memasang service:

```bash
cd /opt/job-scraper/app
sudo -u jobscraper /opt/job-scraper/venv/bin/python scraper.py \
  --site jobstreet_id --daterange 3 --csv /tmp/uji.csv
```

Hasil yang diharapkan: baris `Selesai: N job ditulis ...` dengan `0 lokasi gagal` dan `0 deskripsi gagal`.
Ulangi dengan `--site seek_au`. Kalau muncul `GAGAL (HTTP 403)`, situsnya menolak IP VM dan aplikasi tidak
akan berguna dari server ini; hentikan di sini dan pertimbangkan region lain.

Uji juga akses ke Gemini:

```bash
sudo -u jobscraper /opt/job-scraper/venv/bin/python scraper.py \
  --site jobstreet_id --daterange 3 --requirements --csv /tmp/uji-ai.csv
```

Baris terakhir harus menyebut `0 requirement gagal`.

## Langkah 9: Jalankan sebagai service

```bash
sudo tee /etc/systemd/system/job-scraper.service > /dev/null <<'EOF'
[Unit]
Description=Job Scraper (Streamlit)
After=network-online.target
Wants=network-online.target

[Service]
User=jobscraper
WorkingDirectory=/opt/job-scraper/app
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/job-scraper/venv/bin/streamlit run app.py \
  --server.port 8501 --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now job-scraper
sudo systemctl status job-scraper --no-pager
```

Statusnya harus `active (running)`. Sekarang buka `http://ALAMAT_IP:8501` dari komputer yang IP-nya
diizinkan di Langkah 4.

## Langkah 10: Kirim log ke Cloud Logging

Pasang Ops Agent:

```bash
cd /tmp
curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
sudo bash add-google-cloud-ops-agent-repo.sh --also-install
```

Secara default agen hanya membaca file syslog. Tambahkan pembacaan journald, tempat log service tersimpan:

```bash
sudo tee /etc/google-cloud-ops-agent/config.yaml > /dev/null <<'EOF'
logging:
  receivers:
    journald:
      type: systemd_journald
  service:
    pipelines:
      journald_pipeline:
        receivers: [journald]
EOF

sudo systemctl restart google-cloud-ops-agent
sudo systemctl status "google-cloud-ops-agent*" --no-pager
```

Komponen "Logging Agent" harus berstatus running.

## Isi log audit

Aplikasi mencatat satu baris per kejadian, dengan format:

```
2026-10-11T21:45:25+0700 INFO jobscraper.audit pull_started {"session": "a1b2c3d4", "ip": "203.0.113.10", "site": "jobstreet_id", ...}
```

| Kejadian | Kapan dicatat | Isi utama |
|---|---|---|
| `pull_started` | Tombol "Tarik data" diklik | Sumber, lokasi, subklasifikasi atau keyword, rentang hari, opsi yang aktif |
| `search_finished` | Pencarian selesai | Jumlah lowongan, pencarian yang gagal, peringatan, lama proses |
| `descriptions_started` / `descriptions_finished` | Tahap deskripsi | Jumlah diminta, jumlah gagal, yang masih kosong, lama proses |
| `ai_started` / `ai_finished` | Tahap analisis AI | Jumlah diminta, jumlah gagal, pesan error, model, lama proses |
| `pull_finished` | Seluruh tarikan selesai | Total lowongan, yang punya deskripsi, yang dianalisis, lama proses |
| `pull_interrupted` | Tarikan terputus di tengah jalan | Penyebab, jumlah yang sudah terambil saat terputus |
| `download` | Tombol download diklik | Format, jumlah baris, berapa yang belum punya deskripsi atau belum dianalisis |

Setiap baris memuat `pull_id` (penanda satu tarikan), `session` (penanda satu tab browser), dan `ip`
pengakses. Selain itu, tiap permintaan yang gagal ke situs sumber dicatat sebagai baris `WARNING` oleh
logger `seek` beserta alasannya.

Contoh membaca kejadian: kalau ada `pull_started` dan `descriptions_started` tanpa `pull_finished`, lalu
`pull_interrupted` atau `download` dengan `missing_description` besar, berarti pengguna mengunduh data dari
tarikan yang belum selesai.

## Melihat log

Di VM, langsung dan waktu nyata:

```bash
sudo journalctl -u job-scraper -f                              # semua log aplikasi
sudo journalctl -u job-scraper --since today | grep jobscraper.audit
sudo journalctl -u job-scraper --since "2026-10-11 19:30" --until "2026-10-11 20:30"
```

Di Cloud Logging, dari laptop:

```bash
gcloud logging read 'resource.type="gce_instance" AND "jobscraper.audit"' \
  --freshness=7d --limit=100 --format="value(timestamp,jsonPayload.MESSAGE,textPayload)"

# hanya tarikan yang terputus
gcloud logging read 'resource.type="gce_instance" AND "pull_interrupted"' --freshness=30d

# semua kejadian dari satu tarikan
gcloud logging read 'resource.type="gce_instance" AND "abc12345"' --freshness=30d
```

Atau lewat konsol: <https://console.cloud.google.com/logs/query>, lalu isi kolom query dengan
`resource.type="gce_instance" "jobscraper.audit"`.

Log baru muncul di Cloud Logging satu sampai dua menit setelah kejadian. Nama kolom persis di Cloud Logging
(`jsonPayload.MESSAGE` atau `textPayload`) belum saya verifikasi; pencarian dengan teks seperti di atas
bekerja di kolom mana pun.

### Masa simpan log

Cloud Logging menyimpan log 30 hari secara default. Untuk kebutuhan audit yang lebih panjang:

```bash
gcloud logging buckets update _Default --location=global --retention-days=365
```

Penyimpanan di atas 30 hari dikenai biaya tambahan.

## Memperbarui aplikasi

Setelah ada perubahan kode di GitHub, jalankan di VM:

```bash
cd /opt/job-scraper/app
sudo -u jobscraper git pull
sudo -u jobscraper /opt/job-scraper/venv/bin/pip install -r requirements.txt
sudo systemctl restart job-scraper
```

Restart memutus tarikan yang sedang berjalan, jadi lakukan saat tidak ada yang memakai.

## Alternatif akses yang lebih aman: tanpa membuka port

Kalau tidak ingin membuka port sama sekali, lewati aturan firewall di Langkah 4 dan akses lewat tunnel SSH.
Setiap pengguna butuh `gcloud` dan izin ke project:

```bash
gcloud compute ssh job-scraper --zone=asia-southeast2-a -- -N -L 8501:localhost:8501
```

Selama perintah itu berjalan, buka `http://localhost:8501`. Lalu lintasnya terenkripsi dan Google mencatat
siapa yang masuk, tetapi cara ini kurang praktis untuk pengguna non-teknis.

## Jika ada masalah

| Gejala | Yang diperiksa |
|---|---|
| Halaman tidak bisa dibuka | IP Anda berubah (perbarui firewall, Langkah 4); service mati (`systemctl status job-scraper`) |
| Service terus restart | `sudo journalctl -u job-scraper -n 50` untuk melihat error-nya |
| AI gagal dengan "Akses Vertex AI ditolak" | Role `aiplatform.user` di Langkah 3; VM dibuat dengan `--scopes=cloud-platform` |
| Semua tarikan `GAGAL (HTTP 403)` | Situs menolak IP VM; lihat Langkah 8 |
| Log tidak muncul di Cloud Logging | Status Ops Agent (Langkah 10); role `logging.logWriter` di Langkah 3 |

## Menghentikan atau menghapus

```bash
gcloud compute instances stop job-scraper --zone=asia-southeast2-a      # berhenti sementara
gcloud compute instances delete job-scraper --zone=asia-southeast2-a    # hapus VM
gcloud compute addresses delete job-scraper-ip --region=asia-southeast2 # lepas IP tetap
gcloud compute firewall-rules delete job-scraper-allow-web
```

VM yang dihentikan tidak dikenai biaya komputasi, tetapi disk dan IP tetap yang dipesan masih ditagih.
