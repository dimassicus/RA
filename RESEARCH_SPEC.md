# Corporate credit monitoring — research specification

Status: research spec, 31 October 2026. Local pilot app has watchlist mgmt, manual search, unverified lead export. Daily scheduling, email, translation, full credit-event assessment remain implementation work. Historical test case below not live monitor result.

## Goal and scope

Monitor user list of ~100–200 legal entities, start from `companies.xlsx`. Run 1-2x daily. Search in local langs, review company, owner, affiliate, country, sector news. Deliver English risk report as Excel, PDF, email to set recipients. UI is HTML app.

Workbook has 1 test row: Fertilizantes Heringer, Brazil, Eurochem key owner, Brazil primary country, fertilizers industry. Blank fields = unknown, not negative.

## Identity and matching rules

1. Assign internal entity ID. Store legal name, aliases, orig script, reg country, nat reg num, exchange/CVM IDs if relevant, source URLs. For Brazil, CNPJ strongest match key.
2. Normalize case, accents, punct, whitespace, legal suffixes for find. Keep orig names. Never merge on normalized name alone.
3. Resolve hits vs jurisdiction + 1 corroborating attr (reg num, address, mgmt, ownership, official site). Keep ambiguous in review queue.
4. Store parent, sub, affiliate rels with dates + evidence. Search named + materially relevant related entities. Label which entity event concerns.

## Monitoring workflow

| Stage | Primary method | Quality control |
| --- | --- | --- |
| Import | Python reads/validates workbook rows | Preserve orig cells; flag missing/conflicting IDs |
| Source discovery | Curated source registry by country/industry | Prefer primary records, known local pubs |
| Collection | Python clients for APIs, RSS, filings, permitted web pages | Respect terms, robots, rate limits, access controls |
| Language handling | Search PT for Brazil + local langs; translate snippets first | Retain orig text/lang; translate full passages only for relevant finds |
| Entity resolution | Exact IDs first, then alias/context match | Human review for uncertain matches |
| Event extraction | Rules for filings/quant data; model-assist for complex text | Every claim links to source + pub/event date |
| Risk review | Score severity, relevance, novelty, confidence, credit channel | Distinguish allegation, company stmt, court order, confirmed outcome |
| Reporting | Python-gen XLSX/PDF; HTML app shows same evidence | Dedupe recurring stories, keep change history, mark no-new-event days |
| Delivery | Scheduled job + transactional email svc | Record job state, recipients, artifact hashes, delivery result |

Signals: debt stress, restructuring, creditor action, litigation, reg sanctions, fraud claims, major mgmt/ownership change, plant shutdowns, losses, liquidity drop, payment delays, supply constraints, material country/sector shocks. Country/sector items must state specific exposure path to company.

## Brazil source registry — first country pack

Maintained access/pricing inventory in [sources](sources/README.md); table below is initial research shortlist.

Register sources with language, URL pattern/API, update freq, access method, license/terms, cost, coverage. These are starting points; availability/commercial reuse terms need checking during impl.

| Source | Type / coverage | Cost indication |
| --- | --- | --- |
| [Receita Federal CNPJ consultation](https://www.gov.br/pt-br/servicos/consultar-cadastro-nacional-de-pessoas-juridicas) | Official reg/status | Free public consult; bulk/API differs |
| [CVM company filings](https://sistemas.cvm.gov.br/cias-abertas.asp) and [open DFP dataset](https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp) | Listed-co disclosures/fin stmts | Free public access |
| [B3 listed company page](https://sistemaswebb3-listados.b3.com.br/listedCompaniesPage/main/20621/FHER/overview?language=pt-br) | Security/co identity/market disclosures | Public pages free; market-data licenses vary |
| [Heringer investor relations](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) | Direct material facts/reports | Free public pages |
| [Diário Oficial da União](https://www.in.gov.br/) | Official notices | Free public search; reuse terms to check |
| [CNJ](https://www.cnj.jus.br/) and relevant state court, incl [TJSP](https://www.tjsp.jus.br/) | Court/insolvency dev | Public search gen free; coverage/access varies |
| [CADE](https://www.gov.br/cade/pt-br) | Competition matters | Free public info |
| [MAPA](https://www.gov.br/agricultura/pt-br) | Fertilizer rules/regs/sector info | Free public info |
| [IBAMA](https://www.gov.br/ibama/pt-br) | Environmental enforcement | Free public info |
| [Banco Central do Brasil](https://www.bcb.gov.br/) and [IBGE](https://www.ibge.gov.br/) | Macro context | Free public data |
| [Reuters](https://www.reuters.com/world/americas/), [Valor Econômico](https://valor.globo.com/), [Brazil Journal](https://braziljournal.com/), [Agência Brasil](https://agenciabrasil.ebc.com.br/) | Global/local news | Mixed free/sub; licensing varies |
| [OpenCorporates](https://opencorporates.com/pricing/) | Cross-border entity discovery/gap fill | Paid plans; prev observed £225/mo or £2,250/yr, recheck before buy |
| [Serasa Experian](https://www.serasaexperian.com.br/) | Private credit/corp data | Commercial quote |
| [NewsAPI](https://newsapi.org/pricing) | News discovery, not source verification sub | Prev observed US$449/mo business price; recheck quotas/rights |

Discussion boards/social media can give leads, but claims need independent corroboration before entering report as facts. Add more country packs as companies added; each needs official registry, filings, insolvency/court, tax, regulator, sanctions, local biz news, credible private-data options.

## Historical sample monitoring row — Fertilizantes Heringer

ID: Fertilizantes Heringer S.A., Brazil, CNPJ **22.266.175/0001-88**, CVM code **20621**, ticker **FHER3**, per [B3 company record](https://sistemaswebb3-listados.b3.com.br/listedCompaniesPage/main/20621/FHER/overview?language=pt-br). [EuroChem announced](https://www.eurochemgroup.com/media/news/investors/eurochem-completes-purchase-of-controlling-share-in-fertilizantes-heringer) completion of 51.48% controlling stake purchase in March 2022; recheck current ownership in filings before each ownership-dependent conclusion.

| Event date | Finding | Credit relevance | Suggested severity | Source / verification |
| --- | --- | --- | --- | --- |
| 27 Aug 2026 | Company material fact re creditor mediation | Possible financing/liquidity pressure; read full notice + subsequent dev before current conclusion | High | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |
| 31 Aug 2026 | Company material fact re protective court relief | Direct legal/debt-enforcement relevance; track court record, creditor terms, cash pos | High | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |
| 9 Sep 2026 | Free-float compliance extension | Governance/market-listing watch item; lower direct credit impact than creditor dev | Medium | [Heringer material facts archive](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) |

Table shows report structure. Production run must fetch underlying notices, verify exact legal lang + later updates, include URLs to individual docs where avail. Must not repeat old events as new daily alerts.

## Storage and application architecture

Use **PostgreSQL** as system of record. Core tables: `entities`, `entity_aliases`, `entity_relationships`, `source_registry`, `source_documents`, `financial_periods`, `financial_metrics`, `events`, `event_evidence`, `risk_assessments`, `run_logs`, `reports`, `report_recipients`, `delivery_logs`. Add uniqueness constraint on reg country + reg num where valid; use stable internal IDs because aliases/ownership change.

Keep orig PDFs, HTML snapshots (if permitted), generated reports in object/file store; save paths, checksums, capture timestamps, provenance in PostgreSQL. PostgreSQL full-text search adequate initially. Add `pgvector` only if multilingual semantic recall/dedup shows measurable benefit. Use migrations, automated backups, retention rules from first deployed version.

Python handles ingestion, source adapters, entity matching, translation routing, dedup, event extraction, scheduling, XLSX/PDF gen. HTML app exposes watchlist, source evidence, review queue, run history, report downloads. Separate scheduled worker from web process so reports run without open browser.

## Skills and integrations

| Capability | Recommended tool / skill | Why |
| --- | --- | --- |
| Spreadsheet input/output | Python `openpyxl` or `xlsxwriter`; Codex Spreadsheets skill for manual workbook design | Reliable repeatable import/export, minimal model use |
| PDF extraction/output | Python `pymupdf`/`pdfplumber` and `reportlab`; Codex PDF skill for layout QA | Handle official reports, gen reviewable PDFs |
| Web application | Python FastAPI + server-rendered HTML initially; Codex Sites skill if building in Sites | Simple stack, path to richer UI later |
| Database | PostgreSQL, SQLAlchemy, Alembic | Durable relational history, controlled schema changes |
| Scheduling | Cron or managed scheduler, Python job queue if needed | Deterministic daily runs, retry handling |
| Email | Transactional email provider or connected mail plugin, once recipients/creds configured | Automated report delivery with logs |
| Search/news | Official APIs/RSS first; licensed search/news API if coverage gaps justify | High source quality, predictable rights/costs |
| Translation | Python lang detection + selective machine translation, [Google Cloud Translation pricing](https://cloud.google.com/products/translate/pricing) as benchmark | Translate only relevant passages; retain originals |
| Model review | Low-cost model for classification/extraction; stronger model for ambiguous/high-impact cases | Spend tokens where judgment affects report |

Check currently connected plugins when impl starts. GitHub connector useful for source control; mail connector/provider useful for delivery. No plugin required to store data in PostgreSQL. Keep provider interfaces replaceable.

## Running-cost model for 100–200 companies

Primary vars: search calls, premium data licensing, translation volume, how often model reads full docs. Budget with usage counters before selecting vendors.

| Monthly component | Planning assumption | Indicative range (USD) |
| --- | --- | ---: |
| PostgreSQL, app/worker hosting, storage, backups | Small managed deployment | 40–200 |
| Search/news discovery | Official feeds/public pages first | 0–500+ |
| Translation | Candidate snippets first; full text only on promoted finds | 0–100 |
| Model extraction/review | Rules/dedup reduce calls; escalate only material/ambiguous items | 20–250 |
| Email/report delivery | 1-2 daily reports to small list | 0–50 |
| **Core operating envelope** | Excludes premium credit/corp datasets + staff review | **60–1,100+** |

At 2 runs/day, 100–200 companies imply ~6,000–12,000 company-run checks per 30-day month. This is **not** necessarily 6,000–12,000 paid search calls: shared country/sector feeds, RSS, saved queries, change detection can reduce calls substantially. Commercial news API at prev published US$449/mo business price shifts lower bound up; premium registries/private credit data may need negotiated contracts. These are planning estimates, not vendor quotes or cost guarantee.

Measure cost per source hit, promoted event, company-run during pilot. Cache unchanged pages, hash docs, use deterministic extraction for structured filings, translate snippets before docs, send only evidence for ambiguous/high-severity cases to stronger model. Do not save tokens by dropping citations or skipping entity verification.

## Acceptance criteria for the first build

1. Import workbook, identify Fertilizantes Heringer by CNPJ, preserve every input field.
2. Run Portuguese/English source search with dated, linked evidence + clear entity matching.
3. Produce reviewed event table distinguishing fresh events from historical context; report when no new event found.
4. Download matching XLSX + PDF report from HTML app.
5. Configure daily schedule + email recipients; log run/delivery outcomes.
6. Show simple monthly usage/cost dashboard before expanding to 100–200 companies.

Recipient addresses, preferred run time/timezone, hosting choice, paid-data subscriptions remain config decisions for impl.
