# RA source registry

Checked: **3 November 2026** for [GNews and TheNewsAPI](news-api-providers.md), the [official-authority coverage checklist](official-authorities.md), [people and biography sources](people-sources.md), [regulator and law-enforcement watchlists](regulatory-watchlists.md), and [user-suggested regional publishers](publisher-candidates.md); **2 November 2026** for [six newly added registration countries](new-countries.md); **31 October 2026** for the earlier [Brazil](brazil.md) and [global and cross-border sources](global.md). [Free news sources](free-news.md) separates machine-readable feeds from free-to-read websites and records commercial-use restrictions. The [parallel AI search and review proposal](ai-provider-options.md) compares Perplexity, Gemini and Kimi. [registry.tsv](registry.tsv) is the machine-readable inventory for future database import.

**Access terms used here**

- **Free API/data/RSS**: a public machine-readable interface or downloadable dataset was identified. It may still have rate limits, attribution, registration, or use restrictions.
- **Free web**: a person can consult the public page without a source fee. This does not grant bulk scraping or redistribution rights.
- **Paid web**: a reader subscription or per-report purchase. This does not include API or redistribution rights.
- **Paid API/licence**: programmatic/commercial access requires a contract or a published paid plan.
- **Unverified**: the source or a specific interface is a candidate, but automated access and terms need a pilot check.

GNews now publishes an [official MCP server](https://docs.gnews.io/mcp-server); each MCP tool call counts against its API quota. RA uses its REST API in Python to keep the monitoring path deterministic and inexpensive. No source-specific MCP server was verified for the other listed providers. The other actionable interfaces found are CSV/ZIP/JSON downloads, APIs, RSS, public webpages, and commercial feeds.

The pilot app currently uses a public Heringer investor-relations JSON endpoint, Google News RSS, recent FCA warning RSS name leads for UK exposure, and optional keyed GNews/TheNewsAPI searches. Preliminary company intake additionally checks GLEIF LEI records globally and BrasilAPI CNPJ data for Brazilian entries. Google News editions for Egypt, Greece, Vietnam, Azerbaijan, Uzbekistan and Turkey returned RSS responses in a local test on 2 November 2026. These sources provide leads and identity suggestions, not a substitute for analyst confirmation. The CVM publishes open CSV/ZIP datasets, but a direct CSV request from this development environment returned HTTP 403; ingestion needs an access check before it is treated as operational.

Source pricing is recorded for **entry access only** and is not a quote for storing full articles or emailing their contents. Check licences, quotas, and current offers before purchasing. For the monitoring system, store links, metadata, and short internal analysis; preserve original filings as permitted by their source terms.

## Adding another country

When a new registration country appears in the watchlist, use the [official-authority coverage checklist](official-authorities.md) to add a country pack and registry rows, then add local-language business publishers and relevant global feeds. Record access method, price, language, update frequency and a tested company identifier. A separate advance list of countries can speed this research; it is not required to add companies. The pilot app does not yet automatically discover or connect a country's sources from its watchlist entry.
