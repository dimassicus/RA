# Global and cross-border sources

Checked 31 October 2026. These complement each country's official registry and filings; they should not replace them.

| Source | Coverage | Access and entry cost | Practical limitation |
| --- | --- | --- | --- |
| [EuroChem corporate news](https://www.eurochemgroup.com/media/news/) | Parent/owner statements and releases | **Free web**; no official API or pricing identified. | Company statements are primary for what the company says, but financial implications need independent checking. |
| [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api/) | LEI legal names, registration IDs, addresses and relationships globally | **Free public API**, no key required; integrated in preliminary intake. | LEI coverage is incomplete for smaller firms. Compare the registration ID and jurisdiction; a fuzzy name result alone is not identity proof. |
| [GDELT](https://gdeltproject.org/data.html) | Multilingual global news discovery, events, and country context | **Free public JSON APIs and raw downloads**; no GDELT subscription fee published. | High recall can produce false matches; verify against the original publisher. BigQuery processing can incur separate cloud charges. |
| [ReliefWeb](https://apidoc.reliefweb.int/) | Curated humanitarian, disaster and country reports | **Free public JSON API**; an approved `appname` is required. | Useful for country shocks, not comprehensive company news; respect original-source rights. |
| [UN News](https://www.un.org/uk/delegate/page/un-news-outlets) | Multilingual country and policy news | **Free public web and feeds**; specific feed URL and reuse terms need validation. | Country context rather than corporate filings. |
| [Google News RSS](https://news.google.com/rss/search?q=%22Fertilizantes%20Heringer%22&hl=pt-BR&gl=BR&ceid=BR:pt) | Local-language news discovery | **Free public RSS endpoint observed in pilot**; no contracted API, SLA, or reuse licence verified. | **Integrated in pilot as discovery only**. Do not rely on it as sole production source. |
| [Reuters news website](https://www.reuters.com/world/americas/) | Global business, country, and sector reporting | Public web access varies; no general free Reuters API identified. | Linking to a story differs from licensing its text for distribution. |
| [Reuters Connect](https://www.reutersconnect.com/plans-and-pricing) | Licensed Reuters/partner text and media, API delivery | **Paid licence/API**; free account to browse; subscription entry price **quote only**. | Text licensing and internal distribution rights depend on contract. |
| [OpenCorporates](https://opencorporates.com/pricing/) | Cross-border company discovery and registry aggregation | **Paid commercial API**: Essentials **£225/month** (500 calls/month, 200/day) or **£2,250/year**; next Starter **£660/month** (2,500 calls/month). Eligible public-benefit projects can request free access. | Verify jurisdiction coverage and terms; source registers remain authoritative. |
| [NewsAPI](https://newsapi.org/pricing) | Multi-publisher news discovery API | **Free Developer** plan: 100 requests/day, 24-hour article delay, development/testing **only**. First production plan **Business US$449/month**, 250,000 requests/month; extra requests US$0.0018 each. | API returns metadata and links, not full article text. |

## Related processing service, not a factual source

[Google Cloud Translation](https://cloud.google.com/products/translate/pricing) offers an API for translating candidate text. Its standard neural text tier applies a **US$10 monthly credit** (equivalent to the first 500,000 characters at the listed rate), then **US$20 per million characters** at the entry consumption tier. Translation costs multiply by target language; document translation has different rates. Preserve original-language evidence.
