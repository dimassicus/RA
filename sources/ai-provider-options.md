# Parallel search and independent review: proposed API design

Checked **31 October 2026**. These are **proposed integrations**, not active RA connections. The current app uses Python to query Google News RSS and Heringer investor-relations notices. No Perplexity, Gemini, or Kimi API credentials are configured.

## Recommendation

Run [Perplexity Search API](https://docs.perplexity.ai/docs/getting-started/pricing) and [Gemini with Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search/) as two parallel discovery paths. Send a merged, deduplicated subset of new or material leads to [Kimi K3.1 Pro](https://platform.kimi.ai/) for an independent review of entity identity, event date, claim, source agreement and contradictions. Keep direct regulator/company feeds as primary evidence.

Kimi should return a structured status—**supported by linked evidence**, **conflicting evidence**, or **insufficient evidence**—with the exact source URLs and the language of each source. It must not label a claim verified solely because Perplexity and Gemini agree; they may cite the same publisher or repeat one another's error. An analyst should confirm material adverse events against an original article, filing or court record before publication.

## Access and cost

| Component | Published entry rate | Suggested use |
| --- | --- | --- |
| [Perplexity Search API](https://docs.perplexity.ai/docs/getting-started/pricing) | **US$5/1,000 successful requests**, or **US$1/1,000** with Fast Search; one request can batch up to five queries | Raw ranked results and snippets with language, country, date and domain filters. Use standard search for material entities, test Fast Search for routine refreshes. |
| [Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/pricing) with [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search/) | Model **US$0.30/million input tokens** and **US$2.50/million output tokens**. Search: **5,000 queries/month included**, then **US$14/1,000 queries** on paid tier | Independent Google-grounded discovery. The model can issue more than one billable search query for a prompt; record actual search use. |
| [Kimi K3.1 Pro](https://platform.kimi.ai/) | **US$2/million input tokens**, **US$10/million output tokens** at the published standard rate | Review only prioritized leads. Kimi's own [web search and URL fetch tools](https://platform.kimi.ai/docs/pricing/websearch) are optional and billed separately; use them for unresolved claims. |

At **100–200 companies**, **one or two daily runs** mean roughly **3,000–12,000 company checks/month**. If each check uses one Perplexity request, that is **US$15–60/month** for standard search (or US$3–12 Fast Search). If each Gemini check triggers exactly one Google Search query, the search fee is **US$0–98/month** after the 5,000-query allowance, plus model tokens. Two Google queries per check would make the search fee about **US$14–266/month**. Kimi reviewing 10–25% of company checks with 2,000 input and 400 output tokens per review would add approximately **US$2.40–24/month** in model tokens; actual evidence size and reasoning tokens can raise this. These figures exclude taxes, article licences, translations, hosting and retries.

## Implementation order

1. Add provider adapters in Python with per-provider API keys in local environment variables or a secret store, never in the watchlist or Git.
2. Record every query, result URL, timestamp, provider, usage and fee in PostgreSQL. Deduplicate publishers and URLs before review.
3. Run the two discovery APIs concurrently with bounded timeouts, quotas and a daily spend limit. A provider failure should be visible in the run, not silently treated as no news.
4. Give Kimi only new, high-risk, conflicting or low-confidence leads plus the source material the app is licensed to process. Store its conclusion separately from the original evidence and from analyst selection.
5. Compare a pilot set of known Heringer events for recall, false matches, language coverage, citation quality, latency and cost before enabling all 100–200 companies.

**Source access is still required.** API keys and billing accounts are not present on this Mac, so this design is not activated by the current code. Pricing and quotas can change; recheck before enabling billing.
