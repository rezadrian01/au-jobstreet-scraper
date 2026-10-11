"""Log audit: satu baris per kejadian (tarikan dimulai, selesai, terputus, diunduh, dst).

Ditulis ke stderr, sehingga ikut tertangkap oleh journald, Cloud Logging, atau log Streamlit Cloud.
Formatnya `<waktu> <level> <logger> <nama_kejadian> <JSON>`, mudah dicari dengan teks biasa.
"""

from __future__ import annotations

import json
import logging
import sys

AUDIT_LOGGER = "jobscraper.audit"
APP_LOGGERS = ("jobscraper", "seek")  # "seek" dipakai modul scraper untuk peringatan request gagal

_log = logging.getLogger(AUDIT_LOGGER)


def setup() -> None:
    """Pasang handler stderr satu kali; aman dipanggil berulang (Streamlit menjalankan ulang skrip)."""
    for name in APP_LOGGERS:
        logger = logging.getLogger(name)
        if any(getattr(h, "_jobscraper", False) for h in logger.handlers):
            continue
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
        handler._jobscraper = True
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def event(name: str, **fields) -> None:
    """Catat satu kejadian audit beserta datanya."""
    _log.info("%s %s", name, json.dumps(fields, ensure_ascii=False, default=str))
