# RA source registry

Checked: **2 November 2026** for [six newly added registration countries](new-countries.md); **31 October 2026** for the earlier [Brazil](brazil.md) and [global and cross-border sources](global.md). [Free news sources](free-news.md) separates machine-readable feeds from free-to-read websites and records commercial-use restrictions. The [parallel AI search and review proposal](ai-provider-options.md) compares Perplexity, Gemini and Kimi. [registry.tsv](registry.tsv) is the machine-readable inventory for future database import.

**Access terms used here**

- **Free API/data/RSS**: a public machine-readable interface or downloadable dataset was identified. It may still have rate limits, attribution, registration, or use restrictions.
- **Free web**: a person can consult the public page without a source fee. This does not grant bulk scraping or redistribution rights.
- **Paid web**: a reader subscription or per-report purchase. This does not include API or redistribution rights.
- **Paid API/licence**: programmatic/commercial access requires a contract or a published paid plan.
- **Unverified**: the source or a specific interface is a candidate, but automated access and terms need a pilot check.

No **source-specific MCP server** was verified for these providers. The actionable interfaces found are CSV/ZIP/JSON downloads, APIs, RSS, public webpages, and commercial feeds. A generic web-search or browser MCP is an access tool, not the source's own official data product.

The pilot app currently uses a public Heringer investor-relations JSON endpoint and Google News RSS for monitoring. Preliminary company intake additionally checks GLEIF LEI records globally and BrasilAPI CNPJ data for Brazilian entries. Google News editions for Egypt, Greece, Vietnam, Azerbaijan, Uzbekistan and Turkey returned RSS responses in a local test on 2 November 2026. These sources provide leads and identity suggestions, not a substitute for analyst confirmation. The CVM publishes open CSV/ZIP datasets, but a direct CSV request from this development environment returned HTTP 403; ingestion needs an access check before it is treated as operational.

Source pricing is recorded for **entry access only** and is not a quote for storing full articles or emailing their contents. Check licences, quotas, and current offers before purchasing. For the monitoring system, store links, metadata, and short internal analysis; preserve original filings as permitted by their source terms.

## Adding another country

When a new registration country appears in the watchlist, add a country pack and registry rows covering its official company register, financial filing portal, courts, insolvency gazette, sector regulators, government data, local-language business publishers and relevant global feeds. Record access method, price, language, update frequency and a tested company identifier. A separate advance list of countries can speed this research; it is not required to add companies. The pilot app does not yet automatically discover or connect a country's sources from its watchlist entry.
