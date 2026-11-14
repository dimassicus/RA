"""Online translation for bounded passages and monitoring terms."""
from functools import lru_cache
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

from .news_apis import ProviderError


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


def google_passage(text, source, target):
    from .article_translation import passages
    return " ".join(translate_public(part.strip(), source, target)
                    for part in passages(text, limit=500) if part.strip())
