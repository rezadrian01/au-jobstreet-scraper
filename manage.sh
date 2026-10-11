#!/usr/bin/env bash
# Kelola deployment Job Scraper di Google Compute Engine. Lihat deploy-gce.md untuk pemasangan awal.
#
# Pemakaian: ./manage.sh <perintah> [argumen]     (./manage.sh help untuk daftar perintah)
# Nilai bawaan bisa diganti lewat variabel environment, mis. VM_NAME=lain ./manage.sh status

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-intervyou-2}"
ZONE="${ZONE:-asia-southeast2-a}"
VM_NAME="${VM_NAME:-job-scraper}"
FIREWALL_RULE="${FIREWALL_RULE:-${VM_NAME}-allow-web}"
SERVICE="${SERVICE:-job-scraper}"
APP_DIR="${APP_DIR:-/opt/job-scraper/app}"
VENV_DIR="${VENV_DIR:-/opt/job-scraper/venv}"
APP_USER="${APP_USER:-jobscraper}"
PORT="${PORT:-8501}"

GCLOUD=(gcloud --project "$PROJECT_ID")
SSH_OPTS=(-o ConnectTimeout=20 -o ServerAliveInterval=15)

die() { echo "Error: $*" >&2; exit 1; }

# Jalankan perintah di VM. Koneksi SSH dicoba ulang beberapa kali karena jaringan sering memutusnya.
vm_run() {
  local attempt rc err
  err=$(mktemp)
  for attempt in 1 2 3 4 5; do
    rc=0
    timeout "${VM_TIMEOUT:-180}" "${GCLOUD[@]}" compute ssh "$VM_NAME" --zone "$ZONE" --quiet \
      --command "$1" -- "${SSH_OPTS[@]}" 2>"$err" || rc=$?
    if [ "$rc" -eq 0 ]; then rm -f "$err"; return 0; fi
    # 255 = SSH gagal tersambung, 124 = batas waktu habis; selain itu perintah di VM yang gagal, jangan diulang
    if [ "$rc" -ne 255 ] && [ "$rc" -ne 124 ]; then cat "$err" >&2; rm -f "$err"; return "$rc"; fi
    sleep 4
  done
  cat "$err" >&2; rm -f "$err"
  die "tidak bisa tersambung ke VM setelah 5 percobaan"
}

# Sesi interaktif (tanpa batas waktu dan tanpa percobaan ulang).
vm_interactive() {
  "${GCLOUD[@]}" compute ssh "$VM_NAME" --zone "$ZONE" --quiet ${1:+--command "$1"} -- "${SSH_OPTS[@]}" -t
}

vm_status() { "${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" --format="value(status)"; }
vm_ip() { "${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" --format="value(networkInterfaces[0].accessConfigs[0].natIP)"; }
firewall_ranges() { "${GCLOUD[@]}" compute firewall-rules describe "$FIREWALL_RULE" --format="value(sourceRanges.list())"; }

confirm() {
  [ "${ASSUME_YES:-}" = "1" ] && return 0
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^[yY]$ ]] || die "dibatalkan"
}

cmd_help() {
  cat <<EOF
Pemakaian: ./manage.sh <perintah> [argumen]

Informasi
  status                 Ringkasan: VM, alamat, service, versi kode, firewall, tarikan terakhir
  url                    Cetak alamat aplikasi
  logs [N]               N baris terakhir log aplikasi (default 100)
  logs -f                Ikuti log aplikasi secara langsung (Ctrl+C untuk berhenti)
  audit [SEJAK]          Kejadian audit sejak waktu tertentu (default "today"; mis. "-2h", "2026-10-11")
  audit-cloud [RENTANG]  Kejadian audit dari Cloud Logging (default 7d; mis. 30d, 12h)
  interrupted [RENTANG]  Tarikan yang terputus, dari Cloud Logging (default 30d)
  pull ID                Semua kejadian dari satu tarikan berdasarkan pull_id

Aplikasi
  update                 Ambil kode terbaru dari GitHub, pasang dependensi, restart
  restart                Restart aplikasi (memutus tarikan yang sedang berjalan)
  test                   Uji dari VM: JobStreet, Seek, saran lokasi
  set-env NAMA           Ubah satu nilai di .env server (nilai diketik tersembunyi), lalu restart
  ssh                    Masuk ke VM

VM
  start | stop           Nyalakan atau matikan VM (saat mati, aplikasi tidak bisa diakses)

Akses
  firewall               Tampilkan alamat IP yang boleh mengakses
  firewall open          Buka untuk semua alamat IP
  firewall allow IP,...  Hanya izinkan alamat IP tertentu (mis. 203.0.113.10,198.51.100.25)
  firewall allow-me      Hanya izinkan alamat IP publik komputer ini

Tambahkan ASSUME_YES=1 di depan perintah untuk melewati konfirmasi.
EOF
}

cmd_url() { echo "http://$(vm_ip):${PORT}"; }

cmd_status() {
  local status ip
  status=$(vm_status)
  ip=$(vm_ip)
  echo "VM            : $VM_NAME ($ZONE) - $status"
  echo "Alamat        : http://${ip}:${PORT}"
  echo "Firewall      : $(firewall_ranges)"
  if [ "$status" != "RUNNING" ]; then
    echo "VM tidak menyala; jalankan ./manage.sh start"
    return 0
  fi
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://${ip}:${PORT}/_stcore/health" || true)
  echo "Dari sini     : HTTP ${code:-000} $([ "$code" = "200" ] && echo "(bisa diakses)" || echo "(TIDAK bisa diakses dari komputer ini)")"
  vm_run "
    echo \"Service       : \$(systemctl is-active $SERVICE), sejak \$(systemctl show $SERVICE -p ActiveEnterTimestamp --value)\"
    echo \"Agen log      : \$(systemctl is-active google-cloud-ops-agent)\"
    echo \"Versi kode    : \$(sudo -u $APP_USER git -C $APP_DIR log -1 --format='%h %s (%cd)' --date=format:'%d %b %H:%M')\"
    echo \"Disk          : \$(df -h / | awk 'NR==2 {print \$3 \" terpakai dari \" \$2}')\"
    echo \"Memori        : \$(free -m | awk 'NR==2 {print \$3 \" MB terpakai dari \" \$2 \" MB\"}')\"
    last=\$(sudo journalctl -u $SERVICE --since '-7d' --no-pager -o cat | grep 'jobscraper.audit' | tail -1 | cut -c1-150)
    echo \"Audit terakhir: \${last:-(belum ada dalam 7 hari)}\"
  "
}

cmd_logs() {
  if [ "${1:-}" = "-f" ]; then
    vm_interactive "sudo journalctl -u $SERVICE -f -o cat"
  else
    vm_run "sudo journalctl -u $SERVICE -n ${1:-100} --no-pager -o cat"
  fi
}

cmd_audit() {
  vm_run "sudo journalctl -u $SERVICE --since '${1:-today}' --no-pager -o cat | grep 'jobscraper.audit' || echo '(tidak ada kejadian audit pada rentang ini)'"
}

cloud_log() {
  local filter="logName=\"projects/${PROJECT_ID}/logs/journald\" AND jsonPayload._SYSTEMD_UNIT=\"${SERVICE}.service\" AND \"$1\""
  "${GCLOUD[@]}" logging read "$filter" --freshness="$2" --limit="${LIMIT:-500}" --order=asc --format="value(jsonPayload.MESSAGE)"
}

cmd_audit_cloud() { cloud_log "jobscraper.audit" "${1:-7d}"; }
cmd_interrupted() { cloud_log "pull_interrupted" "${1:-30d}"; }
cmd_pull() { [ -n "${1:-}" ] || die "sebutkan pull_id, mis. ./manage.sh pull b2711793"; cloud_log "$1" "${2:-30d}"; }

cmd_restart() {
  confirm "Restart memutus tarikan yang sedang berjalan. Lanjut?"
  vm_run "sudo systemctl restart $SERVICE && sleep 5 && echo \"Service: \$(systemctl is-active $SERVICE)\""
}

cmd_update() {
  confirm "Update akan me-restart aplikasi dan memutus tarikan yang sedang berjalan. Lanjut?"
  VM_TIMEOUT=600 vm_run "
    set -e
    before=\$(sudo -u $APP_USER git -C $APP_DIR rev-parse --short HEAD)
    sudo -u $APP_USER git -C $APP_DIR pull --ff-only
    after=\$(sudo -u $APP_USER git -C $APP_DIR rev-parse --short HEAD)
    if [ \"\$before\" = \"\$after\" ]; then echo \"Kode sudah versi terbaru (\$after); tidak ada yang diubah.\"; exit 0; fi
    sudo -u $APP_USER $VENV_DIR/bin/pip install -q -r $APP_DIR/requirements.txt
    sudo systemctl restart $SERVICE
    sleep 6
    echo \"Diperbarui: \$before -> \$after. Service: \$(systemctl is-active $SERVICE)\"
  "
}

cmd_test() {
  VM_TIMEOUT=600 vm_run "sudo -u $APP_USER bash -c '
    cd $APP_DIR
    echo \"== JobStreet (3 hari, tanpa deskripsi)\"
    $VENV_DIR/bin/python scraper.py --site jobstreet_id --daterange 3 --no-description --csv /tmp/manage-test-id.csv 2>&1 | tail -2
    echo \"== Seek Australia (3 hari, tanpa deskripsi)\"
    $VENV_DIR/bin/python scraper.py --site seek_au --daterange 3 --no-description --csv /tmp/manage-test-au.csv 2>&1 | tail -2
    echo \"== Saran lokasi (\\\"sura\\\")\"
    $VENV_DIR/bin/python -c \"import scraper; c = scraper.load_config(); print(scraper.suggest_locations(c[\\\"sites\\\"][\\\"jobstreet_id\\\"], c[\\\"http\\\"], \\\"sura\\\")[:3])\"
    rm -f /tmp/manage-test-*.csv
  '"
}

cmd_set_env() {
  local name="${1:-}" value
  [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || die "nama variabel tidak valid; contoh: ./manage.sh set-env APIFY_TOKEN"
  read -r -s -p "Nilai baru untuk $name (tidak ditampilkan): " value
  echo
  [ -n "$value" ] || die "nilai kosong; dibatalkan"
  # Disimpan dalam kutip tunggal supaya dibaca apa adanya oleh aplikasi
  [[ "$value" != *"'"* ]] || die "nilai tidak boleh mengandung tanda kutip tunggal (')"
  confirm "Simpan $name di server dan restart aplikasi?"
  # Nilai dikirim lewat stdin, bukan argumen, supaya tidak muncul di daftar proses atau riwayat shell
  printf '%s' "$value" | timeout 120 "${GCLOUD[@]}" compute ssh "$VM_NAME" --zone "$ZONE" --quiet --command "
    set -e
    value=\$(cat)
    tmp=\$(sudo -u $APP_USER mktemp)
    sudo -u $APP_USER bash -c \"grep -v '^${name}=' $APP_DIR/.env > \$tmp || true\"
    printf \"%s='%s'\\n\" '$name' \"\$value\" | sudo -u $APP_USER tee -a \$tmp > /dev/null
    sudo install -o $APP_USER -g $APP_USER -m 600 \$tmp $APP_DIR/.env
    sudo rm -f \$tmp
    sudo systemctl restart $SERVICE
    echo \"$name diperbarui; service: \$(sleep 5; systemctl is-active $SERVICE)\"
  " -- "${SSH_OPTS[@]}"
}

cmd_start() {
  "${GCLOUD[@]}" compute instances start "$VM_NAME" --zone "$ZONE" --quiet
  echo "VM menyala. Aplikasi siap dalam sekitar satu menit di $(cmd_url)"
}

cmd_stop() {
  confirm "Mematikan VM membuat aplikasi tidak bisa diakses. Lanjut?"
  "${GCLOUD[@]}" compute instances stop "$VM_NAME" --zone "$ZONE" --quiet
  echo "VM dimatikan. Disk dan alamat IP tetap masih ditagih."
}

cmd_firewall() {
  case "${1:-show}" in
    show) echo "Port $PORT terbuka untuk: $(firewall_ranges)" ;;
    open)
      confirm "Membuka aplikasi untuk SEMUA alamat IP. Lanjut?"
      "${GCLOUD[@]}" compute firewall-rules update "$FIREWALL_RULE" --source-ranges="0.0.0.0/0" --quiet
      cmd_firewall show ;;
    allow)
      [ -n "${2:-}" ] || die "sebutkan alamat IP dipisah koma, mis. ./manage.sh firewall allow 203.0.113.10,198.51.100.25"
      local ranges="" ip
      for ip in ${2//,/ }; do
        [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$ ]] || die "alamat IP tidak valid: $ip"
        [[ "$ip" == */* ]] || ip="$ip/32"
        ranges="${ranges:+$ranges,}$ip"
      done
      "${GCLOUD[@]}" compute firewall-rules update "$FIREWALL_RULE" --source-ranges="$ranges" --quiet
      cmd_firewall show ;;
    allow-me)
      local me
      me=$(curl -4 -s --max-time 10 https://ifconfig.me || curl -4 -s --max-time 10 https://api.ipify.org) || true
      [[ "$me" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "tidak bisa mengetahui alamat IP publik komputer ini"
      echo "Alamat IP publik komputer ini: $me"
      cmd_firewall allow "$me" ;;
    *) die "pilihan firewall tidak dikenal: $1 (show | open | allow IP,... | allow-me)" ;;
  esac
}

main() {
  local command="${1:-help}"
  shift || true
  case "$command" in
    help | -h | --help) cmd_help ;;
    status) cmd_status ;;
    url) cmd_url ;;
    logs) cmd_logs "$@" ;;
    audit) cmd_audit "$@" ;;
    audit-cloud) cmd_audit_cloud "$@" ;;
    interrupted) cmd_interrupted "$@" ;;
    pull) cmd_pull "$@" ;;
    restart) cmd_restart ;;
    update) cmd_update ;;
    test) cmd_test ;;
    set-env) cmd_set_env "$@" ;;
    ssh) vm_interactive "" ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    firewall) cmd_firewall "$@" ;;
    *) echo "Perintah tidak dikenal: $command" >&2; echo; cmd_help; exit 1 ;;
  esac
}

main "$@"
