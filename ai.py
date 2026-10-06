"""Ekstraksi requirement dan software lowongan dengan Gemini di Vertex AI."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

log = logging.getLogger("seek.ai")

DESIRABLE_PREFIX = "(Desirable) "

SYSTEM_INSTRUCTION = f"""You extract candidate requirements and software from job advertisements.

"requirements": return every requirement the candidate must or should meet: experience, skills, knowledge, education, \
certifications, licences, software proficiency, eligibility (such as citizenship, work rights or clearances) \
and personal attributes.

Rules:
- Include desirable, preferred or "nice to have" qualifications, and prefix each of those with "{DESIRABLE_PREFIX}".
- Do not include job responsibilities or duties, company descriptions, benefits, salary, or application instructions.
- One requirement per item. Keep each item close to the original wording and in the original language of the ad.
- Do not invent or infer requirements that the ad does not state.
- If the ad states no requirements, return an empty list.

"software": return every software product, application, system or tool the ad names as something the \
candidate should know or will use (for example Microsoft Excel, SAP, Xero, Accurate, Power BI, ACL).

Rules:
- One entry per product, no duplicates. Write each product by its common official name \
(for example "Ms. Excel" becomes "Microsoft Excel", "MS Office" becomes "Microsoft Office").
- Only products the ad names explicitly. Do not include generic phrases such as "accounting software", \
"ERP" or "computer", and do not infer products that are not named.
- If the ad names no software, return an empty list."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "requirements": {"type": "ARRAY", "items": {"type": "STRING"}},
        "software": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["requirements", "software"],
}

RETRY_CODES = {429, 500, 502, 503, 504}

ENV_PATH = Path(__file__).with_name(".env")
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

AUTH_HELP = (
    "Kredensial Google Cloud belum ada. Isi GCP_CLIENT_EMAIL dan GCP_PRIVATE_KEY di file .env "
    "(service account), atau jalankan `gcloud auth application-default login`."
)


class AIError(Exception):
    pass


def load_settings() -> dict:
    """Baca pengaturan GCP dari .env (variabel environment yang sudah ada tidak ditimpa)."""
    load_dotenv(ENV_PATH)
    project = os.environ.get("GCP_PROJECT_ID", "").strip()
    if not project:
        raise AIError("GCP_PROJECT_ID belum diisi. Salin .env.example menjadi .env lalu isi nilainya.")
    return {
        "project": project,
        "location": os.environ.get("GCP_LOCATION", "").strip() or "global",
        "client_email": os.environ.get("GCP_CLIENT_EMAIL", "").strip(),
        # Private key di .env ditulis satu baris dengan \n; kembalikan menjadi baris baru sungguhan
        "private_key": os.environ.get("GCP_PRIVATE_KEY", "").replace("\\n", "\n").strip(),
    }


def _credentials(settings: dict):
    """Service account dari .env; jika kosong, Application Default Credentials, lalu token gcloud CLI."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials

    if settings["client_email"]:
        info = {
            "type": "service_account",
            "project_id": settings["project"],
            "client_email": settings["client_email"],
            "private_key": settings["private_key"] + "\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        try:
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except ValueError as e:
            raise AIError(f"GCP_PRIVATE_KEY di .env tidak valid: {e}") from e
    try:
        creds, _ = google.auth.default(scopes=SCOPES)
        return creds
    except DefaultCredentialsError:
        pass
    try:
        token = subprocess.run(
            ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise AIError(AUTH_HELP) from e
    if not token:
        raise AIError(AUTH_HELP)
    return Credentials(token=token)


def make_client():
    from google import genai

    settings = load_settings()
    return genai.Client(
        vertexai=True,
        project=settings["project"],
        location=settings["location"],
        credentials=_credentials(settings),
    )


def format_requirements(items: list[str]) -> str:
    cleaned = [item.strip().lstrip("-•* ").strip() for item in items]
    return "\n".join(f"- {item}" for item in cleaned if item)


def format_software(items: list[str]) -> str:
    """Gabungkan nama software menjadi satu teks, tanpa duplikat (tanpa peduli huruf besar/kecil)."""
    names: list[str] = []
    for item in items:
        name = item.strip()
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return ", ".join(names)


def extract_requirements(client, ai_cfg: dict, title: str | None, description: str, sleep=time.sleep) -> dict:
    """Kembalikan {"requirements": [...], "software": [...]} dari satu deskripsi lowongan."""
    from google.genai import errors, types

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    prompt = f"Job title: {title or '-'}\n\nJob advertisement:\n{description}"
    retries = ai_cfg["max_retries"]
    for attempt in range(retries + 1):
        try:
            resp = client.models.generate_content(model=ai_cfg["model"], contents=prompt, config=config)
            data = json.loads(resp.text or "")
            return {
                "requirements": [str(item) for item in data["requirements"]],
                "software": [str(item) for item in data.get("software") or []],
            }
        except errors.APIError as e:
            if e.code in (401, 403):
                raise AIError(f"Akses Vertex AI ditolak ({e.code}): {e.message}") from e
            if e.code not in RETRY_CODES or attempt == retries:
                raise AIError(f"Gemini error {e.code}: {e.message}") from e
            reason = f"HTTP {e.code}"
        except (ValueError, KeyError, TypeError) as e:
            if attempt == retries:
                raise AIError(f"Jawaban Gemini tidak bisa dibaca ({type(e).__name__})") from e
            reason = "jawaban tidak valid"
        wait = ai_cfg["backoff_base"] * 2**attempt
        log.warning("Gemini gagal (%s), retry %d/%d dalam %.0f detik", reason, attempt + 1, retries, wait)
        sleep(wait)
    raise AIError("unreachable")


def pending_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("description") and r.get("requirements") is None]


def add_requirements(
    client,
    ai_cfg: dict,
    rows: list[dict],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, str | None]:
    """Isi kolom requirements dan software untuk baris yang punya deskripsi tetapi belum diproses.

    Mengembalikan (jumlah gagal, pesan error terakhir).
    """
    pending = pending_rows(rows)
    failed = 0
    last_error = None
    if on_progress:
        on_progress(0, len(pending), "")
    with ThreadPoolExecutor(max_workers=ai_cfg["concurrency"]) as pool:
        futures = {
            pool.submit(extract_requirements, client, ai_cfg, row["title"], row["description"]): row
            for row in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            try:
                extracted = future.result()
                row["requirements"] = format_requirements(extracted["requirements"])
                row["software"] = format_software(extracted["software"]) or None
            except AIError as e:
                failed += 1
                last_error = str(e)
                log.warning("Requirement job %s gagal diekstrak (%s)", row["job_id"], e)
            if on_progress:
                on_progress(done, len(pending), row["title"] or row["job_id"])
    return failed, last_error
