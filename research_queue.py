"""Database operations for the company research queue.

Schema changes are additive. Existing company and provenance records are read
in place and are never rebuilt or replaced by this module.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_STATUSES = {"queued", "in_progress", "researched", "needs_review"}
COMPANY_COLUMNS = (
    "name", "sector", "categories", "hq_city", "hq_region", "hq_country",
    "website", "careers_url", "source_url", "verification_status", "confidence",
    "operating_locations", "employee_count", "size_band", "stage", "company_linkedin_url",
)
RESEARCH_FIELDS = (
    ("sector", "Sector"),
    ("hq_city", "Headquarters city"),
    ("hq_country", "Headquarters country"),
    ("website", "Website"),
    ("careers_url", "Careers URL"),
    ("company_linkedin_url", "Company LinkedIn"),
    ("employee_count", "Company size"),
    ("stage", "Company stage"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_research_schema(db_path: str | Path) -> None:
    """Create research tables and indexes without touching existing data."""
    with connect(db_path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS research_records (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'in_progress', 'researched', 'needs_review')),
                findings TEXT NOT NULL DEFAULT '',
                verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_sources (
                id INTEGER PRIMARY KEY,
                research_record_id INTEGER NOT NULL
                    REFERENCES research_records(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                verified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_research_records_company
                ON research_records(company_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_research_records_status
                ON research_records(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_research_sources_record
                ON research_sources(research_record_id);
        """)


def _missing_fields(company: dict) -> list[str]:
    return [label for key, label in RESEARCH_FIELDS if not company.get(key)]


def search_companies(db_path: str | Path, query: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """Search existing companies and include research status/missing fields."""
    limit = min(max(int(limit), 1), 250)
    offset = max(int(offset), 0)
    query = query.strip()
    sql = """
        SELECT c.*, (
            SELECT r.status FROM research_records r
            WHERE r.company_id = c.id ORDER BY r.updated_at DESC, r.id DESC LIMIT 1
        ) AS research_status
        FROM companies c
    """
    params: list[object] = []
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        sql += " WHERE c.name LIKE ? ESCAPE '\\' OR c.sector LIKE ? ESCAPE '\\' OR c.hq_city LIKE ? ESCAPE '\\' OR c.hq_country LIKE ? ESCAPE '\\'"
        params.extend([like] * 4)
    sql += " ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect(db_path) as db:
        rows = db.execute(sql, params).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["missing_fields"] = _missing_fields(item)
        item["has_missing_research_info"] = bool(item["missing_fields"])
        results.append(item)
    return results


def get_company(db_path: str | Path, company_id: int) -> dict | None:
    with connect(db_path) as db:
        row = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if row is None:
            return None
        company = dict(row)
        company["missing_fields"] = _missing_fields(company)
        company["has_missing_research_info"] = bool(company["missing_fields"])
        company["sources"] = [dict(source) for source in db.execute(
            "SELECT worksheet, worksheet_row, source_name, source_url FROM company_sources WHERE company_id = ? ORDER BY worksheet, worksheet_row",
            (company_id,),
        )]
        company["research_records"] = [get_research_record(db_path, record[0], connection=db) for record in db.execute(
            "SELECT id FROM research_records WHERE company_id = ? ORDER BY updated_at DESC, id DESC", (company_id,),
        )]
        return company


def get_research_record(db_path: str | Path, record_id: int, connection=None) -> dict | None:
    owns_connection = connection is None
    db = connection or connect(db_path)
    try:
        row = db.execute("SELECT * FROM research_records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["sources"] = [dict(source) for source in db.execute(
            "SELECT id, url, label, added_at, verified_at FROM research_sources WHERE research_record_id = ? ORDER BY id",
            (record_id,),
        )]
        return result
    finally:
        if owns_connection:
            db.close()


def start_research(db_path: str | Path, company_id: int) -> dict:
    now = utc_now()
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise ValueError("Company not found")
        existing = db.execute(
            "SELECT id FROM research_records WHERE company_id = ? AND status IN ('queued', 'in_progress', 'needs_review') ORDER BY updated_at DESC, id DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        if existing:
            db.execute("UPDATE research_records SET status = 'in_progress', updated_at = ? WHERE id = ?", (now, existing[0]))
            record_id = existing[0]
        else:
            cursor = db.execute(
                "INSERT INTO research_records (company_id, status, created_at, updated_at) VALUES (?, 'in_progress', ?, ?)",
                (company_id, now, now),
            )
            record_id = cursor.lastrowid
        return get_research_record(db_path, record_id, connection=db)


def save_research(
    db_path: str | Path,
    company_id: int,
    findings: str,
    source_urls: list[str],
    status: str = "needs_review",
    verified: bool = False,
    record_id: int | None = None,
) -> dict:
    if status not in ALLOWED_STATUSES:
        raise ValueError("Invalid research status")
    sources = []
    for raw_url in source_urls:
        url = raw_url.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Source must be an http(s) URL: {url}")
        if url not in sources:
            sources.append(url)
    now = utc_now()
    verified_at = now if verified else None
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise ValueError("Company not found")
        if record_id is not None:
            existing = db.execute("SELECT company_id FROM research_records WHERE id = ?", (record_id,)).fetchone()
            if existing is None or existing[0] != company_id:
                raise ValueError("Research record not found for this company")
            db.execute(
                "UPDATE research_records SET status = ?, findings = ?, verified_at = ?, updated_at = ? WHERE id = ?",
                (status, findings.strip(), verified_at, now, record_id),
            )
        else:
            cursor = db.execute(
                "INSERT INTO research_records (company_id, status, findings, verified_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (company_id, status, findings.strip(), verified_at, now, now),
            )
            record_id = cursor.lastrowid
        db.execute("DELETE FROM research_sources WHERE research_record_id = ?", (record_id,))
        db.executemany(
            "INSERT INTO research_sources (research_record_id, url, added_at, verified_at) VALUES (?, ?, ?, ?)",
            [(record_id, url, now, verified_at) for url in sources],
        )
        return get_research_record(db_path, record_id, connection=db)


def company_fields(company: dict) -> list[tuple[str, object]]:
    """Return the available company values in a stable presentation order."""
    return [(column.replace("_", " ").title(), company.get(column)) for column in COMPANY_COLUMNS]

