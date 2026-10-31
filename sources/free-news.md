# Free news sources for monitoring

Checked **31 October 2026**. “Free” below means no source subscription fee for the stated access, not unrestricted article storage, translation, or email redistribution. Use headlines, dates, links and brief original analysis in RA reports; confirm material claims in the original article or a primary filing. No source-specific MCP server has been verified.

## Best machine-readable starting points

| Source | Coverage and languages | Free access | Fit and limitation |
| --- | --- | --- | --- |
| [GDELT DOC API](https://gdeltproject.org/data.html) | Global online news; searches coverage translated from many languages | Public JSON, CSV and RSS; **US$0 source fee** | Strong cross-border company discovery. Search output is leads, not verified facts; expect duplicate and name-match noise. [API documentation](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). |
| [Google News RSS](https://news.google.com/rss/search?q=%22Fertilizantes%20Heringer%22&hl=pt-BR&gl=BR&ceid=BR:pt) | Global and local-language publisher discovery | Public RSS observed; **US$0 source fee** | **Already integrated in the pilot.** Uncontracted endpoint; no verified SLA, coverage guarantee or reuse licence. Link through to the original publisher. |
| [Agência Brasil RSS](https://agenciabrasil.ebc.com.br/feed/) | Brazil; Portuguese, including economy and justice sections | Publisher RSS and public site; **R$0 source fee** | Useful Brazil country and sector context. RSS page lists topic feeds; article reuse terms need separate checking. |
| [Agência Câmara RSS](https://www.camara.leg.br/noticias/rss) | Brazilian legislative, economic, agricultural and environmental news; Portuguese | Publisher RSS by topic; **R$0 source fee** | Good for proposed law and policy changes; legislation still needs status verification. The [publisher's guidance](https://infograficos.camara.leg.br/manual-de-comunicacao/agencia-camara/) allows credited reproduction, but RA should link to the source. |
| [ReliefWeb API](https://apidoc.reliefweb.int/) | Curated humanitarian, disaster and country reports; multilingual source material | Public JSON API; **US$0 source fee**, but an approved `appname` is required | Useful for country disruption, conflict and natural disaster context; limited company coverage. Original-source rights still apply. |
| [UN News](https://www.un.org/uk/delegate/page/un-news-outlets) | Global policy, conflict and country context; English, French, Arabic, Spanish, Portuguese, Kiswahili and others | Public website, feeds and news alerts; **US$0 source fee** | Valuable country-risk context; not a company-news search API. Confirm the specific feed endpoint and reuse terms before integration. |

## Free reading, but automated commercial use needs more checking

| Source | Access | Why it is useful / restriction |
| --- | --- | --- |
| [Brazil Journal](https://braziljournal.com/) | Public web; no official API verified | Brazilian companies, ownership and transactions. Check access to each article and cite original publisher. |
| [AgFeed](https://agfeed.com.br/) | Public web observed; no official API verified | Brazilian agriculture and fertilizer sector reporting; already surfaced a Heringer headline in the pilot search. |
| [Notícias Agrícolas](https://www.noticiasagricolas.com.br/noticias/) | Public web and [free newsletter](https://www.noticiasagricolas.com.br/notificacoes/); no official API verified | Crop, fertilizer, commodity and agribusiness context. Newsletter is for reading, not automatically a data licence. |
| [Deutsche Welle](https://amp.dw.com/en/benefit-from-smart-content-made-in-germany/a-19470839) | Public multilingual web; RSS or content arrangements through DW | Broad regional coverage, including Arabic, French, Portuguese and Spanish. Its German News Service offers free material for eligible media partners, with registration/terms; do not assume the same rights for RA. |
| [Reuters website](https://www.reuters.com/world/americas/) | Public article access varies; no free commercial API verified | High-quality global reporting, but [Reuters Connect](https://www.reutersconnect.com/plans-and-pricing) requires a licensed contract for API/content delivery. |
| [BBC News RSS](https://support.bbc.co.uk/platform/feeds/NewsFeeds.htm) | Public RSS for reading | The [BBC terms](https://downloads.bbc.co.uk/usingthebbc/bbc_terms_of_use_31March2022english.pdf) require permission for business use of RSS/metadata and say a fee may apply. Do not classify it as a free production feed. |
| [The Guardian Open Platform](https://open-platform.theguardian.com/access/) | Free API key for **non-commercial** use, up to 500 calls/day | Useful for testing and research; a commercial RA deployment needs a priced-by-usage key. |

## Practical order for RA

1. Keep Heringer's official notices and Google News RSS for the first company, then add GDELT as an independent discovery path.
2. Add Agência Brasil and Câmara RSS for Brazil country, agricultural policy and sector signals.
3. Use ReliefWeb and UN News for relevant country shocks as the watchlist expands; add country-specific publishers when new countries enter the list.
4. Deduplicate by original URL and normalized headline, retain original language and publication time, and label each finding as a lead until a source is checked.

The pilot app currently **implements only Google News RSS and Heringer's investor-relations notices**. The other feeds above are researched candidates, not running adapters.
