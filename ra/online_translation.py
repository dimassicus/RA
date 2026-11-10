"""Online translation for individual public headlines and monitoring terms."""
from functools import lru_cache
from html import unescape
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

from .news_apis import ProviderError, fetch_json


@lru_cache(maxsize=2048)
def translate_public(text, source, target):
    """Keyless public translation; never calls the paid Cloud Translation API."""
    if not text.strip() or len(text) > 500:
        raise ProviderError("text outside public translation request limits")
    query = urlencode({"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text})
    # shortcut: this public endpoint is undocumented; fail closed if its format/access changes.
    try:
        with urlopen(Request("https://translate.googleapis.com/translate_a/single?" + query,
                             headers={"User-Agent": "RA-Credit-Monitor/0.2"}), timeout=12) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("response too large")
        data = json.loads(raw)
        segments = data[0]
        if not isinstance(segments, list) or not segments:
            raise ValueError("missing segments")
        result = "".join(segment[0] for segment in segments if isinstance(segment[0], str)).strip()
        if not result or "".join(segment[1] for segment in segments) != text:
            raise ValueError("source mismatch")
        return result
    except (OSError, URLError, ValueError, TypeError, IndexError, KeyError) as exc:
        raise ProviderError("Public translation unavailable or invalid response") from exc


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
