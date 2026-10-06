"""Scraper lowongan Accounting dari au.seek.com dan id.jobstreet.com.

Modul ini berisi logika inti (dipakai oleh app.py) dan CLI sederhana yang menulis CSV.
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import httpx

SEARCH_PATH = "/api/jobsearch/v5/search"
GRAPHQL_PATH = "/graphql"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
CONFIG_PATH = Path(__file__).with_name("config.toml")

JOB_DETAILS_QUERY = "query jobDetails($id: ID!) { jobDetails(id: $id) { job { id content } } }"

FIELDS = [
    "job_id",
    "title",
    "company",
    "location",
    "search_location",
    "subclassification",
    "work_type",
    "work_arrangement",
    "seniority",
    "industry",
    "salary",
    "listed_at",
    "apply_type",
    "software",
    "description",
    "requirements",
    "job_url",
    "scraped_at",
]

log = logging.getLogger("seek")


class FetchError(Exception):
    pass


@dataclass
class Stats:
    requests: int = 0
    failed_requests: int = 0


@dataclass
class SearchResult:
    rows: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed_locations: int = 0


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def normalize_iso(value: str | None) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "br", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(content: str | None) -> str:
    if not content:
        return ""
    parser = _TextExtractor()
    parser.feed(content)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and line != "-")


def new_row(**values) -> dict:
    """Baris hasil dengan semua kolom FIELDS; kolom yang tidak diisi bernilai None."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise KeyError(f"kolom tidak dikenal: {sorted(unknown)}")
    return {field: values.get(field) for field in FIELDS}


LOCATION_SUGGEST_QUERY = """query SearchLocationsSuggest($query: String!, $count: Int!, $recentLocation: String!, $locale: Locale, $country: CountryCodeIso2) {
  searchLocationsSuggest(query: $query, count: $count, recentLocation: $recentLocation, locale: $locale, country: $country) {
    suggestions {
      ... on LocationSuggestion { text }
    }
  }
}"""


def suggest_locations(site: dict, http_cfg: dict, query: str, count: int = 8, client: httpx.Client | None = None) -> list[str]:
    """Saran lokasi dari Seek/JobStreet, sama dengan kolom lokasi di situsnya. Kosong jika gagal."""
    query = query.strip()
    if len(query) < 2:
        return []
    base_url = site["base_url"]
    body = {
        "operationName": "SearchLocationsSuggest",
        "variables": {"query": query, "count": count, "recentLocation": "", "locale": site["locale"], "country": site["country_code"]},
        "query": LOCATION_SUGGEST_QUERY,
    }
    headers = {"User-Agent": http_cfg["user_agent"], "Accept": "application/json", "Origin": base_url, "Referer": base_url + "/"}
    try:
        post = client.post if client else httpx.post
        resp = post(base_url + GRAPHQL_PATH, json=body, headers=headers, timeout=10)
        suggestions = resp.json()["data"]["searchLocationsSuggest"]["suggestions"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("Saran lokasi gagal diambil (%s)", type(e).__name__)
        return []
    return [item["text"] for item in suggestions if item.get("text")]


def parse_job(raw: dict, base_url: str, search_location: str, classification: int, sub_ids: list[int], scraped_at: str) -> dict:
    """Ubah satu item response pencarian menjadi baris hasil.

    Satu job bisa punya beberapa klasifikasi (mis. Audit - Internal + Government - Federal),
    jadi subklasifikasi diambil dari entri klasifikasi yang dicari, bukan entri pertama.
    """
    wanted = {str(s) for s in sub_ids}
    in_class, selected = [], []
    for c in raw.get("classifications") or []:
        sub = c.get("subclassification") or {}
        name = sub.get("description")
        if not name or str((c.get("classification") or {}).get("id")) != str(classification):
            continue
        if name not in in_class:
            in_class.append(name)
        if str(sub.get("id")) in wanted and name not in selected:
            selected.append(name)
    job_id = str(raw["id"])
    return new_row(
        job_id=job_id,
        title=raw.get("title"),
        company=raw.get("companyName") or (raw.get("advertiser") or {}).get("description"),
        location="; ".join(loc["label"] for loc in raw.get("locations") or [] if loc.get("label")) or None,
        search_location=search_location,
        subclassification="; ".join(selected or in_class) or None,
        work_type="; ".join(raw.get("workTypes") or []) or None,
        work_arrangement=(raw.get("workArrangements") or {}).get("displayText") or None,
        salary=raw.get("salaryLabel") or None,
        listed_at=normalize_iso(raw.get("listingDate")),
        job_url=f"{base_url}/job/{job_id}",
        scraped_at=scraped_at,
    )


class SeekClient:
    def __init__(self, cfg: dict, site_key: str, stats: Stats | None = None, sleep=time.sleep):
        self.site = cfg["sites"][site_key]
        self.search_cfg = cfg["search"]
        self.http = cfg["http"]
        self.stats = stats or Stats()
        self.sleep = sleep
        self._first = True
        base_url = self.site["base_url"]
        self.client = httpx.Client(
            base_url=base_url,
            timeout=self.http["timeout"],
            headers={
                "User-Agent": self.http["user_agent"],
                "Accept": "application/json",
                "Accept-Language": "en-AU,en;q=0.9",
                "Origin": base_url,
                "Referer": base_url + "/",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, delay: tuple[float, float], **kwargs) -> dict:
        retries = self.http["max_retries"]
        for attempt in range(retries + 1):
            if not self._first:
                self.sleep(random.uniform(*delay))
            self._first = False
            self.stats.requests += 1
            try:
                resp = self.client.request(method, path, **kwargs)
                if resp.status_code == 200:
                    return resp.json()
                reason = f"HTTP {resp.status_code}"
                retryable = resp.status_code in RETRY_STATUSES
            except (httpx.TransportError, ValueError) as e:
                reason = f"{type(e).__name__}: {e}"
                retryable = True
            self.stats.failed_requests += 1
            if not retryable or attempt == retries:
                raise FetchError(reason)
            wait = self.http["backoff_base"] * 2**attempt + random.uniform(0, 1)
            log.warning("Request gagal (%s), retry %d/%d dalam %.0f detik", reason, attempt + 1, retries, wait)
            self.sleep(wait)
        raise FetchError("unreachable")

    def search(self, where: str, sub_ids: list[int], daterange: int) -> tuple[list[dict], int, int, bool]:
        """Ambil semua halaman hasil pencarian untuk satu lokasi.

        Mengembalikan (jobs, received, total_count, truncated). `received` adalah jumlah item
        mentah: situs menyisipkan salinan "promoted" dari job yang sama dan ikut menghitungnya
        di totalCount, jadi pagination dibandingkan dengan item mentah, bukan job unik.
        """
        params = {
            "siteKey": self.site["site_key"],
            "locale": self.site["locale"],
            "classification": self.search_cfg["classification"],
            "sortmode": self.search_cfg["sort_mode"],
            "pageSize": self.search_cfg["page_size"],
        }
        if where:
            params["where"] = where
        if sub_ids:
            params["subclassification"] = ",".join(str(s) for s in sub_ids)
        if daterange > 0:
            params["daterange"] = daterange

        delay = (self.http["delay_min"], self.http["delay_max"])
        jobs: dict[str, dict] = {}
        total = 0
        received = 0
        for page in range(1, self.search_cfg["max_pages"] + 1):
            data = self._request("GET", SEARCH_PATH, delay, params={**params, "page": page})
            total = data.get("totalCount") or 0
            batch = data.get("data") or []
            received += len(batch)
            for raw in batch:
                jobs.setdefault(str(raw["id"]), raw)
            if not batch or received >= total:
                return list(jobs.values()), received, total, False
        return list(jobs.values()), received, total, True

    def fetch_description(self, job_id: str) -> str:
        """Ambil isi lengkap lowongan (teks polos) lewat GraphQL."""
        delay = (self.http["detail_delay_min"], self.http["detail_delay_max"])
        body = {"operationName": "jobDetails", "variables": {"id": job_id}, "query": JOB_DETAILS_QUERY}
        data = self._request("POST", GRAPHQL_PATH, delay, json=body)
        job = ((data.get("data") or {}).get("jobDetails") or {}).get("job")
        if not job:
            self.stats.failed_requests += 1
            errors = data.get("errors") or []
            raise FetchError(errors[0].get("message", "tanpa data") if errors else "tanpa data")
        return html_to_text(job.get("content"))


def collect_jobs(
    client: SeekClient,
    locations: list[str],
    sub_ids: list[int],
    daterange: int,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> SearchResult:
    """Jalankan pencarian untuk tiap lokasi dan gabungkan hasilnya tanpa duplikat."""
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    classification = client.search_cfg["classification"]
    result = SearchResult()
    seen: set[str] = set()
    targets = locations or [""]
    for i, where in enumerate(targets):
        label = where or client.site.get("all_locations_label", "Semua lokasi")
        if on_progress:
            on_progress(i, len(targets), label)
        try:
            raw_jobs, received, total, truncated = client.search(where, sub_ids, daterange)
        except FetchError as e:
            result.failed_locations += 1
            result.warnings.append(f"{label}: GAGAL ({e})")
            continue
        new = 0
        for raw in raw_jobs:
            row = parse_job(raw, client.site["base_url"], label, classification, sub_ids, scraped_at)
            if row["job_id"] not in seen:
                seen.add(row["job_id"])
                result.rows.append(row)
                new += 1
        dupes = received - len(raw_jobs)
        result.messages.append(f"{label}: {new} job" + (f" (+{dupes} duplikat promoted dibuang)" if dupes else ""))
        if truncated:
            result.warnings.append(
                f"{label}: batas max_pages={client.search_cfg['max_pages']} tercapai, data kemungkinan "
                f"TERPOTONG ({received} dari {total}). Perpendek rentang hari atau persempit subklasifikasi."
            )
        elif received < total:
            result.warnings.append(f"{label}: hanya dapat {received} dari {total} item yang dilaporkan API")
    if on_progress:
        on_progress(len(targets), len(targets), "")
    return result


def add_descriptions(
    client: SeekClient,
    rows: list[dict],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Isi kolom description untuk baris yang belum punya. Mengembalikan jumlah yang gagal."""
    pending = [r for r in rows if r["description"] is None]
    failed = 0
    for i, row in enumerate(pending):
        if on_progress:
            on_progress(i, len(pending), row["title"] or row["job_id"])
        try:
            row["description"] = client.fetch_description(row["job_id"])
        except FetchError as e:
            failed += 1
            log.warning("Deskripsi job %s gagal diambil (%s)", row["job_id"], e)
    if on_progress:
        on_progress(len(pending), len(pending), "")
    return failed


def filter_title(rows: list[dict], keywords: list[str]) -> list[dict]:
    """Hanya job yang judulnya memuat salah satu keyword (tanpa peduli huruf besar/kecil).

    Pencocokan per potongan teks, jadi "audit" juga cocok dengan "Auditor".
    """
    wanted = [k.lower() for k in keywords if k.strip()]
    if not wanted:
        return rows
    return [r for r in rows if any(k in (r["title"] or "").lower() for k in wanted)]


def estimate_detail_seconds(cfg: dict, count: int) -> float:
    http = cfg["http"]
    return count * ((http["detail_delay_min"] + http["detail_delay_max"]) / 2 + 0.5)


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig supaya Excel membaca karakter non-ASCII dengan benar
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape lowongan Accounting dari Seek / JobStreet ke CSV")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--site", default="seek_au", help="seek_au atau jobstreet_id (default: seek_au)")
    parser.add_argument("--location", action="append", help="bisa diulang; default dari config")
    parser.add_argument("--subclass", action="append", help="nama subklasifikasi, bisa diulang; 'all' = semua Accounting")
    parser.add_argument("--daterange", type=int, help="N hari terakhir; 0 = tanpa filter")
    parser.add_argument("--no-description", action="store_true", help="lewati pengambilan deskripsi (lebih cepat)")
    parser.add_argument("--requirements", action="store_true", help="ekstrak requirement dan software dengan Gemini (Vertex AI)")
    parser.add_argument("--csv", type=Path, default=Path("data/jobs.csv"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    cfg = load_config(args.config)
    if args.site not in cfg["sites"]:
        parser.error(f"--site harus salah satu dari: {', '.join(cfg['sites'])}")
    if cfg["sites"][args.site].get("type", "seek") != "seek":
        parser.error("Sumber ini hanya tersedia lewat UI (streamlit run app.py)")
    if args.requirements and args.no_description:
        parser.error("--requirements butuh deskripsi; jangan dipakai bersama --no-description")

    sub_map = cfg["subclassifications"]
    names = args.subclass or cfg["defaults"]["subclassifications"]
    if any(n.lower() == "all" for n in names):
        names = []
    unknown = [n for n in names if n not in sub_map]
    if unknown:
        parser.error(f"subklasifikasi tidak dikenal: {', '.join(unknown)}")
    sub_ids = [sub_map[n] for n in names]
    locations = args.location or cfg["sites"][args.site]["default_locations"]
    daterange = args.daterange if args.daterange is not None else cfg["search"]["default_daterange"]

    client = SeekClient(cfg, args.site)
    try:
        log.info(
            "Mulai: %s | lokasi=%s | subklasifikasi=%s | daterange=%s",
            cfg["sites"][args.site]["name"], locations, names or "semua Accounting",
            f"{daterange} hari" if daterange > 0 else "tanpa filter",
        )
        result = collect_jobs(client, locations, sub_ids, daterange)
        for msg in result.messages:
            log.info(msg)
        for warning in result.warnings:
            log.warning(warning)
        rows = result.rows
        failed_details = 0
        if rows and not args.no_description:
            log.info("Mengambil deskripsi %d job (perkiraan %.0f menit)", len(rows), estimate_detail_seconds(cfg, len(rows)) / 60)
            failed_details = add_descriptions(
                client, rows, lambda i, n, _: log.info("Deskripsi %d/%d", i, n) if i and i % 25 == 0 else None
            )
        failed_ai = 0
        if args.requirements and rows:
            import ai

            log.info("Mengekstrak requirement %d job dengan %s", len(ai.pending_rows(rows)), cfg["ai"]["model"])
            try:
                failed_ai, _ = ai.add_requirements(ai.make_client(), cfg["ai"], rows)
            except ai.AIError as e:
                log.error("Ekstraksi requirement dibatalkan: %s", e)
                failed_ai = len(ai.pending_rows(rows))
        write_csv(rows, args.csv)
        log.info(
            "Selesai: %d job ditulis ke %s | %d request (%d gagal), %d lokasi gagal, %d deskripsi gagal, "
            "%d requirement gagal",
            len(rows), args.csv, client.stats.requests, client.stats.failed_requests,
            result.failed_locations, failed_details, failed_ai,
        )
    finally:
        client.close()
    return 1 if result.failed_locations or failed_details or failed_ai else 0


if __name__ == "__main__":
    sys.exit(main())
