import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
import glints
import jobspy_sources
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


def test_add_requirements_leaves_software_none_when_ad_names_none():
    rows = [{"job_id": "1", "title": "a", "description": "text", "requirements": None, "software": None}]
    ai.add_requirements(FakeGemini(['{"requirements": ["CPA"], "software": []}']), AI_CFG, rows)
    assert rows[0]["requirements"] == "- CPA" and rows[0]["software"] is None


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
    client = FakeGemini(['{"requirements": ["2 years experience", "(Desirable) CPA"], "software": ["SAP"]}'])
    assert ai.extract_requirements(client, AI_CFG, "Auditor", "text") == {
        "requirements": ["2 years experience", "(Desirable) CPA"],
        "software": ["SAP"],
    }


def test_format_software_dedupes_case_insensitively():
    assert ai.format_software(["Microsoft Excel", " SAP ", "microsoft excel", ""]) == "Microsoft Excel, SAP"
    assert ai.format_software([]) == ""


def test_extract_requirements_retries_invalid_answer_then_fails():
    client = FakeGemini(["not json", '{"requirements": ["ok"]}'])
    assert ai.extract_requirements(client, AI_CFG, "Auditor", "text", sleep=lambda s: None) == {"requirements": ["ok"], "software": []}
    assert client.models.calls == 2

    client = FakeGemini(["bad", "bad", "bad"])
    with pytest.raises(ai.AIError):
        ai.extract_requirements(client, AI_CFG, "Auditor", "text", sleep=lambda s: None)


def test_add_requirements_only_processes_rows_with_description():
    rows = [
        {"job_id": "1", "title": "a", "description": "text", "requirements": None, "software": None},
        {"job_id": "2", "title": "b", "description": None, "requirements": None, "software": None},
        {"job_id": "3", "title": "c", "description": "text", "requirements": "- done", "software": "SAP"},
    ]
    client = FakeGemini(['{"requirements": [], "software": ["Xero", "MYOB"]}'])
    progress = []
    failed, error = ai.add_requirements(client, AI_CFG, rows, lambda i, n, label: progress.append((i, n)))
    assert (failed, error) == (0, None)
    assert [r["requirements"] for r in rows] == ["", None, "- done"]
    assert [r["software"] for r in rows] == ["Xero, MYOB", None, "SAP"]
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


LINKEDIN_RECORD = {
    "id": "li-4473870600",
    "title": "Internal Auditor",
    "company": "PT Contoh",
    "location": "Jakarta, Indonesia",
    "date_posted": __import__("datetime").date(2026, 10, 3),
    "job_type": "fulltime",
    "is_remote": False,
    "min_amount": float("nan"),
    "max_amount": float("nan"),
    "job_function": None,
    "description": "**Requirements**\n\n* 2 years\\-experience\n* Proficient in SAP",
    "job_url": "https://www.linkedin.com/jobs/view/4473870600",
}


def test_linkedin_clean_markdown():
    assert jobspy_sources.clean_markdown("**Requirements**\n\n* 2 years\\-experience\n+ CPA\n## About") == (
        "Requirements\n- 2 years-experience\n- CPA\nAbout"
    )
    assert jobspy_sources.clean_markdown(None) is None


def test_linkedin_parse_job():
    row = jobspy_sources.parse_job(LINKEDIN_RECORD, "Indonesia", "t")
    assert set(row) == set(scraper.FIELDS)
    assert row["job_id"] == "li-4473870600"
    assert row["listed_at"] == "2026-10-03"
    assert row["work_type"] == "Full time"
    assert row["work_arrangement"] is None and row["salary"] is None and row["subclassification"] is None
    assert row["description"] == "Requirements\n- 2 years-experience\n- Proficient in SAP"


def test_linkedin_salary_formatting():
    record = {**LINKEDIN_RECORD, "min_amount": 8000000.0, "max_amount": 12000000.0, "currency": "IDR", "interval": "monthly"}
    assert jobspy_sources.parse_job(record, "Indonesia", "t")["salary"] == "IDR 8,000,000 – 12,000,000 monthly"


def test_linkedin_collect_pages_dedupes_and_warns(monkeypatch):
    import jobspy
    import pandas as pd

    calls = []
    pages = {
        ("audit", 0): ["li-1", "li-2"], ("audit", 2): ["li-2", "li-3"],
        ("auditor", 0): ["li-3"],
        ("big", 0): ["li-5", "li-6"], ("big", 2): ["li-7", "li-8"],
    }

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        if kwargs["search_term"] == "boom":
            raise RuntimeError("429")
        ids = pages[(kwargs["search_term"], kwargs["offset"])]
        return pd.DataFrame([{**LINKEDIN_RECORD, "id": i} for i in ids])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    site = {"all_locations_label": "Indonesia", "batch_size": 2, "jobspy_site": "linkedin"}
    progress = []
    result = jobspy_sources.collect_jobs(
        site, ["audit", "auditor", "boom", "big"], [], 7, 4, lambda i, n, label: progress.append((i, n)), sleep=lambda s: None
    )
    assert sum(c["search_term"] == "boom" for c in calls) == jobspy_sources.BATCH_RETRIES + 1
    assert [r["job_id"] for r in result.rows] == ["li-1", "li-2", "li-3", "li-5", "li-6", "li-7", "li-8"]
    assert calls[0]["location"] == "Indonesia" and calls[0]["hours_old"] == 168 and calls[0]["fetch_description"] is False
    assert result.messages[0] == 'Indonesia · "audit": 3 job'
    assert result.failed_locations == 1
    # "audit" dan "big" berhenti di max_results (terpotong), "boom" gagal
    assert len(result.warnings) == 3 and sum("berhenti di batas 4 hasil" in w for w in result.warnings) == 2
    assert progress[0] == (0, 16) and progress[-1] == (1, 1)


def test_indeed_collect_passes_country_and_keeps_descriptions(monkeypatch):
    import jobspy
    import pandas as pd

    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{**LINKEDIN_RECORD, "id": "in-1", "location": "Jakarta, JW, ID"}])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    site = {"all_locations_label": "Indonesia", "batch_size": 50, "jobspy_site": "indeed", "country_indeed": "Indonesia"}
    result = jobspy_sources.collect_jobs(site, ["audit"], [], 3, 100)
    assert calls[0]["site_name"] == ["indeed"] and calls[0]["country_indeed"] == "Indonesia" and calls[0]["hours_old"] == 72
    assert [r["job_id"] for r in result.rows] == ["in-1"]
    assert result.rows[0]["description"].startswith("Requirements")


LINKEDIN_DETAIL_HTML = """<section>
<div class="show-more-less-html__markup x"><p>Uses <b>Xero</b></p><ul><li>CPA</li></ul></div>
<ul><li class="description__job-criteria-item"><h3> Seniority level </h3><span> Not Applicable </span></li>
<li class="description__job-criteria-item"><h3> Employment type </h3><span> Full-time </span></li>
<li class="description__job-criteria-item"><h3> Industries </h3><span> Banking </span></li></ul>
<a data-tracking-control-name="public_jobs_apply-link-offsite">Apply</a></section>"""


def test_linkedin_parse_details():
    details = jobspy_sources.parse_details(LINKEDIN_DETAIL_HTML)
    assert details == {
        "description": "Uses Xero\n- CPA",
        "work_type": "Full-time",
        "industry": "Banking",
        "apply_type": "External (situs perusahaan)",
    }
    easy = jobspy_sources.parse_details(LINKEDIN_DETAIL_HTML.replace("apply-link-offsite", "apply-link-onsite"))
    assert easy["apply_type"] == "Easy Apply"
    with pytest.raises(scraper.FetchError):
        jobspy_sources.parse_details("<html>login wall</html>")


def test_linkedin_add_descriptions_fills_details():
    def handler(request):
        if request.url.path.endswith("/2"):
            return httpx.Response(404)
        return httpx.Response(200, text=LINKEDIN_DETAIL_HTML)

    client = httpx.Client(base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler))
    rows = [
        scraper.new_row(job_id="li-1", title="a"),
        scraper.new_row(job_id="li-2", title="b"),
        scraper.new_row(job_id="li-3", title="c", description="already"),
    ]
    failed = jobspy_sources.add_descriptions(make_cfg(), {"base_url": "https://www.linkedin.com"}, rows, sleep=lambda s: None, client=client)
    assert failed == 1
    assert [r["description"] for r in rows] == ["Uses Xero\n- CPA", None, "already"]
    assert (rows[0]["work_type"], rows[0]["industry"], rows[0]["apply_type"]) == ("Full-time", "Banking", "External (situs perusahaan)")
    assert rows[2]["work_type"] is None


def test_new_row_rejects_unknown_columns():
    assert set(scraper.new_row(job_id="1")) == set(scraper.FIELDS)
    with pytest.raises(KeyError):
        scraper.new_row(nope=1)


def test_suggest_locations():
    def handler(request):
        body = json.loads(request.content)
        assert body["variables"] == {"query": "jak", "count": 8, "recentLocation": "", "locale": "en-ID", "country": "ID"}
        return httpx.Response(200, json={"data": {"searchLocationsSuggest": {"suggestions": [{"text": "Jakarta Raya"}, {}, {"text": "Jakarta Selatan Jakarta Raya"}]}}})

    site = {"base_url": "https://id.jobstreet.com", "locale": "en-ID", "country_code": "ID"}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert scraper.suggest_locations(site, {"user_agent": "test"}, " jak ", client=client) == ["Jakarta Raya", "Jakarta Selatan Jakarta Raya"]
    assert scraper.suggest_locations(site, {"user_agent": "test"}, "j", client=client) == []
    broken = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, text="oops")))
    assert scraper.suggest_locations(site, {"user_agent": "test"}, "jak", client=broken) == []


def test_filter_title_matches_any_keyword_as_substring():
    rows = [{"title": "Senior Auditor"}, {"title": "Inventory Control Staff"}, {"title": "TAX Consultant"}, {"title": None}]
    assert [r["title"] for r in scraper.filter_title(rows, ["audit", "tax"])] == ["Senior Auditor", "TAX Consultant"]
    assert scraper.filter_title(rows, []) == rows


GLINTS_SITE = {"actor": "user~actor", "country": "Indonesia", "all_locations_label": "Semua kota di Indonesia"}

GLINTS_ITEM = {
    "platform_url": "https://glints.com/id/opportunities/jobs/audit-warehouse/753cbcb8-d23b",
    "title": "Audit Warehouse",
    "posted_date": "2099-01-02T02:42:18.547475Z",
    "location": {"raw": "Penjaringan", "country": "Indonesia"},
    "is_remote": False,
    "description": "Job description\n- Stock opname",
    "job_type": "Full-time",
    "job_function": "Auditor",
    "work_mode": "",
    "salary_period": "monthly",
    "salary_minimum": 5500000,
    "salary_maximum": 6500000,
    "salary_currency": "IDR",
    "company_name": "PT Sumber Sejahtera",
}


def test_glints_parse_job():
    row = glints.parse_job(GLINTS_ITEM, "Semua kota di Indonesia", "t")
    assert set(row) == set(scraper.FIELDS)
    assert row["job_id"] == "gl-753cbcb8-d23b"
    assert row["salary"] == "IDR 5,500,000 – 6,500,000 monthly"
    assert row["listed_at"] == "2099-01-02T02:42:18+00:00"
    assert (row["location"], row["subclassification"], row["work_arrangement"]) == ("Penjaringan", "Auditor", None)


def test_glints_collect_runs_actor_filters_old_and_reports_failure():
    started = []

    def handler(request):
        path = request.url.path
        if path.endswith("/acts/user~actor/runs"):
            body = json.loads(request.content)
            started.append(body)
            if body["keyword"] == "boom":
                return httpx.Response(402)
            return httpx.Response(201, json={"data": {"id": "run1", "status": "READY", "defaultDatasetId": "ds1"}})
        if path.endswith("/actor-runs/run1"):
            return httpx.Response(200, json={"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})
        if path.endswith("/datasets/ds1/items"):
            old = {**GLINTS_ITEM, "platform_url": "https://glints.com/x/old", "posted_date": "2020-01-01T00:00:00Z"}
            return httpx.Response(200, json=[GLINTS_ITEM, GLINTS_ITEM, old])
        return httpx.Response(404)

    client = httpx.Client(base_url=glints.APIFY_BASE, transport=httpx.MockTransport(handler))
    result = glints.collect_jobs(GLINTS_SITE, ["audit", "boom"], [], 7, 50, client=client, sleep=lambda s: None)
    assert [r["job_id"] for r in result.rows] == ["gl-753cbcb8-d23b"]
    assert started[0]["max_results"] == 50 and started[0]["country"] == "Indonesia" and "location" not in started[0]
    assert len(started[0]["posted_since"]) == 10
    assert "1 lowongan yang diposting lebih lama dibuang" in result.messages[0]
    assert result.failed_locations == 1 and "Kredit Apify" in result.warnings[0]


def test_glints_missing_token_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(glints, "ENV_PATH", tmp_path / ".env")
    result = glints.collect_jobs(GLINTS_SITE, ["audit"], [], 7, 50)
    assert result.rows == [] and "APIFY_TOKEN" in result.warnings[0]


def test_jobspy_batch_retries_when_error_logged_with_empty_result(monkeypatch):
    import logging

    import jobspy
    import pandas as pd

    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            logging.getLogger("JobSpy:Indeed").error("Indeed: Connection reset by peer")
            return pd.DataFrame()
        return pd.DataFrame([{**LINKEDIN_RECORD, "id": "in-1"}])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    site = {"all_locations_label": "Indonesia", "batch_size": 50, "jobspy_site": "indeed", "country_indeed": "Indonesia"}
    result = jobspy_sources.collect_jobs(site, ["audit"], [], 3, 100, sleep=lambda s: None)
    assert len(calls) == 3 and [r["job_id"] for r in result.rows] == ["in-1"]
    assert result.warnings == [] and result.failed_locations == 0
