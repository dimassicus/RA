"""Optional GNews and TheNewsAPI discovery, with a persistent daily request cap."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from . import db
from .news_topics import excluded_query_terms, excluded_story


PROVIDERS = {
    "gnews": ("RA_GNEWS_API_KEY", "RA_GNEWS_DAILY_LIMIT", 95),
    "thenewsapi": ("RA_THENEWSAPI_API_KEY", "RA_THENEWSAPI_DAILY_LIMIT", 95),
}


class ProviderError(Exception):
    """A provider failure whose message never contains the API key or request URL."""


def configured_providers() -> list[str]:
    return [name for name, (env_name, _, _) in PROVIDERS.items() if os.environ.get(env_name, "").strip()]


def daily_limit(provider: str) -> int:
    _, env_name, default = PROVIDERS[provider]
    try:
        return max(0, int(os.environ.get(env_name, str(default))))
    except ValueError:
        return default


def plan_subjects(subjects: list[dict], run_id: int, now: datetime | None = None) -> list[dict]:
    """Spread limited API coverage across a large watchlist over successive days."""
    providers = configured_providers()
    if not providers or not subjects:
        return subjects
    limits = {provider: daily_limit(provider) for provider in providers}
    if len(providers) == 2 and len(subjects) <= min(limits.values()):
        return [{**subject, "_news_providers": providers} for subject in subjects]
    now = now or datetime.now(timezone.utc)
    offset = (now.astimezone(timezone.utc).date().toordinal() * 97 + run_id) % len(subjects)
    ordered = subjects[offset:] + subjects[:offset]
    assigned = {provider: 0 for provider in providers}
    planned = []
    for subject in ordered:
        eligible = [provider for provider in providers if assigned[provider] < limits[provider]]
        if eligible:
            provider = min(eligible, key=lambda item: assigned[item] / max(1, limits[item]))
            assigned[provider] += 1
            planned.append({**subject, "_news_providers": [provider]})
        else:
            planned.append({**subject, "_news_providers": []})
    return planned


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "RA-Credit-Monitor/0.1"})
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(3_000_001)
    except HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError("network request failed") from None
    if len(raw) > 3_000_000:
        raise ProviderError("response exceeded size limit")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProviderError("invalid JSON response") from None
    if not isinstance(payload, dict):
        raise ProviderError("unexpected JSON response")
    return payload


def _published(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_articles(provider: str, payload: dict, query: str) -> list[dict]:
    from .search import signal_terms

    if payload.get("error") or payload.get("errors"):
        raise ProviderError("provider reported an error")
    records = payload.get("articles" if provider == "gnews" else "data")
    if not isinstance(records, list):
        raise ProviderError("unexpected article list")
    articles = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or "").strip()
        url = str(record.get("url") or "").strip()
        if not title or urlparse(url).scheme != "https" or not urlparse(url).hostname:
            continue
        source = record.get("source")
        publisher = str(source.get("name") or "") if isinstance(source, dict) else str(source or "")
        if excluded_story(title, url, publisher=publisher, categories=record.get("categories") or (),
                          summary=str(record.get("description") or record.get("snippet") or "")):
            continue
        published = record.get("publishedAt") if provider == "gnews" else record.get("published_at")
        articles.append({
            "title": title, "url": url, "publisher": publisher,
            "published_at": _published(published), "query": query,
            "language": record.get("lang" if provider == "gnews" else "language") or "",
            "signal_terms": signal_terms(title), "source_kind": "news",
            "discovery_service": "GNews" if provider == "gnews" else "TheNewsAPI",
        })
    return articles


def search(provider: str, subject: dict, fetcher=fetch_json,
           reserve=db.reserve_news_api_request, now: datetime | None = None) -> list[dict]:
    """One preferred-name request per provider; leave entity verification to the analyst."""
    key_env, _, _ = PROVIDERS[provider]
    key = os.environ.get(key_env, "").strip()
    if not key:
        return []
    from .aliases import provider_name
    name = provider_name(subject)
    if not name:
        return []
    now = now or datetime.now(timezone.utc)
    since = (now.astimezone(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    phrase = f'"{name}"'
    if provider == "gnews":
        if len(phrase) > 200:
            raise ProviderError("company name exceeds GNews query limit")
        from .search import LOCALES, normalized
        locale = LOCALES.get(normalized(subject.get("country_registration", "")), ("en",))[0]
        query = phrase
        for term in excluded_query_terms(locale):
            exclusion = f' NOT "{term}"'
            if len(query + exclusion) <= 200:
                query += exclusion
        params = {"q": query, "max": 10, "from": since, "sortby": "publishedAt", "apikey": key}
        endpoint = "https://gnews.io/api/v4/search"
    else:
        # TheNewsAPI accepts UTC without a timezone suffix (GNews requires it).
        params = {"search": phrase, "published_after": since.removesuffix("Z"), "sort": "published_on",
                  "limit": 3, "api_token": key, "exclude_categories": "sports,entertainment"}
        endpoint = "https://api.thenewsapi.com/v1/news/all"
    if not reserve(provider, daily_limit(provider)):
        raise ProviderError("daily request budget exhausted; this company was not checked")
    return parse_articles(provider, fetcher(endpoint + "?" + urlencode(params)), phrase)
