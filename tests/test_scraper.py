import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper

SUB_NAMES = {"6144": "Audit - External", "6145": "Audit - Internal"}

RAW_JOB = {
    "id": "94927352",
    "title": "Internal Auditor",
    "companyName": "Acme",
    "advertiser": {"id": "1", "description": "Acme Recruiting"},
    "classifications": [
        {"classification": {"id": "1210", "description": "Government & Defence"},
         "subclassification": {"id": "6300", "description": "Government - Federal"}},
        {"classification": {"id": "1200", "description": "Accounting"},
         "subclassification": {"id": "6145", "description": "Audit - Internal"}},
    ],
    "locations": [{"label": "Macquarie Park, Sydney NSW"}],
    "workTypes": ["Full time"],
    "workArrangements": {"data": [], "displayText": "Hybrid"},
    "salaryLabel": "",
    "listingDate": "2026-09-29T01:11:01.000Z",
}


def make_cfg(max_pages=20, page_size=2, max_retries=2):
    return {
        "search": {"site_key": "AU-Main", "locale": "en-AU", "classification": 1200,
                   "sort_mode": "ListedDate", "page_size": page_size, "max_pages": max_pages},
        "http": {"user_agent": "test", "timeout": 5, "delay_min": 0, "delay_max": 0,
                 "max_retries": max_retries, "backoff_base": 0},
    }


def make_client(handler, **cfg_kwargs):
    stats = scraper.Stats()
    client = scraper.SeekClient(make_cfg(**cfg_kwargs), stats, sleep=lambda s: None)
    client.client = httpx.Client(base_url=scraper.BASE_URL, transport=httpx.MockTransport(handler))
    return client, stats


def test_parse_job_picks_configured_subclassification():
    row = scraper.parse_job(RAW_JOB, "All Sydney NSW", SUB_NAMES, "2026-10-03T00:00:00+00:00")
    assert row["job_id"] == "94927352"
    assert row["subclassification"] == "Audit - Internal"
    assert row["company"] == "Acme"
    assert row["location"] == "Macquarie Park, Sydney NSW"
    assert row["work_arrangement"] == "Hybrid"
    assert row["work_type"] == "Full time"
    assert row["salary"] is None
    assert row["listed_at"] == "2026-09-29T01:11:01+00:00"
    assert row["job_url"] == "https://au.seek.com/job/94927352"


def test_upsert_does_not_duplicate(tmp_path):
    conn = scraper.init_db(tmp_path / "jobs.db")
    row = scraper.parse_job(RAW_JOB, "All Sydney NSW", SUB_NAMES, "t1")
    scraper.upsert_jobs(conn, [row])
    scraper.upsert_jobs(conn, [{**row, "title": "Changed", "scraped_at": "t2"}])
    assert conn.execute("SELECT job_id, title, scraped_at FROM jobs").fetchall() == [("94927352", "Changed", "t2")]


def test_pagination_runs_until_total_reached():
    def handler(request):
        page = int(request.url.params["page"])
        ids = {1: ["1", "2"], 2: ["3", "4"], 3: ["5"]}[page]
        return httpx.Response(200, json={"totalCount": 5, "data": [{"id": i} for i in ids]})

    client, stats = make_client(handler)
    jobs, received, total, truncated = client.fetch_combination("All Sydney NSW", 6144, 7)
    assert [j["id"] for j in jobs] == ["1", "2", "3", "4", "5"]
    assert (received, total, truncated, stats.requests) == (5, 5, False, 3)


def test_promoted_duplicates_count_toward_total():
    def handler(request):
        return httpx.Response(200, json={"totalCount": 3, "data": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})

    client, stats = make_client(handler, page_size=100)
    jobs, received, total, truncated = client.fetch_combination("All Sydney NSW", 6144, 7)
    assert [j["id"] for j in jobs] == ["1", "2"]
    assert (received, total, truncated, stats.requests) == (3, 3, False, 1)


def test_truncated_when_max_pages_reached():
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"totalCount": 100, "data": [{"id": f"{page}-a"}, {"id": f"{page}-b"}]})

    client, _ = make_client(handler, max_pages=3)
    jobs, received, total, truncated = client.fetch_combination("All Sydney NSW", 6144, 0)
    assert (len(jobs), received, total, truncated) == (6, 6, 100, True)


def test_retry_then_success_counts_failures():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"totalCount": 1, "data": [{"id": "1"}]})

    client, stats = make_client(handler)
    jobs, *_ = client.fetch_combination("All Sydney NSW", 6144, 7)
    assert len(jobs) == 1
    assert (stats.requests, stats.failed_requests) == (3, 2)


def test_gives_up_after_max_retries():
    client, stats = make_client(lambda request: httpx.Response(403), max_retries=2)
    with pytest.raises(scraper.FetchError):
        client.fetch_combination("All Sydney NSW", 6144, 7)
    assert stats.failed_requests == 3
