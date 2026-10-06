"""Pencarian lowongan LinkedIn dan Indeed lewat JobSpy (tanpa login)."""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Callable

import httpx
from bs4 import BeautifulSoup

from scraper import RETRY_STATUSES, FetchError, SearchResult, html_to_text, new_row

log = logging.getLogger("seek.jobspy")

BATCH_RETRIES = 6
RETRY_WAIT = 2.0

JOB_TYPES = {
    "fulltime": "Full time",
    "parttime": "Part time",
    "contract": "Contract",
    "internship": "Internship",
    "temporary": "Temporary",
}


def _value(record: dict, key: str):
    """Nilai kolom JobSpy, dengan NaN/None/string kosong disamakan menjadi None."""
    value = record.get(key)
    if value is None or value != value or value == "":
        return None
    return value


def clean_markdown(text: str | None) -> str | None:
    """Rapikan deskripsi markdown dari JobSpy menjadi teks polos dengan bullet '- '."""
    if not text:
        return None
    text = re.sub(r"\\([\-*_.#&+()\[\]!>])", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*[*+]\s+", "- ", line.rstrip())
        line = re.sub(r"^#+\s*", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) or None


def _salary(record: dict) -> str | None:
    low, high = _value(record, "min_amount"), _value(record, "max_amount")
    if low is None and high is None:
        return None
    amounts = " – ".join(f"{amount:,.0f}" for amount in (low, high) if amount is not None)
    parts = [_value(record, "currency"), amounts, _value(record, "interval")]
    return " ".join(str(p) for p in parts if p)


def parse_job(record: dict, search_location: str, scraped_at: str) -> dict:
    """Ubah satu baris hasil JobSpy menjadi baris hasil scraper."""
    posted = _value(record, "date_posted")
    job_type = _value(record, "job_type")
    level = _value(record, "job_level")
    return new_row(
        job_id=str(record["id"]),
        title=_value(record, "title"),
        company=_value(record, "company"),
        location=_value(record, "location"),
        search_location=search_location,
        subclassification=_value(record, "job_function"),
        work_type="; ".join(JOB_TYPES.get(t.strip(), t.strip()) for t in str(job_type).split(",")) if job_type else None,
        work_arrangement="Remote" if _value(record, "is_remote") else None,
        seniority=str(level).capitalize() if level else None,
        industry=_value(record, "company_industry"),
        salary=_salary(record),
        listed_at=str(posted)[:10] if posted is not None else None,
        description=clean_markdown(_value(record, "description")),
        job_url=_value(record, "job_url"),
        scraped_at=scraped_at,
    )


class _LogCollector(logging.Handler):
    """Kumpulkan pesan error JobSpy (mis. 429 dari LinkedIn) supaya bisa ditampilkan ke user."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        message = record.getMessage()
        if message not in self.messages:
            self.messages.append(message)


def collect_jobs(
    site: dict,
    keywords: list[str],
    locations: list[str],
    daterange: int,
    max_results: int,
    on_progress: Callable[[int, int, str], None] | None = None,
    sleep=time.sleep,
) -> SearchResult:
    """Cari lowongan per keyword x lokasi dan gabungkan tanpa duplikat.

    Pencarian dipecah per batch supaya progress bisa dilaporkan. Untuk LinkedIn deskripsi tidak
    ikut di hasil pencarian; ambil terpisah lewat add_descriptions, hanya untuk lowongan yang
    memang akan dipakai. Indeed sudah menyertakan deskripsi.
    """
    from jobspy import scrape_jobs

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = SearchResult()
    seen: set[str] = set()
    limit, batch = max_results, site["batch_size"]
    searches = [(k, loc) for loc in (locations or [site["all_locations_label"]]) for k in keywords]
    source = site["jobspy_site"]
    extra = {"country_indeed": site["country_indeed"]} if "country_indeed" in site else {}
    collector = _LogCollector()
    jobspy_log = logging.getLogger("JobSpy:LinkedIn" if source == "linkedin" else "JobSpy:Indeed")
    jobspy_log.addHandler(collector)
    try:
        for i, (keyword, location) in enumerate(searches):
            label = f'{location} · "{keyword}"'
            new, offset, truncated = 0, 0, False
            while True:
                if on_progress:
                    on_progress(i * limit + min(offset, limit), len(searches) * limit, f"{label} · {new} lowongan")
                df = None
                for attempt in range(BATCH_RETRIES + 1):
                    logged = len(collector.messages)
                    try:
                        df = scrape_jobs(
                            site_name=[source],
                            search_term=keyword,
                            location=location,
                            results_wanted=batch,
                            offset=offset,
                            hours_old=daterange * 24 if daterange > 0 else None,
                            fetch_description=False,
                            description_format="markdown",
                            verbose=0,
                            **extra,
                        )
                        error = "; ".join(collector.messages[logged:]) if len(df) == 0 else None
                    except Exception as e:  # JobSpy melempar berbagai jenis exception saat diblokir atau gagal parsing
                        df, error = None, f"{type(e).__name__}: {e}"
                    # JobSpy menelan error jaringan dan mengembalikan hasil kosong; coba ulang batch itu
                    del collector.messages[logged:]
                    if not error:
                        break
                    if attempt < BATCH_RETRIES:
                        log.warning("%s gagal (%s), mencoba ulang batch", label, error)
                        sleep(RETRY_WAIT * (attempt + 1))
                if error:
                    result.failed_locations += 1
                    result.warnings.append(f"{label}: GAGAL setelah {BATCH_RETRIES + 1} percobaan ({error})")
                    break
                fresh = 0
                for record in df.to_dict("records"):
                    row = parse_job(record, location, scraped_at)
                    if row["job_id"] not in seen:
                        seen.add(row["job_id"])
                        result.rows.append(row)
                        fresh += 1
                new += fresh
                offset += batch
                if len(df) < batch or fresh == 0:
                    break
                if offset >= limit:
                    truncated = True
                    break
            result.messages.append(f"{label}: {new} job")
            if truncated:
                result.warnings.append(
                    f"{label}: pencarian berhenti di batas {limit} hasil, jadi mungkin masih ada lowongan yang "
                    "belum terambil. Naikkan \"Maks. hasil per keyword\" di sidebar atau perpendek rentang hari."
                )
    finally:
        jobspy_log.removeHandler(collector)
    result.warnings.extend(f"{source}: {message}" for message in collector.messages)
    if on_progress:
        on_progress(1, 1, "")
    return result


CRITERIA_FIELDS = {"Employment type": "work_type", "Seniority level": "seniority", "Industries": "industry", "Job function": "subclassification"}


def parse_details(html: str) -> dict:
    """Ambil deskripsi dan data pelengkap dari halaman lowongan LinkedIn (versi tamu, bahasa Inggris)."""
    soup = BeautifulSoup(html, "html.parser")
    markup = soup.find("div", class_=lambda c: c and "show-more-less-html__markup" in c)
    if markup is None:
        raise FetchError("deskripsi tidak ditemukan di halaman")
    details = {"description": html_to_text(str(markup))}
    for item in soup.select("li.description__job-criteria-item"):
        label, value = item.find("h3"), item.find("span")
        if not label or not value:
            continue
        field = CRITERIA_FIELDS.get(label.get_text(strip=True))
        text = value.get_text(strip=True)
        if field and text and text != "Not Applicable":
            details[field] = text
    if "apply-link-offsite" in html:
        details["apply_type"] = "External (situs perusahaan)"
    elif "apply-link-onsite" in html or "Easy Apply" in html:
        details["apply_type"] = "Easy Apply"
    return details


def fetch_details(client: httpx.Client, http_cfg: dict, job_id: str, sleep=time.sleep) -> dict:
    """Ambil detail satu lowongan dari endpoint tamu LinkedIn."""
    number = job_id.removeprefix("li-")
    retries = http_cfg["max_retries"]
    for attempt in range(retries + 1):
        try:
            resp = client.get(f"/jobs-guest/jobs/api/jobPosting/{number}")
            if resp.status_code == 200:
                return parse_details(resp.text)
            reason = f"HTTP {resp.status_code}"
            retryable = resp.status_code in RETRY_STATUSES
        except httpx.TransportError as e:
            reason, retryable = f"{type(e).__name__}: {e}", True
        if not retryable or attempt == retries:
            raise FetchError(reason)
        wait = http_cfg["backoff_base"] * 2**attempt + random.uniform(0, 1)
        log.warning("LinkedIn gagal (%s), retry %d/%d dalam %.0f detik", reason, attempt + 1, retries, wait)
        sleep(wait)
    raise FetchError("unreachable")


def add_descriptions(
    cfg: dict,
    site: dict,
    rows: list[dict],
    on_progress: Callable[[int, int, str], None] | None = None,
    sleep=time.sleep,
    client: httpx.Client | None = None,
) -> int:
    """Isi deskripsi dan data pelengkap (jenis pekerjaan, senioritas, industri, jenis lamaran) untuk
    baris yang belum punya deskripsi. Mengembalikan jumlah yang gagal."""
    http_cfg = cfg["http"]
    pending = [r for r in rows if r["description"] is None]
    client = client or httpx.Client(
        base_url=site["base_url"],
        timeout=http_cfg["timeout"],
        follow_redirects=True,
        headers={"User-Agent": http_cfg["user_agent"], "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
    )
    failed = 0
    try:
        for i, row in enumerate(pending):
            if on_progress:
                on_progress(i, len(pending), row["title"] or row["job_id"])
            if i:
                sleep(random.uniform(http_cfg["detail_delay_min"], http_cfg["detail_delay_max"]))
            try:
                for field, value in fetch_details(client, http_cfg, row["job_id"], sleep).items():
                    if field == "description" or not row.get(field):
                        row[field] = value
            except FetchError as e:
                failed += 1
                log.warning("Deskripsi job %s gagal diambil (%s)", row["job_id"], e)
    finally:
        client.close()
    if on_progress:
        on_progress(len(pending), len(pending), "")
    return failed
