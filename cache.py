"""Cache deskripsi dan hasil AI per lowongan (SQLite), supaya tarikan ulang hanya memproses lowongan baru.

Hasil pencarian tidak pernah di-cache: daftar lowongan selalu diambil langsung dari situs. Yang dipakai ulang
hanya isi lowongan (deskripsi dan data pelengkap LinkedIn) dan hasil analisis AI, selama belum kedaluwarsa.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# Kolom pelengkap yang ikut diambil bersama deskripsi (LinkedIn) dan ikut disimpan
DETAIL_FIELDS = ("work_type", "seniority", "industry", "apply_type", "subclassification")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    site TEXT NOT NULL,
    job_id TEXT NOT NULL,
    description TEXT,
    details TEXT,
    description_at REAL,
    requirements TEXT,
    software TEXT,
    ai_version TEXT,
    ai_at REAL,
    PRIMARY KEY (site, job_id)
)
"""


class Cache:
    def __init__(self, path: str | Path, ttl_days: float = 30, clock=time.time):
        self.path = Path(path)
        self.ttl = ttl_days * 86400
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # Koneksi baru tiap operasi: aman dipakai dari beberapa sesi Streamlit (thread) sekaligus
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _fresh_after(self) -> float:
        return self.clock() - self.ttl

    def _lookup(self, site: str, job_ids: list[str], columns: str, time_column: str, extra: str = "", args=()) -> dict:
        found = {}
        with self._connect() as db:
            for start in range(0, len(job_ids), 500):
                chunk = job_ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                query = (
                    f"SELECT job_id, {columns} FROM jobs WHERE site = ? AND job_id IN ({marks}) "
                    f"AND {time_column} >= ? {extra}"
                )
                for row in db.execute(query, (site, *chunk, self._fresh_after(), *args)):
                    found[row[0]] = row[1:]
        return found

    def fill_descriptions(self, site: str, rows: list[dict]) -> int:
        """Isi deskripsi (dan data pelengkap yang masih kosong) dari cache. Mengembalikan jumlah yang terisi."""
        pending = [r for r in rows if r["description"] is None]
        found = self._lookup(site, [r["job_id"] for r in pending], "description, details", "description_at",
                             "AND description IS NOT NULL")
        for row in pending:
            if row["job_id"] in found:
                description, details = found[row["job_id"]]
                row["description"] = description
                for field, value in json.loads(details or "{}").items():
                    if value and not row.get(field):
                        row[field] = value
        return sum(r["job_id"] in found for r in pending)

    def store_descriptions(self, site: str, rows: list[dict]) -> None:
        now = self.clock()
        records = [
            (site, r["job_id"], r["description"], json.dumps({f: r.get(f) for f in DETAIL_FIELDS if r.get(f)}), now)
            for r in rows
            if r.get("description")
        ]
        with self._connect() as db:
            db.executemany(
                "INSERT INTO jobs (site, job_id, description, details, description_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(site, job_id) DO UPDATE SET description = excluded.description, "
                "details = excluded.details, description_at = excluded.description_at",
                records,
            )

    def fill_ai(self, site: str, rows: list[dict], version: str) -> int:
        """Isi requirements dan software dari cache jika dibuat dengan versi AI yang sama."""
        pending = [r for r in rows if r.get("description") and r["requirements"] is None]
        found = self._lookup(site, [r["job_id"] for r in pending], "requirements, software", "ai_at",
                             "AND ai_version = ? AND requirements IS NOT NULL", (version,))
        for row in pending:
            if row["job_id"] in found:
                row["requirements"], row["software"] = found[row["job_id"]]
        return sum(r["job_id"] in found for r in pending)

    def store_ai(self, site: str, rows: list[dict], version: str) -> None:
        now = self.clock()
        records = [
            (site, r["job_id"], r["requirements"], r.get("software"), version, now)
            for r in rows
            if r.get("requirements") is not None
        ]
        with self._connect() as db:
            db.executemany(
                "INSERT INTO jobs (site, job_id, requirements, software, ai_version, ai_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(site, job_id) DO UPDATE SET requirements = excluded.requirements, "
                "software = excluded.software, ai_version = excluded.ai_version, ai_at = excluded.ai_at",
                records,
            )
