# RA

Local PostgreSQL pilot. Multilingual corporate credit monitoring.

- [Research and product specification](RESEARCH_SPEC.md)
- [Initial company list](companies.xlsx)
- [Source registry](sources/README.md)
- [Parallel Perplexity, Gemini and Kimi API proposal](sources/ai-provider-options.md)

First test: Fertilizantes Heringer S.A. (Brazil). Workbook has Brazil, Egypt, Greece, Vietnam, Azerbaijan, Uzbekistan, Turkey. Target: 100–200 companies, multi-country/industry.

## Run locally

PostgreSQL running. DB name exactly `RA` (uppercase). Mac has it. From folder:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m ra.bootstrap
.venv/bin/python scripts/serve_local.py start
```

Open <http://127.0.0.1:8765/>. `scripts/serve_local.py status` checks server. `scripts/serve_local.py stop` stops. For auto-start after Mac login (incl. first login after restart), install user LaunchAgent once:

```bash
.venv/bin/python scripts/serve_local.py install-service
```

Service waits for PostgreSQL, reads local API keys, keeps stable session secret, restarts RA after crash. PostgreSQL must start at login; Mac has this in Postgres.app. Logs: `.local/ra_server.log`. Service: `~/Library/LaunchAgents/com.ra.credit-monitor.plist`. `stop` unloads for current login. `start` loads. To disable login start permanently: `launchctl disable gui/$(id -u)/com.ra.credit-monitor` then `scripts/serve_local.py stop` (virtualenv Python). Rerun `install-service` to re-enable. No API keys in plist. Login service uses default local DB/port. Shell overrides below for manual server without LaunchAgent.

Without LaunchAgent, script keeps dev server running after shell closes. App binds local machine only. Different connection/port: set `RA_DATABASE_URL` (libpq URI) or `RA_PORT` in shell. Keep DB password out of Git. Production needs WSGI server, auth, backups, scheduled worker.

## Interface and result languages

Select **English** or **Русский** in the header. The preference stays in this browser and applies to pages, weekly scores, and PDF/Excel downloads. Both company and watchlist runs search English and Russian news alongside the company's local editions. Publisher names, identifiers, URLs, and original quotations remain available for checking.

Runs created on or after 13 November 2026 at 00:00 Moscow time queue English and Russian display text for its leads, discovery summaries, event briefs, and assessments regardless of the selected language. The background worker reuses the exact source wording across runs. Russian uses the local English-to-Russian Argos pair or Gemma in LM Studio; automatic model downloads still require `RA_ALLOW_MODEL_DOWNLOADS=1`. If translation is unavailable, the page labels the original and retries in the background. Earlier runs show saved translations or the original text. Opening them does not queue translations. Eligibility follows the run’s creation time, including when a new run searches older publication dates. Display translations do not create leads, change analyst decisions, or add weight to an event.

## News API keys

Under Setup → Search settings, **Use Perplexity API** and **Use Google API** apply independently to new individual and watchlist runs. With both enabled, Perplexity handles routine discovery and Gemini 4 Carbon uses Google Search grounding for missing filing coverage, conflicting earnings comparisons or unresolved rulings. Google alone searches each calendar quarter in the publication window. Disabling both preserves exchange, issuer, RSS and other configured news searches. Google News RSS has its own retrieval path. RA searches the selected publication window with issuer names and local risk terms. Historical searches cover each calendar quarter. A reported court decision or petition triggers a dated follow-up search for its outcome. RA records provider failures and keeps earlier results if a later request fails. The Perplexity search switch requires its configured key. For Gemini use `Google API` or `RA_GOOGLE_API_KEY` in the local environment file. The optional `RA_PERPLEXITY_DAILY_LIMIT` caps requests across the app. A blank limit uses the account limit.

Decision briefs link their source and quote the operative terms. Partial relief requires separate evidence of the remaining exposure. An unreadable decision is flagged for review. Watchlist names, aliases and tickers are preserved in machine translations.

GNews and TheNewsAPI optional. From RA folder, create local key file:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Put keys after `RA_GNEWS_API_KEY=` and `RA_THENEWSAPI_API_KEY=`. Save. Restart app: `.venv/bin/python scripts/serve_local.py stop` and `.venv/bin/python scripts/serve_local.py start`. Launcher reads `.env` on start. Dashboard shows **configured**, hides values. File ignored by Git. Or set env vars in shell before start.

RA searches reviewed aliases, short names and legal names using exact phrases joined with OR. Short ambiguous names also require country context. Query groups and pages use the selected date range without restricting article language. GNews requests 10 articles per page; TheNewsAPI uses the account's page size. Default cap: 95 requests/provider/UTC day (PostgreSQL tracked), shared across companies and runs. Remaining budget is divided across the watchlist; scarce coverage rotates across runs. Partial results survive later-page failures, and incomplete coverage appears in run errors. See [provider access, limits and pricing](sources/news-api-providers.md). GNews free tier: dev/eval only. Paid plan for business production.

Offline translation model installation requires explicit approval: set `RA_ALLOW_MODEL_DOWNLOADS=1` in `.env` and restart RA only after approving the download. Existing installed models remain usable without this setting. Start the LM Studio server on 127.0.0.1:1234 with qwen/qwen3.8-27b and google/gemma-4-31b-qat downloaded. Discovery can overlap an earlier run’s model work. RA reads up to two issuer archives concurrently and preserves their original order. Model passes share one lease and reuse the loaded instance. Qwen reviews up to four independent headlines or passages at once, with a separate evidence check for each. Gemma handles generation, Qwen reviews the candidates, and rejected translations may require one Gemma correction pass followed by final Qwen review. Qwen also assesses themes and drafts events. Keyword maintenance and CLI retries share the same process lock and cannot evict a run’s model mid-pass. Logs record each actual model load and its batch. Each load uses at least 16,384 context tokens; Gemma requests maximum GPU offload. Run status shows waiting, unloading, loading and processing. Both models use reasoning off. RA does not yet start the LM Studio server automatically. Missing headline translations automatically fall back from Argos to MyMemory, then local Gemma, then Google public translation. These fallbacks work with offline-model downloads disabled. Qwen reviews the English candidate against the original. Checks never authorize score. Model may abstain. Failed checks/missing translations stay visible. Retry missing translations/unavailable reviews for single run: `.venv/bin/python -m ra.translation --run RUN_ID`. Meta [NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-1.3B#intended-use) benchmark candidate: research use, noncommercial license. Initial download takes minutes, uses local disk. Translate older saved leads after models install: `.venv/bin/python -m ra.translation`.

App reads company workbook: separate **Registration number** and **Tax ID** columns, plus full/short names, aliases, legal address. Accepts older combined `Registration number/tax ID` column, splits clear pairs for review. Single uncertain identifier stays in Registration number until checked. Workbook imports, manual adds, one-off subjects enter **identity review queue** first. Background preliminary check compares names, countries, identifiers in GLEIF and (for Brazil) BrasilAPI. Proposes empty-field values only after strong match. Shows early news leads. Analyst corrects fields, double-checks identity, confirms before company created/updated. Run app after upgrade so `db.init_schema()` migrates existing company/draft identifiers. Then use **Stage workbook for review** on dashboard. App supports edit/remove/restore, whole-list search and individual watchlist searches. Completed searches have Excel/PDF **lead reports**. Removal archives watchlist entry, preserves search history.

Pilot collects: public Google News RSS leads, optional GNews/TheNewsAPI leads (if keys set), Heringer official investor-relations notices, recent FCA warning RSS name leads (UK registered/present companies). FCA feed checks legal/short names in titles. No full historical Warning List search. Every match needs identity/clone verification. Google News uses local editions for watchlist countries. Stores headlines, source links, dates, simple review keywords. Run page: analyst selects leads, builds selected-lead Excel/PDF report. Headlines classified by actual language, independent of feed locale. App discovers substantial/official languages per watchlist country from CLDR data. With `RA_ALLOW_MODEL_DOWNLOADS=1`, installs available direct Argos/OPUS-MT → English models from the official index when a country or article language needs them. Original headline stays visible. Packaged models derive from OPUS-MT work by Jörg Tiedemann and Santhosh Thottingal, licensed CC BY 4.0. If Argos cannot translate, RA sends text to MyMemory’s free online translation API. Limits: 500 UTF-8 bytes/request, 5,000 chars/day (anonymous). RA accepts exact source match in requested target language. If online translation fails, RA uses local Gemma for translation fallback, Qwen for review. Missing English-to-local keyword pairs also use MyMemory, local Gemma backup, Qwen review. Both run with reasoning off. Publisher brands retained as names. These are **unverified leads**, not completed credit assessment. Conservative source-text checks, duplicate-event grouping available. Most country-specific daily adapters, scheduled delivery, email remain later work. [source registry](sources/README.md) records inputs/prices.

## Risk relevance and review

Company-specific searches also use 60 adverse keyword families covering financial distress, enforcement, financial crime, trafficking, labour/environmental harm, and operational incidents. Each related phrase and management or ownership title variant follows the same translation and review process as a main keyword. Keyword settings show approved translations and pending languages for each phrase. Translated phrases inherit the parent keyword’s switch and sentiment. Existing terms, edits, disabled settings, and translations are preserved when new seeds are added. New local-language translations enter the existing independent-review queue; English searches remain available while translations are pending.

Adverse queries use company names/aliases and separately searchable names from top management and key owners. Company-linked title queries include CEO, CFO, chair, main shareholder, beneficial owner, and founder, even without known personal names. Company/sector/country risk-factor switches retain their scopes: this vocabulary does not add country or sector query branches. Short OR batches cover the vocabulary without silently truncating later terms; overlapping phrases already covered by a shorter phrase do not add redundant requests. Broader coverage increases Google News RSS requests per company; paid-provider budgets are unchanged.

Person names are discovery leads, never company aliases. Person-only sources remain available for identity and relationship review. Source checks distinguish reported allegations, investigations, arrests, charges, convictions, denials, cleared/resolved cases, and victim/witness contexts. These cues do not establish guilt or company credit impact. Analysts use the existing source, score-review, audit-history and duplicate-event workflows to check identity, source credibility, event date, severity, and company consequences. RA checks case cues in English. Every non-English source requires a reviewed translation of the selected passages. No automatic case-management or alert schedule is added.

Analyst review lists **Leads used in the AI summary** first. Check **Accept** beside an event headline to select all its sources, including filtered rows. Expand the headline to adjust individual leads, then click **Save report selection**. Use **Accept all summary leads** to select every cited source across the events. **Other collected leads** lists the rest, including positive leads. Filters apply to this second section. The optional **Recent risk leads** view shows supported or analyst-confirmed negative leads within 30 days of the window end. Turn on **Focus discovery on adverse events and material filings** to prioritise adverse queries and dated issuer responses, including exchange inquiries, across the search window. The article assessment shows the automatic score alongside any analyst review. Absent company match in readable source text goes to **Background**.

The event summary links to each underlying lead. Individual leads can be edited in the review table. Both sections retain unchecked leads. Report selections in other views remain saved. Automatic PDF uses new recent risk leads. All-lead Excel and manually selected reports available. Source-supported statement not independent confirmation. Empty risk view does not establish company risk-free. Scheduled monitoring disabled pending analyst-labelled calibration.

## Local verification — 4 November 2026

- Preserved pre-existing uncommitted changes. Initial tracked diff saved locally in `.local/changes-before.patch`.
- Both provider keys configured in Git-ignored `.env` (mode `600`). Fixed TheNewsAPI UTC date formatting: `published_after` param does not accept GNews trailing `Z` format.
- One Heringer live search (run 9) completed, zero source errors. Both APIs accepted one request each, returned zero exact-legal-name hits in 7 days. Other sources supplied 34 leads. Daily usage 1/95 per provider after check.
- Existing watchlist run 8: 73 collected leads = 1 recent risk, 10 older risks, 62 background leads. Triage, not measured precision/recall.
- Validation: 36 offline tests passed. Filtered/all-lead pages, PDF/Excel endpoints returned HTTP 200. User LaunchAgent loaded successfully, recovered RA after simulated server crash without additional API requests. Database-readiness retry covered by offline test. Actual Mac reboot not performed.

## Company events and weekly scores

Individual searches require an active watchlist company. Every completed search keeps its collected leads. **AI summary for search results** shows one timeline across countries and companies, oldest first. Events use the documented event date when available, then the first coverage date. Undated events appear last. Each brief describes one incident, its dates and severity. Expand its evidence to inspect the articles in publication order. Positive articles remain available under **Other collected leads**.

RA checks up to three directly linked reports, filings or source documents on a publisher page, prioritising PDFs. Each document has its own source and translation checks. Public reader fallbacks require matching publisher metadata. Some publishers deny automated access.

Article sentiment uses **−1 negative, 0 neutral and +1 positive**. In **Analyst review**, edit scores and use **Save report selection** or **Save all leads**. Saving accepts the current scores of selected, dated leads assigned to a company event, including unchanged scores. Source verification and explicit analyst exclusions are preserved. Positive leads and unsupported exposure leads cannot contribute. Weekly scores use the recalculated event scores. Changing a selection or article score preserves the original AI brief.

Repeated copies of one document count once. Material adverse evidence takes precedence. For other events, the first negative source has weight one and each additional negative source adds two. The event scores −1 when that weight meets or exceeds the neutral source count. Neutral-only coverage scores zero. Source volume and materiality also determine event order within a company.

When saved runs cover the same company event, Weekly scores pool their selected, eligible evidence and recalculate one event score. A document found in multiple runs counts once, and its latest explicit analyst review takes precedence over automatic scores. Weekly scores average separate events. Each event belongs to the Moscow publication week of its earliest selected eligible source. Later coverage of the same event can revise that week. A week without eligible events shows no data. Open a cell for event contributions and underlying sources, or use `/api/scores/weekly`.

On upgrade, back up PostgreSQL and stop older writers before starting the new version. Startup maps old grades to their sign and rebuilds weekly scores. Prior assessments and weekly snapshots remain in audit history. Restore the pre-upgrade database and application together for a full rollback.

Validation 4 November 2026: **70 tests passed**, incl. six real PostgreSQL integration tests in disposable schemas (rolled back). Existing 411 articles backfilled into 96 company-week records across nine companies. 122 article assessments require review, 289 provisional. Database backed up to Git-ignored `.local/before-scoring/RA-before-scoring.dump` first. Repeating refresh changed no rows. Heatmap, weekly JSON, scored-run pages returned HTTP 200. No additional news requests made.

To run all tests incl. PostgreSQL integration checks:

```bash
RA_TEST_DATABASE_URL=postgresql://localhost/RA .venv/bin/python -m pytest -q
```

Without explicit test URL, database tests skipped. Each database test uses unique schema in transaction, rolls back. Does not change watchlist.


## Reviewed aliases — 4 November 2026

Company edit forms now have separate **Reviewed company aliases / local-language names** and **Alias sources and review notes** fields, plus preview of next saved-name search. Use one same-entity name per line (max 8, 3–150 chars each). Do not place owners, subsidiaries, product brands here. Existing mixed workbook affiliate/brand columns remain affiliate context. Dedicated `Aliases` or `Company aliases` workbook column imports into new field. `Alias sources` imports notes. Blank new fields preserve existing intake fingerprints. Unchanged workbook not restaged after upgrade.

Google News uses max 3 queries: legal name + up to 2 aliases. Short name/legal-suffix fallback where space. Short ambiguous aliases require country context in discovery, country/industry context in headline matching. Both news APIs use one preferred alias phrase, same limits, 7-day lookback. Relevance filter and scorer share alias matcher. Vietnamese đ/d, Turkish dotless ı/i normalized. Publisher names, affiliate fields not company-identity evidence. Scoring version `headline-credit-v2` records matcher change. Analyst overrides protected.

[sourced watchlist review](sources/company-alias-review.json) covers all nine entries. Eight have explicit aliases. MARS-A has unresolved-identity note only. Vietnam now uses **PVFCCo** and **Phân bón và Hóa chất Dầu khí**. English name spacing, current tax ID corrected from company's official reports. Previous `4103007696` value preserved in notes as older registration certificate. CMS short name narrowed to **CMS Jant ve Makina** to avoid conflating separate group entity. Greek/Arabic names added where supported. Cibra, DPM, parent names, product brands deliberately not approved as equivalent names. Source notes explain remaining attribution risks.

Review can be inspected without changing records, then applied explicitly:

```bash
.venv/bin/python scripts/review_company_aliases.py
.venv/bin/python scripts/review_company_aliases.py --apply
```

Script checks company/country/registration identity, expected old values. Preserves existing aliases/notes. Saves exact before/after values under `.local/before-aliases/`. Idempotent. Local database backed up there before applying. Past search-run snapshots retain original names. Alias edits affect future searches. No new news requests needed to preview names, edit aliases, refresh saved scores. Source-attribution/event-grouping implementation described below. Analyst-labelled calibration next prerequisite for scheduled monitoring.

Validation: **80 tests passed**, incl. eight rollback-only PostgreSQL tests. Alias form persistence, old-run snapshots, unchanged-workbook intake hashes, short-name ambiguity, exactly one mocked request per API covered. Running Vietnam edit page, search preview checked visually. Dashboard, scores, weekly JSON, saved-run endpoints returned HTTP 200. All 411 existing article scores/inclusion decisions unchanged after version upgrade. Historical run snapshots unchanged. Second score refresh, alias preview changed zero rows. GNews/TheNewsAPI usage stayed one request each for 4 November. Alias review made no news-provider requests.


## Source verification and duplicate events — 4 November 2026

After each new search, app queues **every collected source for verification**. Prioritises direct publisher URLs, stronger headline cues, recent dates. Existing history not fetched automatically. **Check next sources** on saved run queues next batch. **Review evidence & score → Check full article** checks one article. For Google News wrapper, reader follows public article-specific publisher handoff. If fails, searches Bing News RSS using original-language headline and publisher. Search match must have same headline/publisher, publication time within 24 hours, one destination, matching headline on publisher page. Search snippets never enter scoring. If lookup/extraction fails, article page offers exact-headline web search, accepts direct publisher URL. Publisher access restrictions remain in force. Background queue survives app restarts. Run page updates two progress bars every 5 seconds: publisher checks across all leads, Qwen reviews across translated headlines. Finished attempt counts even if source inaccessible or review inconclusive. Lead table refreshes separately so unsaved selections survive live updates. Completed job can still mean unavailable/ambiguous source.

Public HTML article/main text or structured `articleBody` extracted. Navigation, headings, related-story widgets, access gates excluded. Text PDFs use [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html). Scans require analyst reading. Each retrieval permits max 4 HTTPS requests (incl. redirects), 2 MB, 30 PDF pages, 100,000 extracted chars, 25 seconds wall time (15 CPU seconds). Every redirect revalidates public IPs. Connections pin validated address with TLS hostname verification. No cookies, browser scripting, login, paywall bypass attempted. Successful text cached 7 days, failures 24 hours across articles. Sources, extraction method, detected language, content hash, evidence revisions remain in PostgreSQL. GNews/TheNewsAPI never called by this queue.

`source-attribution-v6` checks company impact in English for every source. Each new run reviews up to 5,000 original characters per document. The article and each linked attachment receive separate budgets. Runs created under the shared per-lead limit keep it on retry. RA selects issuer-relevant excerpts from longer sources and labels partial coverage in the evidence view. Every non-English source uses reviewed translations of the selected text before the same attribution rules run. Argos translates bounded passages first; MyMemory/Gemma provide fallback. Qwen independently reviews every translated passage against its original, with reasoning off. Rejected Argos output gets one fallback translation and a fresh Qwen review. Every passage must be approved; missing, uncertain or rejected translations cannot authorize scores. The cache binds translations to the complete original body, language and translation version. Retries reuse approved passages. Article assessment pages show translation coverage and original passages beside translated evidence. Setup's **Retry source checks** includes incomplete article translations. Headlines never substitute for full article text.

The legal name or a reviewed alias must still be the grammatical subject directly connected to a credit-event phrase through a small allowed verb vocabulary. No liability is inferred from suppliers, parents, affiliates, publisher names or a company mention elsewhere. Negated/conditional statements, adjacent denials, historical context, clone warnings, unresolved pronouns and ambiguous event links require review. Recovered reader text retains its analyst-review gate. “Supported” means **the source makes an explicit attributable statement**, not independent corroboration. Short aliases still need country/industry context. These conservative rules miss valid formulations and require analyst-labelled calibration before scheduling. Multilingual translation removes the language gate; it does not resolve every attribution gap.

Source-credit-v4 introduced attributable source evidence on the former −3…+3 scale; source-credit-v6 used −2…+2. Current assessments use −1, 0 and +1. Unsupported articles keep headline triage value for inspection, do not enter averages. Lack of detected event not treated as verified neutrality. Analyst can approve neutral 0 after reading. Prior analyst score/inclusion decisions survive subsequent source checks. Migration retains every lead, search snapshot, previous weekly revision. Records new exclusions. Consequently historical heatmap cells can become **no eligible coverage** until source checks/analyst review completed.

`event-group-v1` groups within each company by tracking-cleaned URL, identical extracted documents, matching resolved publisher URL, or near-identical attributable event sentences. Near-copy matches require same event rules/direction, identical numeric/non-filler word tokens (protect distinct locations/counterparties), at least 8 distinct words, at least 90% word-set overlap, publication dates within 3 days. Comparisons use group representative to avoid transitive chains. Conservative duplicate grouping, not broad semantic clustering. Paraphrases can remain separate. Each event contributes once, in the week of its earliest selected, eligible source. Within that company event, matching publisher links (including www, mobile and AMP variants), resolved links or document hashes count as one article across runs. Article IDs in query parameters remain distinct. Analyst review and Weekly scores link each duplicate to the representative lead and indicate whether it counted. All copies, scores, and selections stay in history.

Each article page exposes group members. Supports **separate**, **join another article's group**, or **restore automatic grouping**, with mandatory reason. Joining restricted to same saved company. Exact repeat URLs inherit explicit grouping decisions. Corrections update weekly snapshots atomically, append grouping audit. Weekly detail lists representatives, excluded articles, duplicate sources. Excel includes source-check status/reason, group IDs.

Validation: **123 tests passed**, incl. eleven rollback-only PostgreSQL tests. Coverage: 7 score steps, supplier/parent confusion, denials, historical context, headline/body separation, HTML/PDF extraction, private-address/redirect rejection, source cache reuse, durable queue limits, manual grouping, duplicate weighting, protected analyst decisions, audit revisions. Live database backed up to `.local/before-verification/RA.dump` before migration. Three saved sources checked live: 8,838-char Portuguese PDF extracted, conservatively held for attribution review. Scanned PDF had no extractable text. Google News link did not redirect to publisher. Neither unavailable source scored as verified. macOS subprocess-limit incompatibility found during testing fixed, covered by regression test. Provider usage unchanged. No scheduled watchlist searches enabled.

Subsequent translation-benchmark inspection found corrupted glyphs, many invalid control characters in extracted Portuguese PDF. Excluded from translation corpus. Successful extraction alone does not establish readable source text. Source-text quality gate remains needed before expanding automatic attribution.

## Isolated translation evaluation

[benchmark instructions](scripts/translation_benchmark_README.txt) cover pinned Meta NLLB, Google MADLAD downloads, hash verification, CTranslate2 INT8 conversion, comparisons with existing translator, local human-review sheet. Corpus includes fictional bilingual credit-news articles, legal/negation stress cases. Saved headline inputs optional. Model weights, environments, database exports, generated results stay in ignored local directories. These tools do not switch running app's translator. Human review, downstream attribution validation required before adoption.

## Country and sector exposure

All-watchlist and selected-company runs search three branches. Company discovery uses existing names, local editions, official sources. Each distinct country/sector gets one cached check per run. Check searches English Google News RSS plus relevant local editions using native topic/credit terms. Country uses own configured edition. Latin America uses Brazilian Portuguese, Mexican Spanish. Sector uses union of editions across all linked companies’ registered/operating locations. Azerbaijan/Uzbekistan searches include Russian terms. Countries without configured edition use English. Unmapped sector labels searched as entered with local credit terms. Failed editions reported separately, successful leads retained. These searches use 7-day feed window, match topic + credit-related term in headline. Country checks cover trade restrictions, funding, currency, operating disruptions. Sector checks also cover supply, prices, demand.

Open **Country and sector exposure** on run page to see completed checks, incl. empty feeds/errors. Each matching article lists connected company, its registration/presence/industry, possible impact to investigate. Sector queries include linked markets. Company receives sector lead only when original headline names its registration country, recorded operating market, or recognised regional member. Publisher names, feed edition do not establish market match. Each source URL has one row per company even when several branches find it. Database retains topic results for restarted run.

Weekly inclusion of an exposure lead requires source-supported impact on the named company. The source checker uses the original English or an independently reviewed English translation of the selected passages. Company detail, Excel leads and selected PDF carry the exposure explanation. One-off searches also check country and any industries supplied with the subject.


## Company monitoring reports

Use watchlist form to search all active companies, one country/region (registration/operating presence), one sector, or intersection. Selected-company searches and historical one-time runs also support reports. On finished run, select company under **Company monitoring report**, download PDF/Excel.

Report ranks run’s preceding 7 days of headlines. Prioritises scored events, analyst selections, company matches. PDF cites up to 5 distinct top events, 3 leads per branch. Excel includes all relevant source records, original headlines, links, verification notes. Weekly sentiment uses the three-grade event scale −1 to +1. Legacy comparison snapshots retain their labelled original scale; changes across different scales are not calculated. Current publication week includes post-run assessments until next company search or week close. Historical comparisons use saved ratings available at corresponding historical time. Missing coverage stays explicit. One-time subjects have no watchlist rating history. Evidence table counts sources, supported impacts, pending checks by branch.


**Next dates to watch** lists upcoming earnings releases, debt maturities, covenant tests in date order, incl. events on report date. Each entry links to issuer filing, identifies reporting period/instrument. Dates selected from `sources/watch-dates.json`, reviewed register keyed by registration country, tax/registration ID. To add date: check latest filing, record publication timestamp with timezone, exact date, source URL, page/section, short supporting excerpt. For amendment, append later filing with same event key. Null event date withdraws event. Reports select last filing available at run cutoff. Calendar entries do not enter credit scoring. Blank calendar means register holds no confirmed upcoming date for issuer.

Company, sector, country searches exclude sports/arts coverage during discovery.
RA applies provider query exclusions, screens incoming categories, publisher URL sections,
headlines, available API summaries in original language before processing leads.

Article verification uses optional Brave, Jina, Firecrawl keys from `.env` (see `.env.example`).
After Google News handoff, original-headline RSS lookup fail, Brave searches
original-language headline, publisher. RA checks publisher page, publication time
(within 24 hours of feed). For public pages that time out, contain multiple articles,
yield too little text, RA tries Jina Reader, then Firecrawl. Jina follows provider limits.
RA reserves one Firecrawl credit per basic scrape, up to 1,000 per UTC calendar month across
all runs. Earlier attempts this month count toward budget. RA returns credits for explicit
API rejections (rate limits), retains reservations when billing uncertain.
Firecrawl also enforces account's available balance. Brave defaults 20 attempts per UTC day.
Assessment page shows recovered text for analyst review.
Reader output can contain comments, related stories, so cannot automatically change
company score. New run uses this flow automatically. Use **Check full article** to retry
previously unavailable article.

Company searches also check leadership, major ownership news in English, registration-country language where configured. Azerbaijan/Uzbekistan get Russian queries too. Search uses issuer aliases, enabled governance terms in keyword editor, separate issuer-plus-name queries for listed executives/owners. **Leadership and ownership** on run page gathers matching leads. Company PDF/Excel briefs include relevant 7-day headlines, available roster comparisons.

For Brazilian companies with matching CNPJ, legal name, each run records names/roles in Receita Federal roster supplied by BrasilAPI. For Heringer, also compares issuer’s dated shareholding table, incl. disclosed percentages, share counts. First observation establishes baseline. Later observations show differences since preceding successful check. Issuer’s update date, RA’s observation date can differ. Partial rosters, missing dates on issuer table, unreconciled totals reported as coverage gaps. Leadership/ownership change needs separate evidence of credit impact to enter automatic weekly score.

Governance checks cover 7 current registration countries. Each company search includes local-edition query for exchange/regulator notices. Curated issuer sources require matching country, legal name, identifier. Run’s **Leadership and ownership** view, both company report formats distinguish source observations, failed checks, register follow-ups. Dates on filings can precede date RA reads them. Only comparable observations for same subject/source enter roster comparisons.

| Registration country | Additional public evidence | Remaining scope |
| --- | --- | --- |
| Azerbaijan | Vergilər official company notices, queried by VÖEN | Obtain State Tax Service extract for current representatives, major owners. Empty gazette search means no matching published notice. |
| Brazil | CNPJ managers, partners, plus Heringer’s dated investor ownership table | Cibrafértil’s CNPJ roster does not establish ownership percentages. |
| Egypt | Delemar Industrial Group’s published chief officers, EGYDIR’s separate, undated entry for operating entity’s general manager | Watched profiles-production entity needs own ITDA extract for directors, owners. |
| Greece | Euronext Athens issuer board, disclosed voting rights of at least 5% | Smaller holdings fall outside exchange table. |
| Turkey | CMS Jant ve Makina board, Tiryaki Agro board, CEO, Tiryaki’s dated KAP shareholding table | CMS ownership needs separate disclosure. |
| Uzbekistan | Balton Trading Asia’s tax-ID-matched Orginfo director, founder percentages, with source date | Orginfo is secondary directory. Use official register extract for confirmation. |
| Vietnam | PVFCCo board, executives, current investor disclosure index, named key shareholders in latest published English investor bulletin | Holdings apply on date printed in bulletin. Report links PDF. |

Watchlist runs default to leads published in the seven days ending at run creation. Search POST requests can instead supply paired `window_start` and `window_end` ISO timestamps with timezone offsets for a past interval of up to seven days. Bounds are inclusive; separate calendar weeks should end at Sunday 23:59:59.999999 in Moscow. The stored window survives search retries and appears on the run page. Company, country, sector, and official-notice searches use historical bounds; the worker rejects undated and out-of-window results before translation or source review. Historical searches skip current roster snapshots, which cannot establish past ownership. Source availability today does not prove complete historical coverage. Prior saved evidence remains unchanged. `run_window` log entries record collected, retained, and excluded counts per company; `run_progress` and `source_timing` entries record processing stages.

Google News RSS pauses across the watchlist after three consecutive HTTP 503 responses or a server-specified retry delay. RA probes the feed after five minutes by default and extends the pause when the probe fails. Other news providers and issuer checks continue. The run page groups missed searches by company and shows how many were recovered. RA replays each outstanding query in its original edition and publication window. Recovered leads enter the original run for translation, verification and analyst selection.

During a gap, RA can use one Brave Search request per affected company per run within the shared daily allowance. The RSS recovery queue and cooldown survive restarts. To recover an older run with recorded company-query 503s, use `.venv/bin/python -m ra.rss_recovery --import-run RUN_ID` after upgrading the database. Legacy failures with ambiguous editions remain visible for review.
