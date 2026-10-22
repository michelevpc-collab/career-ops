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
    "notes", "priority", "fit_score", "cv_version", "next_action", "follow_up_date", "source_documents",
    "primary_email_domain", "email_count_in_archive", "first_email_date", "latest_email_date",
    "last_outgoing_subject", "reply_evidence", "outreach_status", "application_status",
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
    ("hq_region", "Headquarters region"),
    ("operating_locations", "Operating locations"),
    ("size_band", "Company size band"),
)
RESEARCH_FIELD_NAMES = tuple(field for field, _ in RESEARCH_FIELDS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try: return super().__exit__(exc_type, exc_value, traceback)
        finally: self.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=10, factory=ClosingConnection)
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
            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                run_type TEXT NOT NULL DEFAULT 'company',
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                retrieved_at TEXT NOT NULL,
                UNIQUE(company_id, run_type, retrieved_at)
            );
            CREATE INDEX IF NOT EXISTS idx_research_runs_company ON research_runs(company_id, retrieved_at DESC);
            CREATE TABLE IF NOT EXISTS research_evidence (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL,
                field_value TEXT NOT NULL,
                source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                source_kind TEXT NOT NULL DEFAULT 'official_website',
                excerpt TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_research_evidence_company ON research_evidence(company_id, field_name, retrieved_at DESC);
            CREATE TABLE IF NOT EXISTS research_source_checks (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                run_id INTEGER REFERENCES research_runs(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                retrieval_status TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_research_checks_company ON research_source_checks(company_id,retrieved_at DESC);
            CREATE TABLE IF NOT EXISTS research_field_state (
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('unknown','not_found','inaccessible','verified','unverified')),
                updated_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(company_id,field_name)
            );
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                name TEXT,
                title TEXT,
                email TEXT,
                linkedin_url TEXT,
                source_url TEXT,
                source_sheet TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                source_data_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(company_id, email)
            );
            CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
            CREATE TABLE IF NOT EXISTS outreach_history (
                id INTEGER PRIMARY KEY,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                contact_email TEXT,
                subject TEXT,
                message_text TEXT,
                sent_at TEXT,
                response_status TEXT,
                classification TEXT,
                next_action TEXT,
                source_sheet TEXT,
                source_row INTEGER,
                source_fingerprint TEXT UNIQUE,
                created_at TEXT NOT NULL,
                source_data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS application_records (
                id INTEGER PRIMARY KEY,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                role_title TEXT,
                status TEXT,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                cv_version TEXT,
                next_action TEXT,
                follow_up_at TEXT,
                source_url TEXT,
                source_sheet TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_fingerprint TEXT UNIQUE,
                source_data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS job_openings (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                location TEXT,
                requirements TEXT NOT NULL DEFAULT '',
                posted_at TEXT,
                deadline TEXT,
                canonical_url TEXT NOT NULL,
                source TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                current_status TEXT NOT NULL DEFAULT 'open',
                UNIQUE(company_id, canonical_url)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_company ON job_openings(company_id, current_status);
            CREATE TABLE IF NOT EXISTS job_discovery_runs (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                limitations_json TEXT NOT NULL DEFAULT '[]',
                jobs_found INTEGER NOT NULL DEFAULT 0,
                retrieved_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_job_runs_company ON job_discovery_runs(company_id,retrieved_at DESC);
            CREATE TABLE IF NOT EXISTS batch_jobs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                remaining INTEGER NOT NULL DEFAULT 0,
                current_company TEXT,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_job_items (
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES batch_jobs(id) ON DELETE CASCADE,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, company_id)
            );
            CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_job_items(batch_id, status, id);
            CREATE TABLE IF NOT EXISTS workbook_imports (
                id INTEGER PRIMARY KEY,
                source_filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                report_json TEXT NOT NULL,
                file_fingerprint TEXT
            );
            CREATE TABLE IF NOT EXISTS unmatched_import_rows (
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES workbook_imports(id) ON DELETE CASCADE,
                sheet_name TEXT NOT NULL,
                source_row INTEGER NOT NULL,
                record_kind TEXT NOT NULL,
                proposed_name TEXT,
                match_candidates TEXT NOT NULL DEFAULT '[]',
                row_json TEXT NOT NULL
            );
        """)
        existing_columns={row[1] for row in db.execute("PRAGMA table_info(companies)")}
        # Additional workbook fields are additive; never rebuild the company table.
        for column, kind in {
            "notes":"TEXT", "priority":"TEXT", "fit_score":"TEXT", "cv_version":"TEXT",
            "next_action":"TEXT", "follow_up_date":"TEXT", "source_documents":"TEXT",
            "primary_email_domain":"TEXT", "email_count_in_archive":"TEXT", "first_email_date":"TEXT",
            "latest_email_date":"TEXT", "last_outgoing_subject":"TEXT", "reply_evidence":"TEXT",
            "outreach_status":"TEXT", "application_status":"TEXT",
        }.items():
            if column not in existing_columns:
                db.execute(f"ALTER TABLE companies ADD COLUMN {column} {kind}")
        source_columns={row[1] for row in db.execute("PRAGMA table_info(company_sources)")}
        if "source_data_json" not in source_columns:
            db.execute("ALTER TABLE company_sources ADD COLUMN source_data_json TEXT NOT NULL DEFAULT '{}'")
        outreach_columns={row[1] for row in db.execute("PRAGMA table_info(outreach_history)")}
        if "classification" not in outreach_columns:
            db.execute("ALTER TABLE outreach_history ADD COLUMN classification TEXT")
        if "source_data_json" not in outreach_columns:
            db.execute("ALTER TABLE outreach_history ADD COLUMN source_data_json TEXT NOT NULL DEFAULT '{}'")
        for table in ("contacts","application_records"):
            columns={row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "source_data_json" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN source_data_json TEXT NOT NULL DEFAULT '{{}}'")
        app_columns={row[1] for row in db.execute("PRAGMA table_info(application_records)")}
        if "source_fingerprint" not in app_columns:
            db.execute("ALTER TABLE application_records ADD COLUMN source_fingerprint TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_source_fingerprint ON application_records(source_fingerprint) WHERE source_fingerprint IS NOT NULL")
        import_columns={row[1] for row in db.execute("PRAGMA table_info(workbook_imports)")}
        if "file_fingerprint" not in import_columns:
            db.execute("ALTER TABLE workbook_imports ADD COLUMN file_fingerprint TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_workbook_file_fingerprint ON workbook_imports(file_fingerprint) WHERE file_fingerprint IS NOT NULL")
        now=utc_now()
        db.executemany("INSERT OR IGNORE INTO research_field_state(company_id,field_name,state,updated_at) SELECT id,?,'unknown',? FROM companies",[(field,now) for field in RESEARCH_FIELD_NAMES])


def _missing_fields(company: dict) -> list[str]:
    return [label for key, label in RESEARCH_FIELDS if not company.get(key)]


def search_companies(db_path: str | Path, query: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """Search existing companies and include research status/missing fields."""
    limit = min(max(int(limit), 1), 250)
    offset = max(int(offset), 0)
    query = query.strip()
    sql = """
        SELECT c.*, COALESCE((
            SELECT rr.status FROM research_runs rr WHERE rr.company_id=c.id
            ORDER BY rr.retrieved_at DESC,rr.id DESC LIMIT 1
        ),(
            SELECT r.status FROM research_records r
            WHERE r.company_id = c.id ORDER BY r.updated_at DESC, r.id DESC LIMIT 1
        )) AS research_status
        FROM companies c
    """
    params: list[object] = []
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        sql += " WHERE c.name LIKE ? ESCAPE '\\' OR c.sector LIKE ? ESCAPE '\\' OR c.hq_city LIKE ? ESCAPE '\\' OR c.hq_country LIKE ? ESCAPE '\\' OR c.website LIKE ? ESCAPE '\\'"
        params.extend([like] * 5)
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
        company["research_evidence"]=[dict(item) for item in db.execute(
            "SELECT id,field_name,field_value,source_url,retrieved_at,confidence,verification_status,source_kind,excerpt FROM research_evidence WHERE company_id=? ORDER BY retrieved_at DESC,id DESC",(company_id,))]
        company["source_checks"]=[dict(item) for item in db.execute(
            "SELECT url,source_kind,retrieval_status,retrieved_at,detail FROM research_source_checks WHERE company_id=? ORDER BY retrieved_at DESC,id DESC LIMIT 30",(company_id,))]
        company["field_states"]={item["field_name"]:dict(item) for item in db.execute("SELECT field_name,state,updated_at,detail FROM research_field_state WHERE company_id=?",(company_id,))}
        company["research_runs"]=[dict(item) for item in db.execute(
            "SELECT id,run_type,status,message,started_at,finished_at,retrieved_at FROM research_runs WHERE company_id=? ORDER BY retrieved_at DESC,id DESC LIMIT 10",(company_id,))]
        company["contacts"]=[dict(item) for item in db.execute(
            "SELECT id,name,title,email,linkedin_url,source_url,source_sheet,confidence,verification_status,source_data_json FROM contacts WHERE company_id=? ORDER BY name,email",(company_id,))]
        company["jobs"]=[dict(item) for item in db.execute(
            "SELECT id,title,description,location,requirements,posted_at,deadline,canonical_url,source,discovered_at,current_status FROM job_openings WHERE company_id=? ORDER BY CASE current_status WHEN 'open' THEN 0 ELSE 1 END,discovered_at DESC",(company_id,))]
        company["job_discovery_runs"]=[dict(item) for item in db.execute(
            "SELECT id,status,sources_json,limitations_json,jobs_found,retrieved_at FROM job_discovery_runs WHERE company_id=? ORDER BY retrieved_at DESC LIMIT 10",(company_id,))]
        company["outreach_history"]=[dict(item) for item in db.execute(
            "SELECT id,contact_email,subject,message_text,sent_at,response_status,classification,next_action,source_sheet,source_data_json FROM outreach_history WHERE company_id=? ORDER BY sent_at DESC,id DESC LIMIT 30",(company_id,))]
        company["applications"]=[dict(item) for item in db.execute(
            "SELECT id,role_title,status,cv_version,next_action,follow_up_at,source_url,source_sheet,created_at,source_data_json FROM application_records WHERE company_id=? ORDER BY created_at DESC,id DESC",(company_id,))]
        company["research_status"]=(company.get("research_runs") or [{}])[0].get("status") or company.get("research_status")
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

