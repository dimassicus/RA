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

RA makes at most one seven-day, exact-phrase request per provider and company. It uses the first reviewed alias of at least six letters/digits, falling back to the legal name. Its default cap is 95 requests per provider per UTC day, tracked in PostgreSQL. For a watchlist above 95 companies, it divides and rotates provider coverage; the free-tier caps cannot check every one of 200 companies twice per day. See [provider access, limits and pricing](sources/news-api-providers.md). GNews's free tier permits development and evaluation but requires a paid plan for business production.

Offline translation models download automatically from the official Argos package index when a watchlist country needs them or a new article language is encountered. Start LM Studio on 127.0.0.1:1234 with qwen/qwen3.8-27b available. Qwen reviews every non-English headline translation against its original. These checks never authorize a score. A model may abstain; failed checks and missing translations stay visible. Retry missing translations and unavailable reviews for a single run with `.venv/bin/python -m ra.translation --run RUN_ID`. Meta [NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-1.3B#intended-use) remains a benchmark candidate: its intended use is research and its license is noncommercial. The initial download can take several minutes and uses local disk space. To translate older saved leads after models finish installing, run `.venv/bin/python -m ra.translation`.

The app reads the company workbook with separate **Registration number** and **Tax ID** columns, plus full and short names, aliases and legal address. It also accepts the older combined `Registration number/tax ID` column and splits clear pairs for review. A single identifier of uncertain type stays in Registration number until checked. Workbook imports, manual additions and one-off subjects offered for saving enter an **identity review queue** first. A background preliminary check compares names, countries and identifiers in GLEIF and, for Brazil, BrasilAPI; it proposes empty-field values only after a strong match and shows early news leads. The analyst can correct the fields, double-check identity and confirm before a company is created or an existing record updated. Run the app after upgrading so `db.init_schema()` migrates existing company and draft identifiers; then use **Stage workbook for review** on the dashboard. The app also supports edit/remove/restore, whole-list search, dropdown search, and one-time search. Completed searches have Excel and PDF **lead reports**. Removal archives the watchlist entry while preserving search history.

The pilot currently collects public Google News RSS leads, optional GNews and TheNewsAPI leads when keys are configured, Heringer's official investor-relations notices, and recent FCA warning RSS name leads for companies registered or present in the UK. The FCA feed checks legal and short names in titles; it does not search the full historical Warning List, and every match requires identity and clone verification. Google News uses local editions for the current watchlist countries. It stores headlines, source links, dates, and simple review keywords. The run page lets an analyst select leads and build a selected-lead Excel or PDF report. Headlines are classified by their actual language, independently of the feed locale. The app discovers several substantial or official languages per watchlist country from CLDR data, downloads available direct Argos/OPUS-MT → English models from the official package index in the background when a country is added, and can install a newly encountered language during a search. This keeps translation local and avoids per-headline AI API charges. The original headline stays visible. The packaged models derive from OPUS-MT work by Jörg Tiedemann and Santhosh Thottingal, licensed CC BY 4.0. Not every language has a direct Argos model (notably Uzbek and Kazakh); RA tries local Qwen, then local Gemma if Qwen cannot supply an English translation. Both run with reasoning off. Publisher brands are retained as names. These are **unverified leads**, not a completed credit assessment. Conservative source-text checks and duplicate-event grouping are available; most country-specific daily adapters, scheduled delivery, and email remain later work; the [source registry](sources/README.md) records available inputs and prices.

## Risk relevance and review

Saved run pages default to **Recent risk leads**. Articles now require a source-supported company event or an analyst-approved assessment, a negative score, and a publication date within 30 days of the search. Headlines alone cannot enter this view or weekly averages. The original headline scores remain visible in **Needs verification** and **All collected**. An absent company match in readable source text goes to **Background**.

Risk, older-risk and scored views show one representative per event, with links to the other source occurrences. All collected preserves every row. Report selections in other views remain saved. The automatic PDF uses new recent risk leads; all-lead Excel and manually selected reports remain available. A source-supported statement is not independent confirmation, and an empty risk view does not establish that a company is risk-free. Scheduled monitoring remains disabled pending analyst-labelled calibration.

## Local verification — 4 November 2026

- Preserved the pre-existing uncommitted changes; the initial tracked diff is saved locally in `.local/changes-before.patch`.
- Both provider keys were configured in the Git-ignored `.env` with mode `600`. Fixed TheNewsAPI's UTC date formatting: its `published_after` parameter does not accept GNews's trailing `Z` format.
- One Heringer live search (run 9) completed with zero source errors. Both APIs accepted one request each and returned zero exact-legal-name hits in seven days; other sources supplied 34 leads. Daily usage was 1/95 for each provider after the check.
- Existing watchlist run 8: 73 collected leads classify as 1 recent risk, 10 older risks and 62 background leads. This is triage, not a measured precision/recall result.
- Validation: 36 offline tests passed; filtered/all-lead pages and PDF/Excel endpoints returned HTTP 200. The user LaunchAgent loaded successfully and recovered RA after a simulated server crash without additional API requests. Database-readiness retry is covered by an offline test; an actual Mac reboot was not performed.

## Article sentiment and weekly history

Open <http://127.0.0.1:8765/scores> for the watchlist heatmap. Each saved article has an integer score from **−3 to +3**: severe, strong and limited negativity; neutral; limited, strong and severe positivity. The scoring rubric is visible on that page. These are versioned, provisional credit-oriented assessments, not credit ratings. Automatic inclusion requires full-source support; headline-only values remain excluded. Both the original and English headline are checked, with matched evidence and the reason displayed. Company identity uncertainty, missing dates, unavailable source text, unsupported body languages and ambiguous or denied events are excluded from weekly averages pending review. A zero on an excluded article is not treated as neutral company coverage.

Scoring runs locally with no AI or news API calls. Source verification fetches public publisher pages in a separate bounded queue. New finished searches are scored automatically. Startup and **Refresh saved scores** backfill existing records, without overwriting analyst decisions. The refresh also fills elapsed no-data weeks. It does not schedule searches. The same operation is available as `.venv/bin/python -m ra.score_store`.

Weekly records use **publication dates, Monday–Sunday in Europe/Moscow**. Each event group contributes once per company/week; an analyst-reviewed occurrence takes precedence over an automatic one (the latest reviewed decision wins). The weekly arithmetic mean is saved to two decimals, with a rounded integer for the colour scale (halves round away from zero). Worst/best scores, negative/neutral/positive counts, reviewed counts, exclusions and search/source-error coverage are saved alongside it. No usable articles means **no data**, not zero. Duplicate source occurrences remain listed in the weekly snapshot. Conflicting automatic scores within a group exclude that group until review.

Click a heatmap cell for its articles and saved revisions. **Review score** lets an analyst change any article to an integer in the seven-step scale, decide whether it belongs in the company average, and explain the decision. Scores are recalculated atomically and prior weekly snapshots and article decisions are retained. Late discoveries can revise historical publication weeks; old snapshots remain available. One-off subjects without a saved company ID do not enter watchlist history. Excel lead downloads include scores, inclusion, status, reasons and rule versions. `/api/scores/weekly` exposes saved history as JSON, optionally filtered with `?company_id=1`.

Validation on 4 November 2026: **70 tests passed**, including six real PostgreSQL integration tests in disposable schemas that rolled back. The existing 411 articles were backfilled into 96 company-week records across nine companies; 122 article assessments require review and 289 are provisional. The database was backed up to the Git-ignored `.local/before-scoring/RA-before-scoring.dump` first. Repeating the refresh changed no rows. Heatmap, weekly JSON and scored-run pages returned HTTP 200. No additional news requests were made.

To run all tests including PostgreSQL integration checks:

```bash
RA_TEST_DATABASE_URL=postgresql://localhost/RA .venv/bin/python -m pytest -q
```

Without that explicit test URL, the database tests are skipped. Each database test uses a unique schema in a transaction and rolls it back; it does not change the watchlist.


## Reviewed aliases — 4 November 2026

Company edit forms now have separate **Reviewed company aliases / local-language names** and **Alias sources and review notes** fields, plus a preview of the next saved-name search. Use one same-entity name per line (up to eight, 3–150 characters each); do not place owners, subsidiaries or product brands here. Existing mixed workbook affiliate/brand columns remain affiliate context. A dedicated `Aliases` or `Company aliases` workbook column imports into the new field, and `Alias sources` imports the notes. Blank new fields preserve existing intake fingerprints, so an unchanged workbook is not restaged after upgrade.

Google News uses at most three queries: the legal name and up to two aliases, with the short name/legal-suffix fallback where there is space. Short ambiguous aliases require country context in discovery and country/industry context in headline matching. Both news APIs use one preferred alias phrase, retaining the same limits and seven-day lookback. The relevance filter and scorer share the alias matcher; Vietnamese đ/d and Turkish dotless ı/i are normalized. Publisher names and affiliate fields are not company-identity evidence. Scoring version `headline-credit-v2` records this matcher change; analyst overrides remain protected.

The [sourced watchlist review](sources/company-alias-review.json) covers all nine entries. Eight have explicit aliases; MARS-A has an unresolved-identity note only. Vietnam now uses **PVFCCo** and **Phân bón và Hóa chất Dầu khí**, and its English name spacing and current tax ID were corrected from the company's official reports. Its previous `4103007696` value is preserved in notes as an older registration certificate. CMS's short name was narrowed to **CMS Jant ve Makina** to avoid conflating a separate group entity. Greek and Arabic names were added where supported. Cibra, DPM, parent names and product brands were deliberately not approved as equivalent names; source notes explain the remaining attribution risks.

The review can be inspected without changing records, then applied explicitly:

```bash
.venv/bin/python scripts/review_company_aliases.py
.venv/bin/python scripts/review_company_aliases.py --apply
```

The script checks company/country/registration identity and expected old values, preserves existing aliases and notes, saves exact before/after values under `.local/before-aliases/`, and is idempotent. The local database was backed up there before applying. Past search-run snapshots retain their original names; alias edits affect future searches. No new news requests are needed to preview names, edit aliases or refresh saved scores. The source-attribution and event-grouping implementation is described below; analyst-labelled calibration is the next prerequisite for scheduled monitoring.

Validation: **80 tests passed**, including eight rollback-only PostgreSQL tests. Alias form persistence, old-run snapshots, unchanged-workbook intake hashes, short-name ambiguity and exactly one mocked request per API are covered. The running Vietnam edit page and search preview were checked visually; dashboard, scores, weekly JSON and saved-run endpoints returned HTTP 200. All 411 existing article scores/inclusion decisions remained unchanged after the version upgrade, historical run snapshots were unchanged, and a second score refresh and alias preview changed zero rows. GNews and TheNewsAPI usage stayed at one request each for 4 November; this alias review made no news-provider requests.


## Source verification and duplicate events — 4 November 2026

After each new search, the app queues **every collected source for verification**, prioritising direct publisher URLs, stronger headline cues and recent dates. Existing history is not fetched automatically. **Check next sources** on a saved run queues the next batch; **Review evidence & score → Check full article** checks one article. For a Google News wrapper, the reader follows its public article-specific publisher handoff. If that fails, it searches Bing News RSS using the original-language headline and publisher. A search match must have the same headline and publisher, a publication time within 24 hours, one destination, and a matching headline on the publisher page. Search snippets never enter scoring. When lookup or extraction fails, the article page offers an exact-headline web search and accepts a direct publisher URL. Publisher access restrictions remain in force. The background queue survives app restarts. The run page updates two progress bars every five seconds: publisher checks across all leads and Qwen reviews across translated headlines. A finished attempt counts even when the source is inaccessible or the review is inconclusive. The lead table refreshes separately so unsaved selections survive live updates. A completed job can still mean an unavailable or ambiguous source.

Public HTML article/main text or structured `articleBody` is extracted with navigation, headings, related-story widgets and access gates excluded. Text PDFs use [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html); scans require analyst reading. Each retrieval permits at most four HTTPS requests including redirects, 2 MB, 30 PDF pages, 100,000 extracted characters and 25 seconds wall time (15 CPU seconds). Every redirect revalidates public IPs; connections pin the validated address with TLS hostname verification. No cookies, browser scripting, login or paywall bypass is attempted. Successful text is cached seven days and failures 24 hours across articles. Sources, extraction method, detected language, content hash and evidence revisions remain in PostgreSQL. GNews/TheNewsAPI are never called by this queue.

`source-attribution-v1` checks **English and Portuguese source sentences**. A legal name or reviewed alias must be the grammatical subject directly connected to a credit-event phrase through a small allowed verb vocabulary. It does not infer liability from suppliers, parents, affiliates, publisher names or a company mention elsewhere. Negated/conditional statements, adjacent denials or historical context, clone warnings, unsupported body languages, pronoun-only attribution and ambiguous event links require review. An English headline does not substitute for an untranslated body. “Supported” means **the source makes an explicit attributable statement**, not that the event has been independently established. Short aliases still need country/industry context. Rules intentionally favour precision and will miss valid formulations; coverage must be calibrated before scheduling.

Automatic scores use attributable source evidence on the existing −3…+3 scale (`source-credit-v3`). Unsupported articles keep their headline triage value for inspection but do not enter averages. Lack of a detected event is not treated as verified neutrality; an analyst can approve a neutral 0 after reading. Prior analyst score/inclusion decisions survive subsequent source checks. The migration retains every lead, search snapshot and previous weekly revision, while recording the new exclusions. Consequently historical heatmap cells can become **no eligible coverage** until source checks or analyst review are completed.

`event-group-v1` groups within each company by tracking-cleaned URL, identical extracted documents, matching resolved publisher URL, or near-identical attributable event sentences. Near-copy matches require the same event rules and direction, identical numeric and non-filler word tokens (to protect distinct locations/counterparties), at least eight distinct words, at least 90% word-set overlap, and publication dates within three days. Comparisons use the group representative to avoid transitive chains. This is conservative duplicate grouping, not broad semantic clustering; paraphrases can remain separate. One event contributes once in each publication week; it can appear in multiple weeks when separately dated coverage is published.

Each article page exposes its group members and supports **separate**, **join another article's group**, or **restore automatic grouping**, with a mandatory reason. Joining is restricted to the same saved company. Exact repeat URLs inherit explicit grouping decisions. Corrections update weekly snapshots atomically and append a grouping audit. Weekly detail lists representatives, excluded articles and duplicate sources; Excel includes source-check status/reason and group IDs.

Validation: **123 tests passed**, including eleven rollback-only PostgreSQL tests. Coverage includes seven score steps, supplier/parent confusion, denials, historical context, headline/body separation, HTML/PDF extraction, private-address and redirect rejection, source cache reuse, durable queue limits, manual grouping, duplicate weighting, protected analyst decisions and audit revisions. The live database was backed up to `.local/before-verification/RA.dump` before migration. Three saved sources were checked live: an 8,838-character Portuguese PDF was extracted and conservatively held for attribution review; a scanned PDF had no extractable text; a Google News link did not redirect to the publisher. Neither unavailable source was scored as verified. A macOS subprocess-limit incompatibility found during testing was fixed and covered by a regression test. Provider usage remained unchanged. No scheduled watchlist searches were enabled.

Subsequent translation-benchmark inspection found corrupted glyphs and many invalid control characters in the extracted Portuguese PDF. It was excluded from the translation corpus; successful extraction alone does not establish readable source text. A source-text quality gate remains needed before expanding automatic attribution.

## Isolated translation evaluation

The [benchmark instructions](scripts/translation_benchmark_README.txt) cover pinned Meta NLLB and Google MADLAD downloads, hash verification, CTranslate2 INT8 conversion, comparisons with the existing translator, and a local human-review sheet. The corpus includes fictional bilingual credit-news articles and legal/negation stress cases; saved headline inputs are optional. Model weights, environments, database exports and generated results stay in ignored local directories. These tools do not switch the running app's translator. Human review and downstream attribution validation are required before adoption.

## Country and sector exposure

All-watchlist and selected-company runs search three branches. Company discovery uses the existing names, local editions and official sources. Each distinct country or sector gets one cached check per run. The check searches English Google News RSS plus relevant local editions using native topic and credit terms. A country uses its own configured edition. Latin America uses Brazilian Portuguese and Mexican Spanish. A sector uses the union of editions across all linked companies’ registered and operating locations. Azerbaijan and Uzbekistan searches include Russian terms. Countries without a configured edition use English. Unmapped sector labels are searched as entered with local credit terms. Failed editions are reported separately and successful leads are retained. These searches use the seven-day feed window and match the topic and a credit-related term in the headline. Country checks cover trade restrictions, funding, currency and operating disruptions. Sector checks also cover supply, prices and demand.

Open **Country and sector exposure** on the run page to see completed checks, including empty feeds and errors. Each matching article lists the connected company, its registration, presence or industry, and a possible impact to investigate. Sector queries include the linked markets. A company receives a sector lead only when the original headline names its registration country, a recorded operating market or a recognised regional member. Publisher names and the feed edition do not establish a market match. Each source URL has one row per company even when several branches find it. The database retains topic results for a restarted run.

Weekly inclusion of an exposure lead requires source-supported impact on the named company. The existing source checker supports English and Portuguese. The company detail, Excel leads and selected PDF carry the exposure explanation. One-off searches also check the country and any industries supplied with the subject.


## Company monitoring reports

Use the watchlist form to search all active companies, one country or region (registration or operating presence), one sector, or their intersection. Selected-company and one-time searches also support reports. On a finished run, select the company under **Company monitoring report** and download PDF or Excel.

The report ranks the run’s preceding seven days of headlines, prioritising scored events, analyst selections and company matches. The PDF cites up to five distinct top events and three leads per branch. Excel includes all relevant source records, original headlines, links and verification notes. Weekly sentiment uses the existing monitored-event scale from −3 to +3. The current publication week includes post-run assessments until the next company search or week close. Historical comparisons use saved ratings available at the corresponding historical time. Missing coverage stays explicit. One-time subjects have no watchlist rating history. The evidence table counts sources, supported impacts and pending checks by branch.


**Next dates to watch** lists upcoming earnings releases, debt maturities and covenant tests in date order, including events on the report date. Each entry links to the issuer filing and identifies the reporting period or instrument. Dates are selected from `sources/watch-dates.json`, a reviewed register keyed by registration country and tax or registration ID. To add a date, check the latest filing, record its publication timestamp with timezone, the exact date, source URL, page or section, and a short supporting excerpt. For an amendment, append a later filing with the same event key. A null event date withdraws the event. Reports select the last filing available at the run cutoff. Calendar entries do not enter credit scoring. A blank calendar means the register holds no confirmed upcoming date for the issuer.
