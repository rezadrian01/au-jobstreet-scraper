"""Pencarian lowongan Glints lewat actor Apify (berbayar per hasil, tanpa login Glints)."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from dotenv import load_dotenv

from ai import ENV_PATH
from scraper import FetchError, SearchResult, new_row, normalize_iso

log = logging.getLogger("seek.glints")

APIFY_BASE = "https://api.apify.com/v2"
RUNNING = {"READY", "RUNNING"}
TOKEN_HELP = "APIFY_TOKEN belum diisi di file .env (buat di https://console.apify.com/settings/integrations)."


def make_client() -> httpx.Client:
    load_dotenv(ENV_PATH)
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise FetchError(TOKEN_HELP)
    return httpx.Client(
        base_url=APIFY_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        transport=httpx.HTTPTransport(retries=5),
    )


def _call(client: httpx.Client, method: str, path: str, sleep=time.sleep, **kwargs) -> dict | list:
    """Panggil API Apify; koneksi yang ter-reset dicoba ulang beberapa kali."""
    for attempt in range(6):
        try:
            resp = client.request(method, path, **kwargs)
        except httpx.TransportError as e:
            if attempt == 5:
                raise FetchError(f"Apify tidak bisa dihubungi ({type(e).__name__})") from e
            sleep(1.5)
            continue
        if resp.status_code in (401, 403):
            raise FetchError(f"Apify menolak token ({resp.status_code}). Periksa APIFY_TOKEN di .env.")
        if resp.status_code == 402:
            raise FetchError("Kredit Apify habis atau batas pemakaian bulanan tercapai.")
        if resp.status_code >= 400:
            raise FetchError(f"Apify HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    raise FetchError("unreachable")


def run_actor(
    client: httpx.Client,
    actor: str,
    run_input: dict,
    on_wait: Callable[[float], None] | None = None,
    sleep=time.sleep,
    timeout: float = 900,
) -> list[dict]:
    """Jalankan actor, tunggu sampai selesai, lalu kembalikan item dataset-nya."""
    run = _call(client, "POST", f"/acts/{actor}/runs", sleep, json=run_input)["data"]
    started = time.monotonic()
    while run["status"] in RUNNING:
        elapsed = time.monotonic() - started
        if elapsed > timeout:
            raise FetchError(f"Run Apify {run['id']} belum selesai setelah {timeout:.0f} detik")
        if on_wait:
            on_wait(elapsed)
        sleep(4)
        run = _call(client, "GET", f"/actor-runs/{run['id']}", sleep)["data"]
    if run["status"] != "SUCCEEDED":
        raise FetchError(f"Run Apify {run['id']} berakhir dengan status {run['status']}")
    return _call(client, "GET", f"/datasets/{run['defaultDatasetId']}/items", sleep, params={"clean": "true"})


def _salary(item: dict) -> str | None:
    low, high = item.get("salary_minimum"), item.get("salary_maximum")
    if not low and not high:
        return None
    amounts = " – ".join(f"{amount:,.0f}" for amount in (low, high) if amount)
    parts = [item.get("salary_currency"), amounts, item.get("salary_period")]
    return " ".join(str(p) for p in parts if p)


def parse_job(item: dict, search_location: str, scraped_at: str) -> dict:
    """Ubah satu item hasil actor menjadi baris hasil scraper."""
    url = item.get("platform_url") or ""
    location = item.get("location") or {}
    return new_row(
        job_id="gl-" + url.rstrip("/").rsplit("/", 1)[-1],
        title=item.get("title") or None,
        company=item.get("company_name") or None,
        location=(location.get("raw") if isinstance(location, dict) else str(location)) or None,
        search_location=search_location,
        subclassification=item.get("job_function") or None,
        work_type=item.get("job_type") or None,
        work_arrangement=item.get("work_mode") or ("Remote" if item.get("is_remote") else None),
        seniority=item.get("job_level") or None,
        industry=item.get("company_industry") or None,
        salary=_salary(item),
        listed_at=normalize_iso(item.get("posted_date")),
        description=item.get("description") or None,
        job_url=url or None,
        scraped_at=scraped_at,
    )


def collect_jobs(
    site: dict,
    keywords: list[str],
    locations: list[str],
    daterange: int,
    max_results: int,
    on_progress: Callable[[int, int, str], None] | None = None,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> SearchResult:
    """Jalankan satu run Apify per keyword x lokasi dan gabungkan hasilnya tanpa duplikat."""
    now = datetime.now(timezone.utc)
    scraped_at = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(days=daterange)).isoformat(timespec="seconds") if daterange > 0 else None
    result = SearchResult()
    try:
        client = client or make_client()
    except FetchError as e:
        result.failed_locations += 1
        result.warnings.append(str(e))
        return result
    seen: set[str] = set()
    searches = [(k, loc) for loc in (locations or [""]) for k in keywords]
    try:
        for i, (keyword, location) in enumerate(searches):
            where = location or site["all_locations_label"]
            label = f'{where} · "{keyword}"'
            if on_progress:
                on_progress(i, len(searches), f"{label} · memulai run Apify")
            run_input = {"keyword": keyword, "country": site["country"], "max_results": max_results}
            if location:
                run_input["location"] = location
            if cutoff:
                run_input["posted_since"] = cutoff[:10]
            try:
                items = run_actor(
                    client, site["actor"], run_input, sleep=sleep,
                    on_wait=lambda s: on_progress(i, len(searches), f"{label} · menunggu Apify ({s:.0f} dtk)") if on_progress else None,
                )
            except FetchError as e:
                result.failed_locations += 1
                result.warnings.append(f"{label}: GAGAL ({e})")
                continue
            new = too_old = 0
            for item in items:
                row = parse_job(item, where, scraped_at)
                # Filter tanggal Glints memakai waktu update, jadi lowongan lama yang diperbarui ikut terbawa
                if cutoff and row["listed_at"] and row["listed_at"] < cutoff:
                    too_old += 1
                elif row["job_id"] not in seen:
                    seen.add(row["job_id"])
                    result.rows.append(row)
                    new += 1
            note = f" ({too_old} lowongan yang diposting lebih lama dibuang)" if too_old else ""
            result.messages.append(f"{label}: {new} job{note}")
            if len(items) >= max_results:
                result.warnings.append(
                    f"{label}: pencarian berhenti di batas {max_results} hasil, jadi mungkin masih ada lowongan "
                    "yang belum terambil. Naikkan \"Maks. hasil per keyword\" di sidebar (menambah biaya Apify)."
                )
    finally:
        client.close()
    if on_progress:
        on_progress(len(searches), len(searches), "")
    return result
