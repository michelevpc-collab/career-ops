#!/usr/bin/env python3
"""Run the local company research queue web app."""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from research_queue import get_company, initialize_research_schema, save_research, search_companies, start_research


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "career_research.db"
MAX_REQUEST_BYTES = 1_000_000


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
                return self._json(200, {"companies": search_companies(db_path, query, limit, offset)})
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
    if not db_path.is_file():
        parser.error(f"Database not found; refusing to create a new one: {db_path}")
    import sqlite3
    with sqlite3.connect(str(db_path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'").fetchone() is None:
            parser.error(f"Database has no companies table: {db_path}")
    initialize_research_schema(db_path)
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
