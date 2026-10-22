# career-ops

Career Research & Outreach Agent, starting with a researched company master and outreach tracker.

A new local-first project for company research and deliberate career outreach. The initial database is seeded from the user's company master workbook; it contains company-level records only.

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

This repository is a local Git repository. No remote has been configured. The existing `career-ops-hq/career-ops` repository was not modified.
