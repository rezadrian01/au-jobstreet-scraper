import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
import scraper

BASE_URL = "https://au.seek.com"

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
        "search": {"classification": 1200, "sort_mode": "ListedDate", "page_size": page_size, "max_pages": max_pages},
        "http": {"user_agent": "test", "timeout": 5, "delay_min": 0, "delay_max": 0,
                 "detail_delay_min": 0, "detail_delay_max": 0, "max_retries": max_retries, "backoff_base": 0},
        "sites": {"seek_au": {"name": "Seek", "base_url": BASE_URL, "site_key": "AU-Main", "locale": "en-AU"}},
    }


def make_client(handler, **cfg_kwargs):
    client = scraper.SeekClient(make_cfg(**cfg_kwargs), "seek_au", sleep=lambda s: None)
    client.client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return client, client.stats


def test_parse_job_picks_selected_subclassification():
    row = scraper.parse_job(RAW_JOB, BASE_URL, "All Sydney NSW", 1200, [6144, 6145], "2026-10-03T00:00:00+00:00")
    assert row["job_id"] == "94927352"
    assert row["subclassification"] == "Audit - Internal"
    assert row["company"] == "Acme"
    assert row["location"] == "Macquarie Park, Sydney NSW"
    assert row["work_arrangement"] == "Hybrid"
    assert row["work_type"] == "Full time"
    assert row["salary"] is None
    assert row["listed_at"] == "2026-09-29T01:11:01+00:00"
    assert row["job_url"] == "https://au.seek.com/job/94927352"


def test_parse_job_without_subclass_filter_uses_searched_classification():
    row = scraper.parse_job(RAW_JOB, BASE_URL, "All Sydney NSW", 1200, [], "t")
    assert row["subclassification"] == "Audit - Internal"


def test_html_to_text():
    html = "<p>Proficiency in <strong>Xero</strong>&nbsp;or comparable systems</p><ul><li>CPA</li><li>Excel</li></ul>"
    assert scraper.html_to_text(html) == "Proficiency in Xero or comparable systems\n- CPA\n- Excel"
    assert scraper.html_to_text(None) == ""


def test_parse_terms():
    assert scraper.parse_terms(" Xero, MYOB\nxero ; SAP ") == ["Xero", "MYOB", "SAP"]
    assert scraper.parse_terms("") == []


def test_match_software_whole_word_case_insensitive():
    text = "Proficiency in XERO or comparable accounting systems. Disappointing sapling."
    assert scraper.match_software(text, ["Xero", "SAP", "MYOB"]) == ["Xero"]
    assert scraper.match_software("Experience with SAP/Oracle and MS Excel", ["sap", "MS Excel"]) == ["sap", "MS Excel"]
    assert scraper.match_software(None, ["Xero"]) == []


def test_apply_software_filters_to_any_match():
    rows = [
        {"title": "Accountant", "description": "Must know Xero", "software": None},
        {"title": "Auditor", "description": "Excel skills", "software": None},
        {"title": "MYOB Bookkeeper", "description": None, "software": None},
    ]
    matched = scraper.apply_software(rows, ["Xero", "MYOB"])
    assert [r["title"] for r in matched] == ["Accountant", "MYOB Bookkeeper"]
    assert [r["software"] for r in rows] == ["Xero", None, "MYOB"]
    assert scraper.apply_software(rows, []) == rows
    assert all(r["software"] is None for r in rows)


def test_pagination_runs_until_total_reached():
    def handler(request):
        page = int(request.url.params["page"])
        ids = {1: ["1", "2"], 2: ["3", "4"], 3: ["5"]}[page]
        return httpx.Response(200, json={"totalCount": 5, "data": [{"id": i} for i in ids]})

    client, stats = make_client(handler)
    jobs, received, total, truncated = client.search("All Sydney NSW", [6144], 7)
    assert [j["id"] for j in jobs] == ["1", "2", "3", "4", "5"]
    assert (received, total, truncated, stats.requests) == (5, 5, False, 3)


def test_search_params():
    captured = {}

    def handler(request):
        captured.update(request.url.params)
        return httpx.Response(200, json={"totalCount": 0, "data": []})

    client, _ = make_client(handler)
    client.search("All Sydney NSW", [6144, 6145], 7)
    assert captured["subclassification"] == "6144,6145"
    assert captured["where"] == "All Sydney NSW"
    assert captured["daterange"] == "7"

    captured.clear()
    client.search("", [], 0)
    assert "where" not in captured and "subclassification" not in captured and "daterange" not in captured
    assert captured["classification"] == "1200"


def test_promoted_duplicates_count_toward_total():
    def handler(request):
        return httpx.Response(200, json={"totalCount": 3, "data": [{"id": "1"}, {"id": "1"}, {"id": "2"}]})

    client, stats = make_client(handler, page_size=100)
    jobs, received, total, truncated = client.search("All Sydney NSW", [6144], 7)
    assert [j["id"] for j in jobs] == ["1", "2"]
    assert (received, total, truncated, stats.requests) == (3, 3, False, 1)


def test_truncated_when_max_pages_reached():
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"totalCount": 100, "data": [{"id": f"{page}-a"}, {"id": f"{page}-b"}]})

    client, _ = make_client(handler, max_pages=3)
    jobs, received, total, truncated = client.search("All Sydney NSW", [6144], 0)
    assert (len(jobs), received, total, truncated) == (6, 6, 100, True)


def test_retry_then_success_counts_failures():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"totalCount": 1, "data": [{"id": "1"}]})

    client, stats = make_client(handler)
    jobs, *_ = client.search("All Sydney NSW", [6144], 7)
    assert len(jobs) == 1
    assert (stats.requests, stats.failed_requests) == (3, 2)


def test_gives_up_after_max_retries():
    client, stats = make_client(lambda request: httpx.Response(403), max_retries=2)
    with pytest.raises(scraper.FetchError):
        client.search("All Sydney NSW", [6144], 7)
    assert stats.failed_requests == 3


def test_collect_jobs_dedupes_across_locations_and_reports_failures():
    def handler(request):
        where = request.url.params["where"]
        if where == "Broken":
            return httpx.Response(404)
        ids = {"A": ["94927352", "2"], "B": ["94927352", "3"]}[where]
        return httpx.Response(200, json={"totalCount": 2, "data": [{**RAW_JOB, "id": i} for i in ids]})

    client, _ = make_client(handler, page_size=100)
    result = scraper.collect_jobs(client, ["A", "Broken", "B"], [6145], 7)
    assert [r["job_id"] for r in result.rows] == ["94927352", "2", "3"]
    assert [r["search_location"] for r in result.rows] == ["A", "A", "B"]
    assert result.failed_locations == 1
    assert len(result.messages) == 2 and len(result.warnings) == 1


def test_add_descriptions_via_graphql():
    def handler(request):
        job_id = json.loads(request.content)["variables"]["id"]
        if job_id == "2":
            return httpx.Response(200, json={"data": {"jobDetails": None}, "errors": [{"message": "Not found"}]})
        return httpx.Response(200, json={"data": {"jobDetails": {"job": {"id": job_id, "content": "<p>Uses Xero</p>"}}}})

    client, _ = make_client(handler)
    rows = [
        {"job_id": "1", "title": "a", "description": None},
        {"job_id": "2", "title": "b", "description": None},
        {"job_id": "3", "title": "c", "description": "already"},
    ]
    progress = []
    failed = scraper.add_descriptions(client, rows, lambda i, n, label: progress.append((i, n)))
    assert failed == 1
    assert [r["description"] for r in rows] == ["Uses Xero", None, "already"]
    assert progress == [(0, 2), (1, 2), (2, 2)]


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return type("Resp", (), {"text": response})()


class FakeGemini:
    def __init__(self, responses):
        self.models = FakeModels(responses)


AI_CFG = {"model": "test-model", "max_retries": 2, "backoff_base": 0, "concurrency": 2}


def test_format_requirements():
    assert ai.format_requirements([" 2 years experience ", "- CPA", "", "(Desirable) CIA"]) == (
        "- 2 years experience\n- CPA\n- (Desirable) CIA"
    )
    assert ai.format_requirements([]) == ""


def test_extract_requirements_parses_json():
    client = FakeGemini(['{"requirements": ["2 years experience", "(Desirable) CPA"]}'])
    assert ai.extract_requirements(client, AI_CFG, "Auditor", "text") == ["2 years experience", "(Desirable) CPA"]


def test_extract_requirements_retries_invalid_answer_then_fails():
    client = FakeGemini(["not json", '{"requirements": ["ok"]}'])
    assert ai.extract_requirements(client, AI_CFG, "Auditor", "text", sleep=lambda s: None) == ["ok"]
    assert client.models.calls == 2

    client = FakeGemini(["bad", "bad", "bad"])
    with pytest.raises(ai.AIError):
        ai.extract_requirements(client, AI_CFG, "Auditor", "text", sleep=lambda s: None)


def test_add_requirements_only_processes_rows_with_description():
    rows = [
        {"job_id": "1", "title": "a", "description": "text", "requirements": None},
        {"job_id": "2", "title": "b", "description": None, "requirements": None},
        {"job_id": "3", "title": "c", "description": "text", "requirements": "- done"},
    ]
    client = FakeGemini(['{"requirements": []}'])
    progress = []
    failed, error = ai.add_requirements(client, AI_CFG, rows, lambda i, n, label: progress.append((i, n)))
    assert (failed, error) == (0, None)
    assert [r["requirements"] for r in rows] == ["", None, "- done"]
    assert client.models.calls == 1
    assert progress == [(0, 1), (1, 1)]


GCP_VARS = ["GCP_PROJECT_ID", "GCP_LOCATION", "GCP_CLIENT_EMAIL", "GCP_PRIVATE_KEY"]


def write_env(tmp_path, monkeypatch, content):
    for var in GCP_VARS:
        monkeypatch.delenv(var, raising=False)
    env = tmp_path / ".env"
    env.write_text(content)
    monkeypatch.setattr(ai, "ENV_PATH", env)


def test_load_settings_requires_project(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, 'GCP_PROJECT_ID=""\n')
    with pytest.raises(ai.AIError, match="GCP_PROJECT_ID"):
        ai.load_settings()


def test_load_settings_defaults(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, 'GCP_PROJECT_ID="my-project"\nGCP_LOCATION=""\n')
    settings = ai.load_settings()
    assert (settings["project"], settings["location"], settings["client_email"]) == ("my-project", "global", "")


def test_service_account_credentials_from_env(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    one_line = pem.replace("\n", "\\n")
    write_env(
        tmp_path, monkeypatch,
        f'GCP_PROJECT_ID="my-project"\nGCP_CLIENT_EMAIL="bot@my-project.iam.gserviceaccount.com"\n'
        f'GCP_PRIVATE_KEY="{one_line}"\n',
    )
    creds = ai._credentials(ai.load_settings())
    assert creds.service_account_email == "bot@my-project.iam.gserviceaccount.com"
    assert creds.project_id == "my-project"


def test_invalid_private_key_gives_clear_error(tmp_path, monkeypatch):
    write_env(
        tmp_path, monkeypatch,
        'GCP_PROJECT_ID="my-project"\nGCP_CLIENT_EMAIL="bot@my-project.iam.gserviceaccount.com"\n'
        'GCP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\n\\n-----END PRIVATE KEY-----\\n"\n',
    )
    with pytest.raises(ai.AIError, match="GCP_PRIVATE_KEY"):
        ai._credentials(ai.load_settings())
