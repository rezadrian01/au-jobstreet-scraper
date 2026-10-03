"""Scraper lowongan au.seek.com (Accounting > Audit) ke SQLite, dengan export CSV."""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE_URL = "https://au.seek.com"
SEARCH_PATH = "/api/jobsearch/v5/search"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}

FIELDS = [
    "job_id",
    "title",
    "company",
    "location",
    "search_location",
    "work_arrangement",
    "subclassification",
    "work_type",
    "salary",
    "listed_at",
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


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def normalize_iso(value: str | None) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_job(raw: dict, search_location: str, sub_names: dict[str, str], scraped_at: str) -> dict:
    """Ubah satu item response API menjadi baris database.

    Satu job bisa punya beberapa klasifikasi (mis. Audit - Internal + Government - Federal),
    jadi subklasifikasi diambil dari entri yang ID-nya ada di config, bukan entri pertama.
    """
    subs = []
    for c in raw.get("classifications") or []:
        sub_id = str((c.get("subclassification") or {}).get("id"))
        if sub_id in sub_names and sub_names[sub_id] not in subs:
            subs.append(sub_names[sub_id])
    job_id = str(raw["id"])
    return {
        "job_id": job_id,
        "title": raw.get("title"),
        "company": raw.get("companyName") or (raw.get("advertiser") or {}).get("description"),
        "location": "; ".join(loc["label"] for loc in raw.get("locations") or [] if loc.get("label")) or None,
        "search_location": search_location,
        "work_arrangement": (raw.get("workArrangements") or {}).get("displayText") or None,
        "subclassification": "; ".join(subs) or None,
        "work_type": "; ".join(raw.get("workTypes") or []) or None,
        "salary": raw.get("salaryLabel") or None,
        "listed_at": normalize_iso(raw.get("listingDate")),
        "job_url": f"{BASE_URL}/job/{job_id}",
        "scraped_at": scraped_at,
    }


class SeekClient:
    def __init__(self, cfg: dict, stats: Stats, sleep=time.sleep):
        self.search = cfg["search"]
        self.http = cfg["http"]
        self.stats = stats
        self.sleep = sleep
        self._first = True
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=self.http["timeout"],
            headers={
                "User-Agent": self.http["user_agent"],
                "Accept": "application/json",
                "Accept-Language": "en-AU,en;q=0.9",
                "Referer": BASE_URL + "/",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _get(self, params: dict) -> dict:
        retries = self.http["max_retries"]
        for attempt in range(retries + 1):
            if not self._first:
                self.sleep(random.uniform(self.http["delay_min"], self.http["delay_max"]))
            self._first = False
            self.stats.requests += 1
            try:
                resp = self.client.get(SEARCH_PATH, params=params)
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

    def fetch_combination(self, where: str, sub_id: int, daterange: int) -> tuple[list[dict], int, int, bool]:
        """Ambil semua halaman untuk satu kombinasi lokasi x subklasifikasi.

        Mengembalikan (jobs, received, total_count, truncated). `received` adalah jumlah item
        mentah: Seek menyisipkan salinan "promoted" dari job yang sama dan ikut menghitungnya
        di totalCount, jadi pagination dibandingkan dengan item mentah, bukan job unik.
        """
        params = {
            "siteKey": self.search["site_key"],
            "locale": self.search["locale"],
            "where": where,
            "classification": self.search["classification"],
            "subclassification": sub_id,
            "sortmode": self.search["sort_mode"],
            "pageSize": self.search["page_size"],
        }
        if daterange > 0:
            params["daterange"] = daterange

        jobs: dict[str, dict] = {}
        total = 0
        received = 0
        for page in range(1, self.search["max_pages"] + 1):
            data = self._get({**params, "page": page})
            total = data.get("totalCount") or 0
            batch = data.get("data") or []
            received += len(batch)
            for raw in batch:
                jobs.setdefault(str(raw["id"]), raw)
            if not batch or received >= total:
                return list(jobs.values()), received, total, False
        return list(jobs.values()), received, total, True


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            search_location TEXT,
            work_arrangement TEXT,
            subclassification TEXT,
            work_type TEXT,
            salary TEXT,
            listed_at TEXT,
            job_url TEXT,
            scraped_at TEXT
        )
        """
    )
    return conn


def upsert_jobs(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cols = ", ".join(FIELDS)
    placeholders = ", ".join(f":{f}" for f in FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in FIELDS if f != "job_id")
    with conn:
        conn.executemany(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders}) ON CONFLICT(job_id) DO UPDATE SET {updates}",
            rows,
        )


def export_csv(conn: sqlite3.Connection, path: Path, scraped_at: str | None) -> int:
    """Export ke CSV. Jika scraped_at diisi, hanya job dari run tersebut (snapshot terbaru)."""
    query = f"SELECT {', '.join(FIELDS)} FROM jobs"
    args: tuple = ()
    if scraped_at:
        query += " WHERE scraped_at = ?"
        args = (scraped_at,)
    query += " ORDER BY listed_at DESC"
    rows = conn.execute(query, args).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    return len(rows)


def run(cfg: dict, daterange: int, csv_path: Path | None, csv_all: bool) -> int:
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sub_names = {str(sub_id): name for name, sub_id in cfg["subclassifications"].items()}
    stats = Stats()
    client = SeekClient(cfg, stats)
    conn = init_db(Path(cfg["storage"]["db_path"]))
    failed_combos = 0
    seen: set[str] = set()

    log.info("Mulai scrape: daterange=%s", f"{daterange} hari" if daterange > 0 else "tanpa filter")
    try:
        for where in cfg["locations"]:
            for name, sub_id in cfg["subclassifications"].items():
                label = f"{where} x {name}"
                try:
                    raw_jobs, received, total, truncated = client.fetch_combination(where, sub_id, daterange)
                except FetchError as e:
                    failed_combos += 1
                    log.error("%s: GAGAL (%s)", label, e)
                    continue
                rows = [parse_job(raw, where, sub_names, scraped_at) for raw in raw_jobs]
                upsert_jobs(conn, rows)
                seen.update(r["job_id"] for r in rows)
                dupes = received - len(rows)
                log.info(
                    "%s: %d job%s", label, len(rows),
                    f" (+{dupes} duplikat promoted dibuang)" if dupes else "",
                )
                if truncated:
                    log.warning(
                        "%s: batas max_pages=%d tercapai, data kemungkinan TERPOTONG (%d dari %d). "
                        "Perpendek daterange atau naikkan max_pages.",
                        label, cfg["search"]["max_pages"], received, total,
                    )
                elif received < total:
                    log.warning("%s: hanya dapat %d dari %d item yang dilaporkan API", label, received, total)

        db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        log.info(
            "Selesai: %d job unik di run ini, %d total di database, %d request (%d gagal), %d kombinasi gagal",
            len(seen), db_total, stats.requests, stats.failed_requests, failed_combos,
        )
        if csv_path:
            n = export_csv(conn, csv_path, None if csv_all else scraped_at)
            log.info("CSV: %d baris ditulis ke %s", n, csv_path)
    finally:
        client.close()
        conn.close()
    return 1 if failed_combos else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape lowongan Audit (External/Internal) dari au.seek.com")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.toml"))
    parser.add_argument("--daterange", type=int, help="N hari terakhir; 0 = tanpa filter (default: dari config)")
    parser.add_argument("--csv", type=Path, help="export hasil run ini ke file CSV")
    parser.add_argument("--csv-all", action="store_true", help="export seluruh isi database, bukan hanya run ini")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    cfg = load_config(args.config)
    db_path = Path(cfg["storage"]["db_path"])
    if not db_path.is_absolute():
        cfg["storage"]["db_path"] = str(args.config.resolve().parent / db_path)
    daterange = args.daterange if args.daterange is not None else cfg["search"]["daterange"]
    return run(cfg, daterange, args.csv, args.csv_all)


if __name__ == "__main__":
    sys.exit(main())
