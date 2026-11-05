"""Online translation for individual public headlines and monitoring terms."""
from functools import lru_cache
from html import unescape
from urllib.parse import urlencode

from .news_apis import ProviderError, fetch_json


@lru_cache(maxsize=2048)
def translate(text, source, target):
    if not text.strip() or len(text.encode("utf-8")) > 500:
        raise ProviderError("text outside MyMemory request limits")
    query = urlencode({"q": text, "langpair": f"{source}|{target}", "mt": 1})
    data = fetch_json("https://api.mymemory.translated.net/get?" + query)
    if str(data.get("responseStatus")) != "200" or data.get("quotaFinished"):
        raise ProviderError("MyMemory unavailable or quota reached")

    # Fuzzy translation-memory hits can describe a different amount or company.
    matches = data.get("matches")
    if not isinstance(matches, list):
        raise ProviderError("invalid MyMemory matches")
    for match in matches:
        if not isinstance(match, dict) or unescape(str(match.get("segment", ""))).strip() != text.strip():
            continue
        result = match.get("translation")
        if isinstance(result, str) and result.strip() and str(match.get("target", "")).split("-")[0].lower() == target:
            return unescape(result.strip())
    raise ProviderError("MyMemory returned no exact source match")
