import sqlite3
import tempfile
import unittest
from pathlib import Path

from research_queue import (
    get_company,
    initialize_research_schema,
    save_research,
    search_companies,
    start_research,
)


class ResearchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE companies (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    sector TEXT,
                    categories TEXT,
                    hq_city TEXT,
                    hq_region TEXT,
                    hq_country TEXT,
                    website TEXT,
                    careers_url TEXT,
                    source_url TEXT,
                    verification_status TEXT,
                    confidence TEXT,
                    operating_locations TEXT,
                    employee_count TEXT,
                    size_band TEXT,
                    stage TEXT,
                    company_linkedin_url TEXT
                );
                CREATE TABLE company_sources (
                    id INTEGER PRIMARY KEY,
                    company_id INTEGER NOT NULL,
                    worksheet TEXT NOT NULL,
                    worksheet_row INTEGER NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT
                );
                INSERT INTO companies (id, name, normalized_name, sector, hq_city, hq_country, website)
                VALUES (1, 'Northstar Systems', 'northstar systems', 'Software', 'Zurich', 'Switzerland', 'https://northstar.example');
                INSERT INTO company_sources (company_id, worksheet, worksheet_row, source_name)
                VALUES (1, 'Company Master', 2, 'Northstar Systems');
            """)
        initialize_research_schema(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_access_adds_schema_and_preserves_existing_rows(self):
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM companies").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM company_sources").fetchone()[0], 1)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("research_records", tables)
            self.assertIn("research_sources", tables)
        company = get_company(self.db_path, 1)
        self.assertEqual(company["name"], "Northstar Systems")
        self.assertEqual(company["sources"][0]["worksheet"], "Company Master")

    def test_company_search_and_missing_information_flags(self):
        matches = search_companies(self.db_path, "northstar")
        self.assertEqual([row["id"] for row in matches], [1])
        self.assertIn("Careers URL", matches[0]["missing_fields"])
        self.assertTrue(matches[0]["has_missing_research_info"])
        self.assertEqual(search_companies(self.db_path, "no-such-company"), [])

    def test_research_selection_and_record_persistence(self):
        started = start_research(self.db_path, 1)
        self.assertEqual(started["status"], "in_progress")
        saved = save_research(
            self.db_path,
            company_id=1,
            record_id=started["id"],
            findings="Company describes its work in the linked product update.",
            source_urls=["https://northstar.example/news/product-update"],
            status="needs_review",
            verified=True,
        )
        self.assertEqual(saved["findings"], "Company describes its work in the linked product update.")
        self.assertEqual(saved["status"], "needs_review")
        self.assertTrue(saved["verified_at"])
        self.assertEqual(saved["sources"][0]["url"], "https://northstar.example/news/product-update")
        company = get_company(self.db_path, 1)
        self.assertEqual(company["research_records"][0]["id"], started["id"])

    def test_invalid_source_url_is_rejected_without_saving(self):
        with self.assertRaises(ValueError):
            save_research(self.db_path, 1, "Unverified note", ["javascript:alert(1)"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM research_records").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
