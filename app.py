"""UI web lokal untuk scraper. Jalankan: streamlit run app.py"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

import ai
import scraper

st.set_page_config(page_title="Job Scraper", page_icon=":mag:", layout="wide")

cfg = scraper.load_config()
sites = cfg["sites"]
sub_map = cfg["subclassifications"]
class_name = cfg["search"]["classification_name"]

TABLE_COLUMNS = [
    "title", "company", "location", "subclassification", "salary", "listed_at", "software",
    "work_type", "work_arrangement", "requirements", "description", "job_url",
]


def fmt_minutes(seconds: float) -> str:
    minutes = seconds / 60
    return "kurang dari 1 menit" if minutes < 1 else f"±{minutes:.0f} menit"


def fetch_descriptions(site_key: str, rows: list[dict]) -> None:
    """Ambil deskripsi untuk baris yang belum punya; rows diubah di tempat supaya hasil
    parsial tetap tersimpan kalau proses dihentikan."""
    pending = sum(r["description"] is None for r in rows)
    if not pending:
        return
    client = scraper.SeekClient(cfg, site_key)
    bar = st.progress(0.0)

    def on_progress(i: int, n: int, label: str) -> None:
        remaining = fmt_minutes(scraper.estimate_detail_seconds(cfg, n - i))
        bar.progress(i / n if n else 1.0, text=f"Deskripsi {i}/{n} · sisa {remaining} · {label[:60]}")

    try:
        failed = scraper.add_descriptions(client, rows, on_progress)
    finally:
        client.close()
    bar.empty()
    st.session_state.detail_failed = failed


def fetch_requirements(rows: list[dict]) -> None:
    """Ekstrak requirement dengan Gemini untuk baris yang punya deskripsi tetapi belum diproses."""
    if not ai.pending_rows(rows):
        return
    bar = st.progress(0.0, text="Menghubungi Gemini…")

    def on_progress(i: int, n: int, label: str) -> None:
        bar.progress(i / n if n else 1.0, text=f"Requirement {i}/{n} · {label[:60]}")

    try:
        failed, error = ai.add_requirements(ai.make_client(), cfg["ai"], rows, on_progress)
    except ai.AIError as e:
        failed, error = len(ai.pending_rows(rows)), str(e)
    bar.empty()
    st.session_state.ai_error = f"{failed} requirement gagal diekstrak: {error}" if failed else None


def run_search(
    site_key: str,
    locations: list[str],
    sub_names: list[str],
    daterange: int,
    with_description: bool,
    with_requirements: bool,
    terms: list[str],
) -> None:
    sub_ids = [sub_map[n] for n in sub_names]
    client = scraper.SeekClient(cfg, site_key)
    bar = st.progress(0.0, text="Mencari lowongan…")

    def on_progress(i: int, n: int, label: str) -> None:
        bar.progress(i / n if n else 1.0, text=f"Mencari lowongan: {label}" if label else "Pencarian selesai")

    try:
        result = scraper.collect_jobs(client, locations, sub_ids, daterange, on_progress)
    finally:
        client.close()
    bar.empty()
    st.session_state.search = {
        "site_key": site_key,
        "rows": result.rows,
        "messages": result.messages,
        "warnings": result.warnings,
        "fetched_at": datetime.now(),
    }
    st.session_state.detail_failed = 0
    st.session_state.ai_error = None
    if with_description:
        fetch_descriptions(site_key, result.rows)
        if with_requirements:
            # Hanya lowongan yang lolos filter software, supaya tidak membayar untuk yang tidak ditampilkan
            fetch_requirements(scraper.apply_software(result.rows, terms))


def to_frame(rows: list[dict], tz: str) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=scraper.FIELDS)
    listed = pd.to_datetime(df["listed_at"], utc=True, errors="coerce").dt.tz_convert(tz)
    df["listed_at"] = listed.dt.strftime("%Y-%m-%d %H:%M")
    return df


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="jobs", engine="openpyxl")
    return buf.getvalue()


# ---------- Sidebar: kriteria ----------

with st.sidebar:
    st.header("Kriteria")
    site_key = st.selectbox("Sumber data", list(sites), format_func=lambda k: sites[k]["name"])
    site = sites[site_key]
    locations = st.multiselect(
        "Lokasi",
        site["locations"],
        default=site["default_locations"],
        key=f"locations_{site_key}",
        accept_new_options=True,
        placeholder=site["all_locations_label"],
        help="Kosongkan untuk semua lokasi. Bisa mengetik lokasi lain yang tidak ada di daftar.",
    )
    sub_names = st.multiselect(
        f"Subklasifikasi ({class_name})",
        list(sub_map),
        default=cfg["defaults"]["subclassifications"],
        help=f"Kosongkan untuk semua subklasifikasi {class_name}.",
    )
    daterange = st.slider(
        "Diposting dalam … hari terakhir",
        min_value=1,
        max_value=31,
        value=cfg["search"]["default_daterange"],
        help="Lowongan hanya tampil sekitar 30 hari di situs, jadi tidak bisa mundur lebih jauh.",
    )
    software_raw = st.text_input(
        "Software (opsional)",
        placeholder="mis. Xero, MYOB, SAP",
        help="Pisahkan dengan koma. Hanya lowongan yang menyebut salah satu software ini yang ditampilkan. "
        "Kosongkan untuk menampilkan semua. Bisa diubah tanpa menarik ulang data.",
    )
    with_description = st.checkbox(
        "Ambil deskripsi lowongan",
        value=True,
        help="Wajib untuk filter software. Butuh 1 request per lowongan, jadi lebih lama.",
    )
    with_requirements = st.checkbox(
        "Ekstrak requirement dengan AI",
        value=True,
        disabled=not with_description,
        help=f"Gemini ({cfg['ai']['model']}) membaca deskripsi dan mengisi kolom Requirements dengan poin-poin "
        "requirement. Berbayar, ditagih ke project Google Cloud yang diatur di file .env.",
    )
    start = st.button("Tarik data", type="primary", width="stretch")

# ---------- Halaman utama ----------

st.title("Job Scraper")
st.caption("Lowongan Accounting dari Seek Australia dan JobStreet Indonesia. Setiap tarikan mengambil data terbaru dari situs.")

terms = scraper.parse_terms(software_raw)

if start:
    run_search(
        site_key, locations, sub_names, daterange, with_description,
        with_description and with_requirements, terms,
    )

search = st.session_state.get("search")
if not search:
    st.info("Atur kriteria di panel kiri, lalu klik **Tarik data**.")
    st.stop()

rows = search["rows"]
result_site = sites[search["site_key"]]
st.subheader(f"Hasil dari {result_site['name']}")
st.caption(f"Ditarik {search['fetched_at']:%d %b %Y %H:%M} · " + " · ".join(search["messages"]))
for warning in search["warnings"]:
    st.warning(warning)

if not rows:
    st.info("Tidak ada lowongan yang cocok dengan kriteria ini.")
    st.stop()

pending = sum(r["description"] is None for r in rows)
if pending:
    failed = st.session_state.get("detail_failed", 0)
    note = f"{failed} deskripsi gagal diambil. " if failed else ""
    estimate = fmt_minutes(scraper.estimate_detail_seconds(cfg, pending))
    st.warning(f"{note}{pending} dari {len(rows)} lowongan belum punya deskripsi.")
    if st.button(f"Ambil deskripsi {pending} lowongan ({estimate})"):
        fetch_descriptions(search["site_key"], rows)
        st.rerun()

shown = scraper.apply_software(rows, terms)
if terms and pending:
    st.info("Lowongan tanpa deskripsi hanya dicocokkan lewat judulnya, jadi hasil filter software belum lengkap.")

if st.session_state.get("ai_error"):
    st.error(st.session_state.ai_error)
ai_pending = len(ai.pending_rows(shown))
if ai_pending and st.button(f"Ekstrak requirement {ai_pending} lowongan dengan AI"):
    fetch_requirements(shown)
    st.rerun()

col_total, col_shown, col_desc, col_req = st.columns(4)
col_total.metric("Lowongan ditemukan", len(rows))
col_shown.metric("Ditampilkan", len(shown), help="Setelah filter software")
col_desc.metric("Punya deskripsi", len(rows) - pending)
col_req.metric("Punya requirement", sum(r["requirements"] is not None for r in rows), help="Hasil ekstraksi AI")

if not shown:
    st.info(f"Tidak ada lowongan yang menyebut: {', '.join(terms)}.")
    st.stop()

df = to_frame(shown, result_site["timezone"])
st.dataframe(
    df,
    column_order=TABLE_COLUMNS,
    column_config={
        "title": st.column_config.TextColumn("Job title", width="medium"),
        "company": "Company",
        "location": "Location",
        "subclassification": "Subclass",
        "salary": "Salary",
        "listed_at": st.column_config.TextColumn("Tanggal posting", help=f"Zona waktu {result_site['timezone']}"),
        "software": "Software",
        "work_type": "Work type",
        "work_arrangement": "Arrangement",
        "requirements": st.column_config.TextColumn("Requirements", width="large", help="Diekstrak oleh AI"),
        "description": st.column_config.TextColumn("Job description", width="large"),
        "job_url": st.column_config.LinkColumn("Link", display_text="Buka"),
    },
    hide_index=True,
    width="stretch",
)

stamp = f"{search['site_key']}_{search['fetched_at']:%Y%m%d_%H%M}"
col_csv, col_xlsx, _ = st.columns([1, 1, 4])
col_csv.download_button(
    "Download CSV",
    df.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"jobs_{stamp}.csv",
    mime="text/csv",
    width="stretch",
)
col_xlsx.download_button(
    "Download Excel",
    to_excel(df),
    file_name=f"jobs_{stamp}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    width="stretch",
)
