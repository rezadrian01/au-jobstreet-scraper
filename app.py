"""UI web lokal untuk scraper. Jalankan: streamlit run app.py"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit_searchbox import st_searchbox

import ai
import glints
import jobspy_sources
import scraper

st.set_page_config(page_title="Job Scraper", page_icon=":mag:", layout="wide")

cfg = scraper.load_config()
sites = cfg["sites"]
sub_map = cfg["subclassifications"]
class_name = cfg["search"]["classification_name"]

KEYWORD_SITES = ("linkedin", "indeed", "glints")  # dicari lewat keyword, bukan subklasifikasi
DESCRIPTION_FETCHERS = ("seek", "linkedin")  # deskripsi diambil terpisah, 1 request per lowongan
# Kolom pelengkap yang hanya disediakan sebagian sumber; disembunyikan jika kosong di semua baris.
# Kolom lain (termasuk software dan gaji) selalu tampil walaupun kosong.
OPTIONAL_COLUMNS = ("subclassification", "work_type", "work_arrangement", "seniority", "industry", "apply_type")

TABLE_COLUMNS = [
    "title", "company", "location", "subclassification", "salary", "listed_at", "software",
    "work_type", "work_arrangement", "seniority", "industry", "apply_type", "requirements", "description", "job_url",
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
    bar = st.progress(0.0)

    def on_progress(i: int, n: int, label: str) -> None:
        remaining = fmt_minutes(scraper.estimate_detail_seconds(cfg, n - i))
        bar.progress(i / n if n else 1.0, text=f"Deskripsi {i}/{n} · sisa {remaining} · {label[:60]}")

    if site_type(site_key) == "linkedin":
        failed = jobspy_sources.add_descriptions(cfg, sites[site_key], rows, on_progress)
    else:
        client = scraper.SeekClient(cfg, site_key)
        try:
            failed = scraper.add_descriptions(client, rows, on_progress)
        finally:
            client.close()
    bar.empty()
    st.session_state.detail_failed = failed


def fetch_requirements(rows: list[dict]) -> None:
    """Ekstrak requirement dan software dengan Gemini untuk baris yang punya deskripsi tetapi belum diproses."""
    if not ai.pending_rows(rows):
        return
    bar = st.progress(0.0, text="Menghubungi Gemini…")

    def on_progress(i: int, n: int, label: str) -> None:
        bar.progress(i / n if n else 1.0, text=f"Analisis AI {i}/{n} · {label[:60]}")

    try:
        failed, error = ai.add_requirements(ai.make_client(), cfg["ai"], rows, on_progress)
    except ai.AIError as e:
        failed, error = len(ai.pending_rows(rows)), str(e)
    bar.empty()
    st.session_state.ai_error = f"{failed} lowongan gagal dianalisis AI: {error}" if failed else None


def site_type(site_key: str) -> str:
    """"seek" (Seek/JobStreet, berbasis subklasifikasi), atau "linkedin" / "indeed" / "glints" (berbasis keyword)."""
    return sites[site_key].get("type", "seek")


def run_search(
    site_key: str,
    locations: list[str],
    sub_names: list[str],
    keywords: list[str],
    daterange: int,
    with_description: bool,
    with_requirements: bool,
    title_only: bool,
    max_results: int,
) -> None:
    bar = st.progress(0.0, text="Mencari lowongan…")

    def on_progress(i: int, n: int, label: str) -> None:
        bar.progress(i / n if n else 1.0, text=f"Mencari lowongan: {label}" if label else "Pencarian selesai")

    if site_type(site_key) in ("linkedin", "indeed"):
        result = jobspy_sources.collect_jobs(sites[site_key], keywords, locations, daterange, max_results, on_progress)
    elif site_type(site_key) == "glints":
        result = glints.collect_jobs(sites[site_key], keywords, locations, daterange, max_results, on_progress)
    else:
        client = scraper.SeekClient(cfg, site_key)
        try:
            result = scraper.collect_jobs(client, locations, [sub_map[n] for n in sub_names], daterange, on_progress)
        finally:
            client.close()
    bar.empty()
    st.session_state.search = {
        "site_key": site_key,
        "keywords": keywords,
        "rows": result.rows,
        "messages": result.messages,
        "warnings": result.warnings,
        "fetched_at": datetime.now(),
    }
    st.session_state.detail_failed = 0
    st.session_state.ai_error = None
    if with_description:
        # Hanya lowongan yang lolos filter judul, supaya tidak mengambil deskripsi yang tidak ditampilkan
        candidates = candidate_rows(result.rows, keywords, title_only)
        if site_type(site_key) in DESCRIPTION_FETCHERS:  # Indeed dan Glints sudah termasuk deskripsi
            fetch_descriptions(site_key, candidates)
        if with_requirements:
            fetch_requirements(candidates)


def candidate_rows(rows: list[dict], keywords: list[str], title_only: bool) -> list[dict]:
    """Baris yang lolos filter judul (jika aktif); deskripsi dan AI hanya dijalankan untuk ini."""
    return scraper.filter_title(rows, keywords) if title_only else rows


@st.cache_data(ttl=3600, show_spinner=False)
def location_suggestions(site_key: str, query: str) -> list[str]:
    return scraper.suggest_locations(sites[site_key], cfg["http"], query)


def add_location(site_key: str, location: str | None) -> None:
    """Masukkan lokasi yang dipilih dari kotak pencarian ke daftar lokasi terpilih."""
    if not location:
        return
    options = st.session_state[f"location_options_{site_key}"]
    selected = st.session_state[f"locations_{site_key}"]
    if location not in options:
        options.append(location)
    if location not in selected:
        st.session_state[f"locations_{site_key}"] = [*selected, location]


def to_frame(rows: list[dict], site: dict) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=scraper.FIELDS)
    if not site.get("date_only"):
        listed = pd.to_datetime(df["listed_at"], utc=True, errors="coerce").dt.tz_convert(site["timezone"])
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
    st.session_state.setdefault(f"location_options_{site_key}", list(site["locations"]))
    st.session_state.setdefault(f"locations_{site_key}", list(site["default_locations"]))
    if site_type(site_key) == "seek":
        st_searchbox(
            lambda query: location_suggestions(site_key, query),
            label="Cari lokasi",
            placeholder="Ketik kota, provinsi, atau kecamatan…",
            key=f"location_search_{site_key}",
            clear_on_submit=True,
            debounce=300,
            submit_function=lambda location: add_location(site_key, location),
            help="Saran lokasi diambil langsung dari situsnya, sama seperti kolom lokasi di webnya. "
            "Pilih salah satu untuk menambahkannya ke daftar di bawah.",
        )
    locations = st.multiselect(
        "Lokasi terpilih" if site_type(site_key) == "seek" else "Lokasi",
        st.session_state[f"location_options_{site_key}"],
        key=f"locations_{site_key}",
        accept_new_options=True,
        placeholder=site["all_locations_label"],
        help="Kosongkan untuk semua lokasi. Bisa mengetik lokasi lain yang tidak ada di daftar.",
    )
    sub_names, keywords, title_only, max_results = [], [], False, 0
    keyword_site = site_type(site_key) in KEYWORD_SITES
    if keyword_site:
        keywords = st.multiselect(
            "Keyword",
            site["keyword_suggestions"],
            default=site["default_keywords"],
            accept_new_options=True,
            placeholder="Ketik keyword lalu Enter",
            key=f"keywords_{site_key}",
            help="Sumber ini tidak punya subklasifikasi, jadi lowongan dicari lewat keyword. Bisa lebih dari satu: "
            "tiap keyword dicari terpisah lalu hasilnya digabung tanpa duplikat. Ketik untuk menambah keyword sendiri.",
        )
        title_only = st.checkbox(
            "Keyword harus ada di judul",
            value=True,
            help="Keyword dicocokkan situs ke seluruh isi lowongan, jadi banyak hasil yang tidak relevan. "
            "Dengan opsi ini hanya lowongan yang judulnya memuat salah satu keyword yang ditampilkan. "
            "Bisa diubah tanpa menarik ulang data.",
        )
    else:
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
    with_description = st.checkbox(
        "Ambil deskripsi lowongan",
        value=True,
        help="Wajib untuk analisis AI. Di sebagian sumber butuh 1 request per lowongan, jadi lebih lama.",
    )
    with_requirements = st.checkbox(
        "Analisis AI: requirement dan software",
        value=True,
        disabled=not with_description,
        help=f"Gemini ({cfg['ai']['model']}) membaca deskripsi lalu mengisi kolom Requirements (poin-poin "
        "requirement) dan Software (software yang disebut di lowongan). Berbayar, ditagih ke project Google "
        "Cloud yang diatur di file .env.",
    )
    if site_type(site_key) == "glints":
        max_results = st.number_input(
            "Maks. hasil per keyword",
            min_value=10,
            max_value=500,
            value=site["max_results"],
            step=10,
            help="Glints diambil lewat Apify, yang menagih $0,005 per lowongan + $0,01 per pencarian ke akun "
            "Apify Anda. 100 hasil = sekitar $0,51. Paket gratis Apify: $5 per bulan.",
        )
    elif keyword_site:
        max_results = st.number_input(
            "Maks. hasil per keyword",
            min_value=25,
            max_value=1000,
            value=site["max_results"],
            step=25,
            help="Pencarian berhenti setelah jumlah ini. Hasil diurutkan dari yang paling relevan, dan situsnya "
            "sendiri berhenti di sekitar 1.000.",
        )
    start = st.button("Tarik data", type="primary", width="stretch", disabled=keyword_site and not keywords)

# ---------- Halaman utama ----------

st.title("Job Scraper")
st.caption("Lowongan dari Seek Australia, JobStreet Indonesia, LinkedIn, Indeed, dan Glints. Setiap tarikan mengambil data terbaru dari situs.")

if start:
    run_search(
        site_key, locations, sub_names, keywords, daterange, with_description,
        with_description and with_requirements, title_only, max_results,
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

result_type = site_type(search["site_key"])
candidates = candidate_rows(rows, search["keywords"], title_only and result_type in KEYWORD_SITES)
pending = sum(r["description"] is None for r in candidates)
if pending:
    failed = st.session_state.get("detail_failed", 0)
    note = f"{failed} deskripsi gagal diambil. " if failed else ""
    estimate = fmt_minutes(scraper.estimate_detail_seconds(cfg, pending))
    st.warning(f"{note}{pending} dari {len(candidates)} lowongan belum punya deskripsi.")
    if result_type in DESCRIPTION_FETCHERS and st.button(f"Ambil deskripsi {pending} lowongan ({estimate})"):
        fetch_descriptions(search["site_key"], candidates)
        st.rerun()

shown = candidates

if st.session_state.get("ai_error"):
    st.error(st.session_state.ai_error)
ai_pending = len(ai.pending_rows(shown))
if ai_pending and st.button(f"Analisis {ai_pending} lowongan dengan AI (requirement dan software)"):
    fetch_requirements(shown)
    st.rerun()

col_total, col_shown, col_desc, col_req = st.columns(4)
col_total.metric("Lowongan ditemukan", len(rows))
col_shown.metric("Ditampilkan", len(shown), help="Setelah filter judul")
col_desc.metric("Punya deskripsi", sum(r["description"] is not None for r in rows))
col_req.metric("Dianalisis AI", sum(r["requirements"] is not None for r in rows), help="Kolom Requirements dan Software")

if not shown:
    st.info("Tidak ada lowongan yang judulnya memuat keyword. Matikan \"Keyword harus ada di judul\" untuk melihat semuanya.")
    st.stop()

df = to_frame(shown, result_site)
empty = [c for c in OPTIONAL_COLUMNS if not df[c].fillna("").astype(str).str.strip().ne("").any()]
filled = [c for c in scraper.FIELDS if c not in empty]
df = df[filled]
st.dataframe(
    df,
    column_order=[c for c in TABLE_COLUMNS if c in filled],
    column_config={
        "title": st.column_config.TextColumn("Job title", width="medium"),
        "company": "Company",
        "location": "Location",
        "subclassification": "Subclass",
        "salary": "Salary",
        "listed_at": st.column_config.TextColumn("Tanggal posting", help=f"Zona waktu {result_site['timezone']}"),
        "software": st.column_config.TextColumn("Software", help="Software yang disebut di lowongan, diekstrak oleh AI"),
        "seniority": "Seniority",
        "industry": "Industry",
        "apply_type": st.column_config.TextColumn("Cara melamar", help="Easy Apply di LinkedIn atau lewat situs perusahaan"),
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
