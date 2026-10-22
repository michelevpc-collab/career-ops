import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import make_handler
from research_queue import initialize_research_schema


class ResearchQueueHttpHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "web-test.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL UNIQUE, sector TEXT, categories TEXT, hq_city TEXT, hq_region TEXT, hq_country TEXT, website TEXT, careers_url TEXT, source_url TEXT, verification_status TEXT, confidence TEXT, operating_locations TEXT, employee_count TEXT, size_band TEXT, stage TEXT, company_linkedin_url TEXT)")
            db.execute("CREATE TABLE company_sources (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, worksheet TEXT NOT NULL, worksheet_row INTEGER NOT NULL, source_name TEXT NOT NULL, source_url TEXT)")
            db.execute("INSERT INTO companies (id, name, normalized_name, sector, website) VALUES (1, 'Northstar Systems', 'northstar systems', 'Software', 'https://northstar.example')")
        initialize_research_schema(self.db_path)
        self.handler_class = make_handler(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, path, payload=None):
        handler = self.handler_class.__new__(self.handler_class)
        handler.path = path
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        captured = {}
        handler._json = lambda status, value: captured.update(status=status, payload=value)
        handler._send = lambda status, value, content_type: captured.update(status=status, body=value, content_type=content_type)
        if payload is None:
            handler.do_GET()
        else:
            handler.do_POST()
        return captured

    def test_browser_handler_completes_company_to_saved_research_flow(self):
        page = self.request("/")
        self.assertEqual(page["status"], 200)
        self.assertIn(b"Company research queue", page["body"])
        search = self.request("/api/companies?q=Northstar")["payload"]
        company_id = search["companies"][0]["id"]
        self.assertEqual(search["companies"][0]["name"], "Northstar Systems")
        detail = self.request(f"/api/companies/{company_id}")["payload"]["company"]
        self.assertIn("Careers URL", detail["missing_fields"])
        started = self.request("/api/research/start", {"company_id": company_id})
        self.assertEqual(started["status"], 201)
        saved = self.request("/api/research", {
            "company_id": company_id,
            "record_id": started["payload"]["record"]["id"],
            "findings": "User-entered finding, awaiting review.",
            "source_urls": ["https://northstar.example/news"],
            "status": "needs_review",
            "verified": False,
        })
        self.assertEqual(saved["status"], 200)
        self.assertEqual(saved["payload"]["record"]["findings"], "User-entered finding, awaiting review.")
        self.assertIsNone(saved["payload"]["record"]["verified_at"])
        final_detail = self.request(f"/api/companies/{company_id}")["payload"]["company"]
        record = final_detail["research_records"][0]
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["sources"][0]["url"], "https://northstar.example/news")


if __name__ == "__main__":
    unittest.main()
