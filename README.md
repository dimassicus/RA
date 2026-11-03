# RA

Local PostgreSQL-backed pilot for multilingual corporate credit monitoring.

- [Research and product specification](RESEARCH_SPEC.md)
- [Initial company list](companies.xlsx)
- [Source registry](sources/README.md)
- [Parallel Perplexity, Gemini and Kimi API proposal](sources/ai-provider-options.md)

The first test case is Fertilizantes Heringer S.A. in Brazil. The workbook now has companies from Brazil, Egypt, Greece, Vietnam, Azerbaijan, Uzbekistan and Turkey. The intended scale is 100–200 companies across multiple countries and industries.

## Run locally

PostgreSQL must be running and the database name must be exactly `RA` (uppercase). On the current Mac it has already been created. From this folder:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m ra.bootstrap
.venv/bin/python scripts/serve_local.py start
```

Open <http://127.0.0.1:8765/>. `scripts/serve_local.py status` checks the server and `scripts/serve_local.py stop` stops it. The script keeps the development server running after the shell closes; it does not configure login startup. The app binds to the local machine only. To use a different connection or port, set `RA_DATABASE_URL` (libpq connection URI) or `RA_PORT` in the shell. Keep any database password out of Git. Production hosting will need a WSGI server, authentication, backups, and a scheduled worker.

Offline translation models download automatically from the official Argos package index when a watchlist country needs them or a new article language is encountered. The initial download can take several minutes and uses local disk space. To translate older saved leads after models finish installing, run `.venv/bin/python -m ra.translation`.

The app reads the company workbook with separate **Registration number** and **Tax ID** columns, plus full and short names, aliases and legal address. It also accepts the older combined `Registration number/tax ID` column and splits clear pairs for review. A single identifier of uncertain type stays in Registration number until checked. Workbook imports, manual additions and one-off subjects offered for saving enter an **identity review queue** first. A background preliminary check compares names, countries and identifiers in GLEIF and, for Brazil, BrasilAPI; it proposes empty-field values only after a strong match and shows early news leads. The analyst can correct the fields, double-check identity and confirm before a company is created or an existing record updated. Run the app after upgrading so `db.init_schema()` migrates existing company and draft identifiers; then use **Stage workbook for review** on the dashboard. The app also supports edit/remove/restore, whole-list search, dropdown search, and one-time search. Completed searches have Excel and PDF **lead reports**. Removal archives the watchlist entry while preserving search history.

The pilot currently collects public Google News RSS leads and Heringer's official investor-relations notices. Google News uses local editions for the current watchlist countries. It stores headlines, source links, dates, and simple review keywords. The run page lets an analyst select leads and build a selected-lead Excel or PDF report. Headlines are classified by their actual language, independently of the feed locale. The app discovers several substantial or official languages per watchlist country from CLDR data, downloads available direct Argos/OPUS-MT → English models from the official package index in the background when a country is added, and can install a newly encountered language during a search. This keeps translation local and avoids per-headline AI API charges. The original headline stays visible. The packaged models derive from OPUS-MT work by Jörg Tiedemann and Santhosh Thottingal, licensed CC BY 4.0. Not every language has a direct Argos model (notably Uzbek and Kazakh); those are shown as unavailable instead of falsely labelled as still pending. Publisher brands are retained as names. These are **unverified leads**, not a completed credit assessment. Document-level analysis, country-specific daily adapters, scheduled delivery, and email are later work; the [source registry](sources/README.md) records available inputs and prices.
