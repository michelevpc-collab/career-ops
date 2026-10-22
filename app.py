#!/usr/bin/env python3
"""Run the local company research queue web app."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from research_queue import connect, get_company, initialize_research_schema, save_research, search_companies, start_research
from research_service import batch_snapshot, control_batch, discover_company_jobs, recover_interrupted_batches, research_company, start_batch
from workbook_import import import_workbook, inspect_workbook


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SOURCE_DB = ROOT / "data" / "career_research.db"
DEFAULT_DB = ROOT / "data" / "career_research.local.db"
MAX_REQUEST_BYTES = 12_000_000
PENDING_IMPORTS = {}


def backup_database(db_path: Path, suffix="pre-migration") -> Path:
    """Take a SQLite-consistent snapshot before an additive schema/import operation."""
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup=db_path.with_name(f"{db_path.name}.{suffix}-{stamp}.bak")
    source=sqlite3.connect(str(db_path)); target=sqlite3.connect(str(backup))
    try: source.backup(target)
    finally: target.close(); source.close()
    return backup


def prepare_database(requested_path: Path) -> None:
    """Create a private writable copy from the tracked company seed once, never replace it."""
    if requested_path.exists(): return
    if requested_path == DEFAULT_DB and SOURCE_DB.is_file():
        requested_path.parent.mkdir(parents=True,exist_ok=True)
        source=sqlite3.connect(str(SOURCE_DB)); target=sqlite3.connect(str(requested_path))
        try: source.backup(target)
        finally: target.close(); source.close()
        print(f"Created local working database from seed; source preserved: {SOURCE_DB}")


def make_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CareerResearchQueue/1.0"

        def log_message(self, fmt, *args):
            print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _body_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request body is too large")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body must be valid JSON") from exc

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            static_files = {
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            if parsed.path in static_files:
                filename, content_type = static_files[parsed.path]
                return self._send(200, (STATIC / filename).read_bytes(), content_type)
            if parsed.path == "/api/companies":
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0]
                try:
                    limit = int(params.get("limit", ["80"])[0])
                    offset = int(params.get("offset", ["0"])[0])
                except ValueError:
                    return self._json(400, {"error": "limit and offset must be integers"})
                with connect(db_path) as db:
                    if query:
                        escaped=query.replace("\\","\\\\").replace("%","\\%").replace("_","\\_"); like=f"%{escaped}%"
                        total=db.execute("SELECT count(*) FROM companies c WHERE c.name LIKE ? ESCAPE '\\' OR c.sector LIKE ? ESCAPE '\\' OR c.hq_city LIKE ? ESCAPE '\\' OR c.hq_country LIKE ? ESCAPE '\\' OR c.website LIKE ? ESCAPE '\\'",[like]*5).fetchone()[0]
                    else: total=db.execute("SELECT count(*) FROM companies").fetchone()[0]
                return self._json(200, {"companies": search_companies(db_path, query, limit, offset),"total":total})
            if parsed.path == "/api/stats":
                with connect(db_path) as db:
                    stats={"companies":db.execute("SELECT count(*) FROM companies").fetchone()[0],"contacts":db.execute("SELECT count(*) FROM contacts").fetchone()[0],"jobs":db.execute("SELECT count(*) FROM job_openings WHERE current_status='open'").fetchone()[0]}
                    latest=db.execute("SELECT id FROM batch_jobs ORDER BY id DESC LIMIT 1").fetchone()
                stats["batch"]=batch_snapshot(db_path,latest[0]) if latest else None
                return self._json(200,stats)
            if parsed.path == "/api/import/unmatched":
                params=parse_qs(parsed.query)
                try:
                    import_id=int(params.get("import_id",[""])[0]); offset=max(0,int(params.get("offset",["0"])[0])); limit=min(200,max(1,int(params.get("limit",["50"])[0])))
                except ValueError: return self._json(400,{"error":"import_id, offset and limit must be integers"})
                with connect(db_path) as db:
                    total=db.execute("SELECT count(*) FROM unmatched_import_rows WHERE import_id=?",(import_id,)).fetchone()[0]
                    rows=db.execute("SELECT id,sheet_name,source_row,record_kind,proposed_name,match_candidates,row_json FROM unmatched_import_rows WHERE import_id=? ORDER BY sheet_name,source_row,id LIMIT ? OFFSET ?",(import_id,limit,offset)).fetchall()
                allowed={"contact name","contact person","recipient display names observed","email","email address","contact email","contact / recipient email","role title","primary target role","application status","outreach status","next action","institution","company","azienda","contact / company"}
                items=[]
                for row in rows:
                    try: source_row=json.loads(row[6])
                    except (TypeError,json.JSONDecodeError): source_row={}
                    review_values={key:value for key,value in source_row.items() if re.sub(r"[^a-z0-9]+"," ",str(key).lower()).strip() in allowed and value not in (None,"")}
                    items.append({"id":row[0],"sheet":row[1],"row":row[2],"kind":row[3],"name":row[4],"candidates":json.loads(row[5] or "[]"),"review_values":review_values})
                return self._json(200,{"import_id":import_id,"unmatched":items,"total":total,"offset":offset,"limit":limit})
            batch_match=re.fullmatch(r"/api/batches/(\d+)",parsed.path)
            if batch_match:
                batch=batch_snapshot(db_path,int(batch_match.group(1)))
                return self._json(200,{"batch":batch}) if batch else self._json(404,{"error":"Batch not found"})
            company_match = re.fullmatch(r"/api/companies/(\d+)", parsed.path)
            if company_match:
                company = get_company(db_path, int(company_match.group(1)))
                if company is None:
                    return self._json(404, {"error": "Company not found"})
                return self._json(200, {"company": company})
            return self._json(404, {"error": "Not found"})

        def do_POST(self):
            try:
                payload = self._body_json()
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if self.path == "/api/research/start":
                    record = start_research(db_path, int(payload.get("company_id", "")))
                    return self._json(201, {"record": record})
                if self.path == "/api/research/enrich":
                    company_id=int(payload.get("company_id",""))
                    batch=start_batch(db_path,job_type="research",company_ids=[company_id])
                    return self._json(202,{"batch":batch})
                if self.path == "/api/jobs/discover":
                    company_id=int(payload.get("company_id",""))
                    batch=start_batch(db_path,job_type="jobs",company_ids=[company_id])
                    return self._json(202,{"batch":batch})
                if self.path == "/api/batches":
                    job_type=str(payload.get("job_type","research"))
                    if job_type not in {"research","jobs"}: raise ValueError("job_type must be research or jobs")
                    ids=payload.get("company_ids")
                    if ids is not None and (not isinstance(ids,list) or len(ids)>1000): raise ValueError("company_ids must be a list of at most 1000 ids")
                    return self._json(202,{"batch":start_batch(db_path,job_type=job_type,company_ids=ids)})
                batch_control=re.fullmatch(r"/api/batches/(\d+)/(pause|resume|retry)",self.path)
                if batch_control:
                    batch=control_batch(db_path,int(batch_control.group(1)),batch_control.group(2))
                    return self._json(200,{"batch":batch}) if batch else self._json(404,{"error":"Batch not found"})
                if self.path == "/api/import/preview":
                    filename=str(payload.get("filename","workbook.xlsx"))[:240]
                    encoded=payload.get("workbook_base64")
                    if not isinstance(encoded,str) or len(encoded)>10_000_000: raise ValueError("Upload a workbook under 7 MB")
                    try: raw=base64.b64decode(encoded,validate=True)
                    except Exception as exc: raise ValueError("Workbook upload was not valid base64") from exc
                    if not raw.startswith(b"PK"): raise ValueError("Upload must be an .xlsx workbook")
                    preview=inspect_workbook(raw,db_path)
                    token=secrets.token_urlsafe(20)
                    PENDING_IMPORTS[token]=(filename,raw,datetime.now(timezone.utc).timestamp())
                    return self._json(200,{"token":token,"preview":preview})
                if self.path == "/api/import/commit":
                    token=str(payload.get("token","")); pending=PENDING_IMPORTS.get(token)
                    if not pending: raise ValueError("Import preview expired; upload and preview the workbook again")
                    filename,raw,created=pending
                    if datetime.now(timezone.utc).timestamp()-created>1800:
                        del PENDING_IMPORTS[token]; raise ValueError("Import preview expired; upload and preview the workbook again")
                    backup=backup_database(db_path,"pre-import")
                    report=import_workbook(db_path,raw,filename,backup_path=None)
                    del PENDING_IMPORTS[token]
                    return self._json(200,{"report":report,"backup":"A local backup was created before import."})
                if self.path == "/api/research":
                    raw_sources = payload.get("source_urls", [])
                    if isinstance(raw_sources, str):
                        raw_sources = raw_sources.splitlines()
                    if not isinstance(raw_sources, list) or not all(isinstance(url, str) for url in raw_sources):
                        raise ValueError("source_urls must be a list of URLs or newline-separated text")
                    raw_record_id = payload.get("record_id")
                    record = save_research(
                        db_path=db_path,
                        company_id=int(payload.get("company_id", "")),
                        record_id=int(raw_record_id) if raw_record_id not in (None, "") else None,
                        findings=str(payload.get("findings", "")),
                        source_urls=raw_sources,
                        status=str(payload.get("status", "needs_review")),
                        verified=bool(payload.get("verified", False)),
                    )
                    return self._json(200, {"record": record})
                return self._json(404, {"error": "Not found"})
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    prepare_database(db_path)
    if not db_path.is_file():
        parser.error(f"Database not found; refusing to create a new one: {db_path}")
    import sqlite3
    with sqlite3.connect(str(db_path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'").fetchone() is None:
            parser.error(f"Database has no companies table: {db_path}")
    db=sqlite3.connect(str(db_path))
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns={r[1] for r in db.execute("PRAGMA table_info(companies)")}
        required_columns={"notes","priority","fit_score","cv_version","next_action","follow_up_date","source_documents","primary_email_domain","email_count_in_archive","first_email_date","latest_email_date","last_outgoing_subject","reply_evidence","outreach_status","application_status"}
        source_columns={r[1] for r in db.execute("PRAGMA table_info(company_sources)")}
        contact_columns={r[1] for r in db.execute("PRAGMA table_info(contacts)")}
        outreach_columns={r[1] for r in db.execute("PRAGMA table_info(outreach_history)")}
        app_columns={r[1] for r in db.execute("PRAGMA table_info(application_records)")}
        import_columns={r[1] for r in db.execute("PRAGMA table_info(workbook_imports)")}
        migrated={"research_evidence","contacts","job_openings","batch_jobs","research_field_state","job_discovery_runs"}.issubset(tables) and required_columns.issubset(columns)
        migrated=migrated and all("source_data_json" in cols for cols in (source_columns,contact_columns,outreach_columns,app_columns))
        migrated=migrated and "file_fingerprint" in import_columns and "source_fingerprint" in app_columns
    finally:
        db.close()
    if not migrated:
        backup_database(db_path,"pre-migration")
    initialize_research_schema(db_path)
    recover_interrupted_batches(db_path)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(db_path))
    print(f"Company research queue listening on http://{args.host}:{args.port}")
    print(f"Database: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
