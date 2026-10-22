# Career Research & Outreach Agent

A local-first career research workspace built on Python's standard library, SQLite, and a static browser interface. It starts from the existing 744-company master and adds company enrichment, job discovery, contacts, application/outreach history, and a reviewable workbook importer.

## Run in a GitHub Codespace

From the repository root:

```bash
python3 app.py --host 0.0.0.0 --port 8000
```

Open **Ports** in the Codespace, forward port `8000`, and click **Open in Browser**. Keep the forwarded port private because the local database can contain contact information and email history. Stop the server with `Ctrl+C`.

On its first run, the application makes a SQLite-consistent local working copy at `data/career_research.local.db` from the tracked seed `data/career_research.db`. The seed file is not changed. The local working database, import backups, contacts and email history are ignored by Git. Schema migrations are additive and the app makes a backup before applying them.

The app uses only the Python standard library. No package installation is required.

## Company research

Select a company and click **Research & Enrich Company**. The service fetches the recorded company website, follows a bounded set of same-site links, extracts conservative page metadata and links, and stores field values with source URLs, retrieval time, confidence, and verification state. Evidence is retained in the database and the profile refreshes after the run. Populated, manually verified profile fields are protected from replacement. Unavailable sources remain marked as inaccessible or unknown; a failed request is not treated as proof a fact does not exist.

For companies without a website, configure one optional web search provider in Codespaces **Secrets**:

```text
CAREER_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=<Codespaces secret>
```

Or use Tavily:

```text
CAREER_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=<Codespaces secret>
```

Do not put keys in source files or `.env` files committed to GitHub. Without a provider credential, companies with a known website are still researched; companies without one are reported as search-limited, and the app does not guess a domain.

**Enrich All Companies** covers every company row, including results beyond the first 80 shown in the browser. Work is sequential and rate-limited, checkpointed, and resumable after restart. Successful runs are cached for seven days. Pause, resume, and retry controls are available while a batch runs.

## Job discovery

Use **Discover Jobs** for the selected company, or the top-level button for all companies. The service checks the recorded careers page and supports public Greenhouse, Lever, and Ashby job-board endpoints. It stores matching roles, descriptions, locations, posting dates where supplied, canonical URLs and discovery history. A successfully fetched ATS listing can mark previously listed roles as expired when they disappear. A failed fetch does not expire jobs. Other careers pages are inspected for visible job links and the limitation is recorded.

Matching terms cover business development, partnerships, fintech, financial markets, structured products, digital assets, blockchain, AI for finance, financial data, research, strategy and investment. Job discovery is best-effort and limited to public, accessible pages; it does not bypass authentication or site protections.

## Import the company master and outreach workbook

Click **Import Company Master Workbook**, select the `.xlsx`, and review worksheet names, detected columns, row counts, company-name duplicates, likely existing matches, proposed new companies and ambiguous matches. Only clicking **Import reviewed data** writes changes.

The importer reads workbook XML without third-party packages, takes a local backup, then imports in a SQLite transaction. It adds missing companies, fills blank company fields without replacing existing values, records source worksheet provenance, imports contacts and professional email addresses, and stores available outreach/message and application records. Unmatched or ambiguous rows are retained in a local review table. Stable row fingerprints prevent duplicate message/application records on a repeat import. Database contents and personal contact/message data must not be committed or uploaded to GitHub.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, fixture workbook data, and mocked HTTP responses. They cover database preservation, search, evidence persistence, failed requests, Greenhouse job deduplication, workbook import, batch progress and retry. They do not contact live websites.

## Current scope and limitations

- The UI supports user-triggered company research and job discovery, optional Brave/Tavily website discovery, source-backed field updates, a full-list sequential batch, and local workbook import.
- Automatic extraction is conservative HTML parsing, not an AI fact verifier. Sector/city snippets and professional contacts may need user review; confidence and verification state are visible.
- Public contacts are extracted only from reachable company pages and the supplied workbook. The app does not scrape logged-in services or guess personal email addresses.
- Live external research is network-dependent. A missing key, inaccessible website, unsupported job platform, or rate limit is recorded as a limitation.
- Existing user-entered research notes remain supported. Email sending, email drafting, CV generation, PDF generation and automatic application submission are not implemented.

## Existing seed database

`data/career_research.db` is the original tracked seed, containing 744 companies, 1,653 company-source rows, and an import-run record. Do not run the legacy import script with `--replace` against it. For new workbook data, use the application importer, which writes only to the local working database.
