# Corporate credit monitoring — research specification

Status: research specification, 31 October 2026. The local pilot app now supports watchlist management, manual searches, and unverified lead exports. Daily scheduling, email, translation, and full credit-event assessment remain implementation work. The historical test case below is not a live monitoring result.

## Goal and scope

Monitor a user-maintained list of roughly 100–200 legal entities, initially from `companies.xlsx`. Run once or twice daily. Search in the company's local languages, review company, owner, affiliate, country, and sector developments, and deliver an English risk report as downloadable Excel and PDF files and by email to configured recipients. The user interface will be an HTML application.

The workbook currently has one test row: Fertilizantes Heringer, Brazil, Eurochem as key owner, Brazil as primary country of presence, and fertilizers as industry. Blank fields remain unknown, not negative findings.

## Identity and matching rules

1. Assign an internal entity ID. Store the legal name, aliases, original script, registration country, national registration number, exchange/CVM identifiers where relevant, and source URLs. For Brazil, CNPJ is the strongest practical matching key.
2. Normalize case, accents, punctuation, whitespace, and legal suffixes for candidate discovery. Preserve original names and never merge entities on normalized name alone.
3. Resolve each hit against jurisdiction and at least one corroborating attribute, such as registration number, address, management, ownership, or official website. Keep ambiguous matches in a review queue.
4. Store parent, subsidiary, and affiliate relationships with effective dates and evidence. Search both the named entity and materially relevant related entities, but label which entity each event concerns.

## Monitoring workflow

| Stage | Primary method | Quality control |
| --- | --- | --- |
| Import | Python reads and validates workbook rows | Preserve original cells; flag missing or conflicting identifiers |
| Source discovery | Curated source registry by country and industry | Prefer primary records and known local publishers |
| Collection | Python clients for APIs, RSS, official filings, and permitted web pages | Respect source terms, robots policy, rate limits, and access controls |
| Language handling | Search in Portuguese for Brazil and other applicable local languages; translate candidate snippets first | Retain original text and language; translate full passages only for relevant findings |
| Entity resolution | Exact identifiers first, then alias and context matching | Human review for uncertain matches |
| Event extraction | Rules for filings and quantitative data; model-assisted reading for complex text | Every claim links to a source and publication/event date |
| Risk review | Score severity, relevance, novelty, confidence, and likely credit transmission channel | Distinguish allegation, company statement, court order, and confirmed outcome |
| Reporting | Python-generated XLSX/PDF; HTML app shows the same evidence | Dedupe recurring stories, maintain change history, and mark no-new-event days |
| Delivery | Scheduled job and transactional email service | Record job state, recipients, artifact hashes, and delivery result |

Signals include debt service stress, restructuring, creditor action, litigation, regulatory sanctions, fraud allegations, major management or ownership changes, plant shutdowns, losses, liquidity deterioration, payment delays, supply constraints, and material country or sector shocks. Country and sector items must state the specific exposure path to the company.

## Brazil source registry — first country pack

The maintained access and pricing inventory is in [sources](sources/README.md); the table below records the initial research shortlist.

Sources should be registered with language, URL pattern/API, update frequency, access method, license/terms, cost, and coverage. The following are starting points; availability and commercial reuse terms require checking during implementation.

| Source | Type / coverage | Cost indication |
| --- | --- | --- |
| [Receita Federal CNPJ consultation](https://www.gov.br/pt-br/servicos/consultar-cadastro-nacional-de-pessoas-juridicas) | Official registration and status | Free public consultation; bulk/API access differs |
| [CVM company filings](https://sistemas.cvm.gov.br/cias-abertas.asp) and [open DFP dataset](https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp) | Listed-company disclosures and financial statements | Free public access |
| [B3 listed company page](https://sistemaswebb3-listados.b3.com.br/listedCompaniesPage/main/20621/FHER/overview?language=pt-br) | Security/company identity and market disclosures | Public pages free; market-data licenses vary |
| [Heringer investor relations](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) | Direct material facts and reports | Free public pages |
| [Diário Oficial da União](https://www.in.gov.br/) | Official notices | Free public search; reuse terms to check |
| [CNJ](https://www.cnj.jus.br/) and relevant state court, including [TJSP](https://www.tjsp.jus.br/) | Court and insolvency developments | Public search generally free; coverage/access varies |
| [CADE](https://www.gov.br/cade/pt-br) | Competition matters | Free public information |
| [MAPA](https://www.gov.br/agricultura/pt-br) | Fertilizer rules, registrations, and sector information | Free public information |
| [IBAMA](https://www.gov.br/ibama/pt-br) | Environmental enforcement | Free public information |
| [Banco Central do Brasil](https://www.bcb.gov.br/) and [IBGE](https://www.ibge.gov.br/) | Macro context | Free public data |
| [Reuters](https://www.reuters.com/world/americas/), [Valor Econômico](https://valor.globo.com/), [Brazil Journal](https://braziljournal.com/), [Agência Brasil](https://agenciabrasil.ebc.com.br/) | Global and local news | Mixed free/subscription; licensing varies |
| [OpenCorporates](https://opencorporates.com/pricing/) | Cross-border entity discovery and gap filling | Paid plans; published price previously observed at £225/month or £2,250/year, recheck before purchase |
| [Serasa Experian](https://www.serasaexperian.com.br/) | Private credit and corporate data | Commercial quote |
| [NewsAPI](https://newsapi.org/pricing) | News discovery, not a substitute for source verification | Published business price previously observed at US$449/month; recheck quotas and rights |

Discussion boards and social media can provide leads, but their claims require independent corroboration before entering the report as facts. Add more country packs as companies are added; each needs official registry, filings, insolvency/court, tax, regulator, sanctions, local business news, and credible private-data options.

## Historical sample monitoring row — Fertilizantes Heringer

Identification: Fertilizantes Heringer S.A., Brazil, CNPJ **22.266.175/0001-88**, CVM code **20621**, ticker **FHER3**, according to the [B3 company record](https://sistemaswebb3-listados.b3.com.br/listedCompaniesPage/main/20621/FHER/overview?language=pt-br). [EuroChem announced](https://www.eurochemgroup.com/media/news/investors/eurochem-completes-purchase-of-controlling-share-in-fertilizantes-heringer) completion of its purchase of a 51.48% controlling stake in March 2022; current ownership should be rechecked in filings before each ownership-dependent conclusion.

| Event date | Finding | Credit relevance | Suggested severity | Source / verification |
| --- | --- | --- | --- | --- |
| 27 Aug 2026 | Company material fact concerning creditor mediation | Possible financing and liquidity pressure; read full notice and subsequent developments before drawing a current conclusion | High | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |
| 31 Aug 2026 | Company material fact concerning protective court relief | Direct legal and debt-enforcement relevance; track court record, creditor terms, and cash position | High | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |
| 9 Sep 2026 | Free-float compliance extension | Governance/market-listing watch item; lower direct credit impact than the creditor developments | Medium | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |

This table demonstrates report structure. A production run must fetch the underlying notices, verify exact legal language and later updates, and include URLs to individual documents where available. It must not repeat old events as new daily alerts.

## Storage and application architecture

Use **PostgreSQL** as the system of record. Core tables: `entities`, `entity_aliases`, `entity_relationships`, `source_registry`, `source_documents`, `financial_periods`, `financial_metrics`, `events`, `event_evidence`, `risk_assessments`, `run_logs`, `reports`, `report_recipients`, and `delivery_logs`. Add a uniqueness constraint on registration country plus registration number where valid; use stable internal IDs because aliases and ownership change.

Keep original PDFs, HTML snapshots where permitted, and generated reports in object storage or a file store; save paths, checksums, capture timestamps, and provenance in PostgreSQL. PostgreSQL full-text search is adequate initially. Add `pgvector` only if multilingual semantic recall or deduplication demonstrates a measurable benefit. Use migrations, automated backups, and retention rules from the first deployed version.

Python should handle ingestion, source adapters, entity matching, translation routing, deduplication, event extraction, scheduling, and XLSX/PDF generation. The HTML app should expose the watchlist, source evidence, review queue, run history, and report downloads. Separate the scheduled worker from the web process so reports can run without an open browser.

## Skills and integrations

| Capability | Recommended tool / skill | Why |
| --- | --- | --- |
| Spreadsheet input/output | Python `openpyxl` or `xlsxwriter`; Codex Spreadsheets skill for manual workbook design | Reliable repeatable import/export with minimal model use |
| PDF extraction/output | Python `pymupdf`/`pdfplumber` and `reportlab`; Codex PDF skill for layout QA | Handle official reports and generate reviewable PDFs |
| Web application | Python FastAPI plus server-rendered HTML initially; Codex Sites skill if building in Sites | Simple stack and a path to richer UI later |
| Database | PostgreSQL, SQLAlchemy, Alembic | Durable relational history and controlled schema changes |
| Scheduling | Cron or managed scheduler, with a Python job queue if needed | Deterministic daily runs and retry handling |
| Email | Transactional email provider or connected mail plugin, once recipients and credentials are configured | Automated report delivery with logs |
| Search/news | Official APIs/RSS first; licensed search/news API if coverage gaps justify it | High source quality and predictable rights/costs |
| Translation | Python language detection and selective machine translation, with [Google Cloud Translation pricing](https://cloud.google.com/products/translate/pricing) as one benchmark | Translate only relevant passages; retain originals |
| Model review | Low-cost model for classification/extraction; stronger model for ambiguous or high-impact cases | Spend tokens where judgment affects the report |

The currently connected plugins should be checked when implementation starts. A GitHub connector is useful for source control; a mail connector or provider is useful for delivery. No plugin is required to store data in PostgreSQL. Keep provider interfaces replaceable.

## Running-cost model for 100–200 companies

The primary variables are search calls, premium data licensing, translation volume, and how often a model reads full documents. Budget with usage counters before selecting vendors.

| Monthly component | Planning assumption | Indicative range (USD) |
| --- | --- | ---: |
| PostgreSQL, app/worker hosting, storage, backups | Small managed deployment | 40–200 |
| Search/news discovery | Official feeds and public pages first | 0–500+ |
| Translation | Candidate snippets first; full text only on promoted findings | 0–100 |
| Model extraction/review | Rules and deduplication reduce calls; escalate only material/ambiguous items | 20–250 |
| Email/report delivery | One or two daily reports to a small list | 0–50 |
| **Core operating envelope** | Excludes premium credit/company datasets and staff review | **60–1,100+** |

At two runs per day, 100–200 companies imply roughly 6,000–12,000 company-run checks per 30-day month. This is **not** necessarily 6,000–12,000 paid search calls: shared country/sector feeds, RSS, saved queries, and change detection can reduce calls substantially. A commercial news API at the previously published US$449/month business price would shift the lower bound upward; premium registries and private credit data may require negotiated contracts. These are planning estimates, not vendor quotes or a cost guarantee.

Measure cost per source hit, promoted event, and company-run during the pilot. Cache unchanged pages, hash documents, use deterministic extraction for structured filings, translate snippets before documents, and send only evidence for ambiguous/high-severity cases to a stronger model. Do not save tokens by dropping citations or skipping entity verification.

## Acceptance criteria for the first build

1. Import the workbook, identify Fertilizantes Heringer by CNPJ, and preserve every input field.
2. Run a Portuguese and English source search with dated, linked evidence and clear entity matching.
3. Produce a reviewed event table that distinguishes fresh events from historical context and reports when no new event was found.
4. Download a matching XLSX and PDF report from the HTML app.
5. Configure a daily schedule and email recipients; log run and delivery outcomes.
6. Show a simple monthly usage and cost dashboard before expanding to 100–200 companies.

Recipient addresses, preferred run time/timezone, hosting choice, and paid-data subscriptions remain configuration decisions for implementation.
