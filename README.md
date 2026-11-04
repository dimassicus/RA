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

Open <http://127.0.0.1:8765/>. `scripts/serve_local.py status` checks the server and `scripts/serve_local.py stop` stops it. For automatic startup after Mac login (including the first login after a restart), install the user LaunchAgent once:

```bash
.venv/bin/python scripts/serve_local.py install-service
```

The service waits for PostgreSQL, reads the local API keys, keeps a stable session secret, and restarts RA after a crash. PostgreSQL must also start at login; this Mac already has that enabled in Postgres.app. Logs are in `.local/ra_server.log`; the installed service is `~/Library/LaunchAgents/com.ra.credit-monitor.plist`. `stop` unloads the service for the current login session, and `start` loads it again. To disable login startup permanently, run `launchctl disable gui/$(id -u)/com.ra.credit-monitor` then `scripts/serve_local.py stop` with the virtualenv Python; rerun `install-service` to re-enable. No API keys are stored in the plist. The login service uses the default local database and port; shell-only overrides below apply to a manually started server without an installed LaunchAgent.

Without the LaunchAgent, the script keeps the development server running after the shell closes. The app binds to the local machine only. To use a different connection or port, set `RA_DATABASE_URL` (libpq connection URI) or `RA_PORT` in the shell. Keep any database password out of Git. Production hosting will need a WSGI server, authentication, backups, and a scheduled worker.

## News API keys

GNews and TheNewsAPI are optional news sources. From the RA folder, create a local key file:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Put your two keys after `RA_GNEWS_API_KEY=` and `RA_THENEWSAPI_API_KEY=`. Save, then restart the app with `.venv/bin/python scripts/serve_local.py stop` and `.venv/bin/python scripts/serve_local.py start`. The launcher reads `.env` when it starts; the dashboard shows **configured** without revealing either value. The file is ignored by Git. You can instead set those two environment variables in the shell before starting the app.

RA makes at most one seven-day, exact-name request per provider and company. Its default cap is 95 requests per provider per UTC day, tracked in PostgreSQL. For a watchlist above 95 companies, it divides and rotates provider coverage; the free-tier caps cannot check every one of 200 companies twice per day. See [provider access, limits and pricing](sources/news-api-providers.md). GNews's free tier permits development and evaluation but requires a paid plan for business production.

Offline translation models download automatically from the official Argos package index when a watchlist country needs them or a new article language is encountered. The initial download can take several minutes and uses local disk space. To translate older saved leads after models finish installing, run `.venv/bin/python -m ra.translation`.

The app reads the company workbook with separate **Registration number** and **Tax ID** columns, plus full and short names, aliases and legal address. It also accepts the older combined `Registration number/tax ID` column and splits clear pairs for review. A single identifier of uncertain type stays in Registration number until checked. Workbook imports, manual additions and one-off subjects offered for saving enter an **identity review queue** first. A background preliminary check compares names, countries and identifiers in GLEIF and, for Brazil, BrasilAPI; it proposes empty-field values only after a strong match and shows early news leads. The analyst can correct the fields, double-check identity and confirm before a company is created or an existing record updated. Run the app after upgrading so `db.init_schema()` migrates existing company and draft identifiers; then use **Stage workbook for review** on the dashboard. The app also supports edit/remove/restore, whole-list search, dropdown search, and one-time search. Completed searches have Excel and PDF **lead reports**. Removal archives the watchlist entry while preserving search history.

The pilot currently collects public Google News RSS leads, optional GNews and TheNewsAPI leads when keys are configured, Heringer's official investor-relations notices, and recent FCA warning RSS name leads for companies registered or present in the UK. The FCA feed checks legal and short names in titles; it does not search the full historical Warning List, and every match requires identity and clone verification. Google News uses local editions for the current watchlist countries. It stores headlines, source links, dates, and simple review keywords. The run page lets an analyst select leads and build a selected-lead Excel or PDF report. Headlines are classified by their actual language, independently of the feed locale. The app discovers several substantial or official languages per watchlist country from CLDR data, downloads available direct Argos/OPUS-MT → English models from the official package index in the background when a country is added, and can install a newly encountered language during a search. This keeps translation local and avoids per-headline AI API charges. The original headline stays visible. The packaged models derive from OPUS-MT work by Jörg Tiedemann and Santhosh Thottingal, licensed CC BY 4.0. Not every language has a direct Argos model (notably Uzbek and Kazakh); those are shown as unavailable instead of falsely labelled as still pending. Publisher brands are retained as names. These are **unverified leads**, not a completed credit assessment. Document-level analysis, most country-specific daily adapters, scheduled delivery, and email are later work; the [source registry](sources/README.md) records available inputs and prices.

## Risk relevance and review

Saved run pages default to **Recent risk leads**. A lead needs a legal/short-name match in the headline (or a company-specific official source), an adverse-event cue in the original or English headline, and a date within 30 days of that run. Short ambiguous names need country or industry context. Signals cover distress, restructuring, litigation, operational disruption, financial deterioration and resignations; normal promotional coverage and routine notices do not automatically qualify. These are explainable headline rules, not document-level conclusions or verified entity matches.

**Needs verification**, **Older risks**, **Background**, and **All collected** keep the other leads accessible. Each row explains its classification. This also applies to historical runs without rewriting or deleting saved leads. Selecting or clearing visible leads preserves saved selections in other views. The automatic PDF uses new recent risk leads; all-lead Excel and manually selected reports remain available. New Google News queries use a seven-day window, matching the API lookback; official notices retain their historical context.

The current pilot still needs reviewed local-language names and aliases (especially the Vietnam entry), full-source entity/event verification, and analyst feedback on false positives and missed events. A blank risk view does not mean a company is risk-free. No scheduled watchlist searches or email delivery are enabled by login startup.

## Local verification — 4 November 2026

- Preserved the pre-existing uncommitted changes; the initial tracked diff is saved locally in `.local/changes-before.patch`.
- Both provider keys were configured in the Git-ignored `.env` with mode `600`. Fixed TheNewsAPI's UTC date formatting: its `published_after` parameter does not accept GNews's trailing `Z` format.
- One Heringer live search (run 9) completed with zero source errors. Both APIs accepted one request each and returned zero exact-legal-name hits in seven days; other sources supplied 34 leads. Daily usage was 1/95 for each provider after the check.
- Existing watchlist run 8: 73 collected leads classify as 1 recent risk, 10 older risks and 62 background leads. This is triage, not a measured precision/recall result.
- Validation: 36 offline tests passed; filtered/all-lead pages and PDF/Excel endpoints returned HTTP 200. The user LaunchAgent loaded successfully and recovered RA after a simulated server crash without additional API requests. Database-readiness retry is covered by an offline test; an actual Mac reboot was not performed.

## Article sentiment and weekly history

Open <http://127.0.0.1:8765/scores> for the watchlist heatmap. Each saved article has an integer score from **−3 to +3**: severe, strong and limited negativity; neutral; limited, strong and severe positivity. The scoring rubric is visible on that page. These are versioned, provisional headline assessments, not verified credit ratings. Both the original and English headline are checked, with matched evidence and the reason displayed. Company identity uncertainty, missing dates, unavailable translations and ambiguous or denied events are excluded from weekly averages pending review. A zero on an excluded article is not treated as neutral company coverage.

Scoring runs locally with no AI or news API calls. New finished searches are scored automatically. Startup and **Refresh saved scores** backfill existing records, without overwriting analyst decisions. The refresh also fills elapsed no-data weeks. It does not schedule searches. The same operation is available as `.venv/bin/python -m ra.score_store`.

Weekly records use **publication dates, Monday–Sunday in Europe/Moscow**. Each included exact article URL contributes once per company/week; an analyst-reviewed occurrence takes precedence over an automatic one. The weekly arithmetic mean is saved to two decimals, with a rounded integer for the colour scale (halves round away from zero). Worst/best scores, negative/neutral/positive counts, reviewed counts, exclusions and search/source-error coverage are saved alongside it. No usable articles means **no data**, not zero. Different articles covering the same event are not yet grouped.

Click a heatmap cell for its articles and saved revisions. **Review score** lets an analyst change any article to an integer in the seven-step scale, decide whether it belongs in the company average, and explain the decision. Scores are recalculated atomically and prior weekly snapshots and article decisions are retained. Late discoveries can revise historical publication weeks; old snapshots remain available. One-off subjects without a saved company ID do not enter watchlist history. Excel lead downloads include scores, inclusion, status, reasons and rule versions. `/api/scores/weekly` exposes saved history as JSON, optionally filtered with `?company_id=1`.

Validation on 4 November 2026: **70 tests passed**, including six real PostgreSQL integration tests in disposable schemas that rolled back. The existing 411 articles were backfilled into 96 company-week records across nine companies; 122 article assessments require review and 289 are provisional. The database was backed up to the Git-ignored `.local/before-scoring/RA-before-scoring.dump` first. Repeating the refresh changed no rows. Heatmap, weekly JSON and scored-run pages returned HTTP 200. No additional news requests were made.

To run all tests including PostgreSQL integration checks:

```bash
RA_TEST_DATABASE_URL=postgresql://localhost/RA .venv/bin/python -m pytest -q
```

Without that explicit test URL, the database tests are skipped. Each database test uses a unique schema in a transaction and rolls it back; it does not change the watchlist.
