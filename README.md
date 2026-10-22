# career-ops

Career Research & Outreach Agent, starting with a researched company master and outreach tracker.

A new local-first project for company research and deliberate career outreach. The initial database is seeded from the user's company master workbook; it contains company-level records only.

## Company research queue

The first working feature is a local browser interface for searching the existing company master, viewing available company fields and missing research fields, selecting a company, and saving a research record with findings, source URLs, status, and an optional verification timestamp. Research is entered by the user; the app does not perform live web research or generate company facts.

On startup, the app adds `research_records` and `research_sources` tables with `CREATE TABLE IF NOT EXISTS`. It does not recreate, replace, or reseed the companies database.

### Run in a GitHub Codespace

From the repository root:

```bash
python3 app.py --host 0.0.0.0 --port 8000
```

In the Codespace **Ports** panel, open port `8000` in the browser preview. If Codespaces does not detect it automatically, use **Forward a Port** and enter `8000`. Stop the app with `Ctrl+C` in the terminal.

The app can also be launched locally with `python3 app.py`; it uses the same port by default. Set `PORT` or pass `--port` to choose another port. `--db /path/to/career_research.db` selects a different existing database; the app refuses to create a missing database.

### Run tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary databases and do not modify `data/career_research.db`.

## Initial company database

`data/career_research.db` is a SQLite database. It contains a normalized `companies` table and a `company_sources` table that records the source worksheet and row for each company. The seed import combines company names from:

- `Company Master`
- `Original 456 Companies`
- `Swiss Target List`
- `Swiss Institution Master`
- `Fintech 100`
- `Swiss Contacts` (organization names only)

The import produced 744 distinct company records from 1,855 worksheet rows: 1,653 identifiable company rows and 202 numeric placeholders. The placeholders were excluded because they are not company names. Individual contacts, email addresses, message content, and outreach history were not imported.

## Rebuild the database

The import script requires Python 3 with `pandas` and `openpyxl` available. No dependencies are installed by this repository.

```bash
python3 scripts/import_companies.py \
  "/path/to/Career_Ops_Complete_Company_Master_and_Outreach_Tracker_FINAL.xlsx" \
  --replace
```

`--replace` is required to overwrite an existing database. The script reads only whitelisted company-level columns from the workbook.

## Current state

This project is connected to `https://github.com/michelevpc-collab/career-ops`. The existing `career-ops-hq/career-ops` repository was not modified.
