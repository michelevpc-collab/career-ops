#!/usr/bin/env python3
"""Seed the local company database from the approved workbook tabs/columns."""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "career_research.db"

# Only company-level fields are selected. Contact identities, email addresses,
# message content, and outreach/application state are deliberately excluded.
SOURCES = [
    {
        "sheet": "Company Master",
        "name": "Company",
        "columns": [
            "Company", "Sector / category", "Other categories", "HQ city",
            "HQ region/state", "HQ country", "Website", "Careers URL",
            "Source / evidence URL", "Verification status", "Confidence",
            "Other operating locations", "Company size (employees)",
            "Company size band", "Company stage", "Company LinkedIn URL",
        ],
        "map": {
            "sector": "Sector / category", "categories": "Other categories",
            "hq_city": "HQ city", "hq_region": "HQ region/state",
            "hq_country": "HQ country", "website": "Website",
            "careers_url": "Careers URL", "source_url": "Source / evidence URL",
            "verification_status": "Verification status", "confidence": "Confidence",
            "operating_locations": "Other operating locations",
            "employee_count": "Company size (employees)",
            "size_band": "Company size band", "stage": "Company stage",
            "company_linkedin_url": "Company LinkedIn URL",
        },
    },
    {
        "sheet": "Original 456 Companies", "name": "Company",
        "columns": ["Company", "Category / sector", "Other screenshot categories"],
        "map": {"sector": "Category / sector", "categories": "Other screenshot categories"},
    },
    {
        "sheet": "Swiss Target List", "name": "Azienda",
        "columns": ["Azienda", "Categoria", "Città / area", "Sito ufficiale", "Fonte"],
        "map": {"sector": "Categoria", "hq_city": "Città / area", "website": "Sito ufficiale", "source_url": "Fonte"},
        "default_country": "Switzerland",
    },
    {
        "sheet": "Swiss Institution Master", "name": "Institution",
        "columns": ["Institution", "Section", "City / Area", "Official website", "Source / verification"],
        "map": {"sector": "Section", "hq_city": "City / Area", "website": "Official website", "source_url": "Source / verification"},
        "default_country": "Switzerland",
    },
    {
        "sheet": "Fintech 100", "name": "Company (transcribed from infographic)",
        "columns": ["Company (transcribed from infographic)", "Sector", "Official website", "Careers / openings URL", "Source"],
        "map": {"sector": "Sector", "website": "Official website", "careers_url": "Careers / openings URL", "source_url": "Source"},
    },
    {
        "sheet": "Swiss Contacts", "name": "Azienda", "columns": ["Azienda"],
        "map": {}, "default_country": "Switzerland",
    },
]


def cell(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    return value or None


def is_placeholder(name: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.0)?", name))


def key_for(name: str) -> str:
    return " ".join(name.casefold().split())


def public_url(value):
    value = cell(value)
    if value and re.match(r"^https?://", value, re.IGNORECASE):
        return value
    return None


def create_schema(db):
    db.executescript("""
        PRAGMA foreign_keys = ON;
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
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            worksheet TEXT NOT NULL,
            worksheet_row INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT,
            UNIQUE(worksheet, worksheet_row)
        );
        CREATE INDEX idx_companies_country ON companies(hq_country);
        CREATE INDEX idx_companies_sector ON companies(sector);
        CREATE INDEX idx_company_sources_company ON company_sources(company_id);
        CREATE TABLE import_runs (
            id INTEGER PRIMARY KEY,
            source_filename TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            source_rows INTEGER NOT NULL,
            company_records INTEGER NOT NULL,
            excluded_numeric_placeholders INTEGER NOT NULL
        );
    """)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--replace", action="store_true", help="replace an existing database")
    args = parser.parse_args()
    workbook = args.workbook.expanduser().resolve()
    db_path = args.db.expanduser().resolve()
    if not workbook.is_file():
        parser.error(f"Workbook not found: {workbook}")
    if db_path.exists() and not args.replace:
        parser.error(f"Database already exists: {db_path}; pass --replace to rebuild")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.replace and db_path.exists():
        db_path.unlink()

    db = sqlite3.connect(db_path)
    try:
        create_schema(db)
        companies = {}
        source_rows = 0
        excluded = 0
        for spec in SOURCES:
            frame = pd.read_excel(workbook, sheet_name=spec["sheet"], usecols=lambda col: col in spec["columns"], dtype=object)
            source_rows += len(frame)
            for offset, row in frame.iterrows():
                name = cell(row.get(spec["name"]))
                if not name:
                    continue
                if is_placeholder(name):
                    excluded += 1
                    continue
                key = key_for(name)
                entry = companies.setdefault(key, {"name": name, "fields": {}, "sources": []})
                if spec["sheet"] == "Company Master":
                    entry["name"] = name
                fields = {db_field: cell(row.get(source_field)) for db_field, source_field in spec["map"].items()}
                if "source_url" in fields:
                    fields["source_url"] = public_url(fields["source_url"])
                if not fields.get("hq_country") and spec.get("default_country"):
                    fields["hq_country"] = spec["default_country"]
                for field, value in fields.items():
                    if value and not entry["fields"].get(field):
                        entry["fields"][field] = value
                src_url = fields.get("source_url")
                entry["sources"].append((spec["sheet"], int(offset) + 2, name, src_url))

        columns = ["sector", "categories", "hq_city", "hq_region", "hq_country", "website",
                   "careers_url", "source_url", "verification_status", "confidence",
                   "operating_locations", "employee_count", "size_band", "stage", "company_linkedin_url"]
        for key, entry in companies.items():
            fields = entry["fields"]
            db.execute(
                f"INSERT INTO companies (name, normalized_name, {', '.join(columns)}) VALUES ({', '.join('?' for _ in range(2 + len(columns)))})",
                [entry["name"], key, *[fields.get(c) for c in columns]],
            )
            company_id = db.execute("SELECT id FROM companies WHERE normalized_name = ?", (key,)).fetchone()[0]
            db.executemany(
                "INSERT INTO company_sources (company_id, worksheet, worksheet_row, source_name, source_url) VALUES (?, ?, ?, ?, ?)",
                [(company_id, *source) for source in entry["sources"]],
            )

        db.execute(
            "INSERT INTO import_runs (source_filename, imported_at_utc, source_rows, company_records, excluded_numeric_placeholders) VALUES (?, ?, ?, ?, ?)",
            (workbook.name, datetime.now(timezone.utc).isoformat(), source_rows, len(companies), excluded),
        )
        db.commit()
        print(f"Imported {len(companies)} distinct companies from {source_rows} source rows; excluded {excluded} numeric placeholders.")
        print(f"Database: {db_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
