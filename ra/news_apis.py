"""Optional GNews and TheNewsAPI discovery, with a persistent daily request cap."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone
from collections import deque
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

    def __init__(self, message, articles=()):
        super().__init__(message)
        self.articles = list(articles)


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
    limits = db.remaining_news_api_requests({provider: daily_limit(provider) for provider in providers})
    if len(providers) == 2 and len(subjects) <= min(limits.values()):
        return [{**subject, "_news_providers": providers, "_news_run_id": run_id,
                 "_news_request_limits": {p: limits[p] // len(subjects) for p in providers}} for subject in subjects]
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
    for subject in planned:
        subject["_news_run_id"] = run_id
        subject["_news_request_limits"] = {p: limits[p] // assigned[p] for p in subject["_news_providers"]}
    return planned


def provider_queries(subject, provider):
    """Search reviewed alternatives together, splitting only at the query-size limit."""
    from babel import Locale, UnknownLocaleError
    from .aliases import ambiguous, normalized, parse_aliases, provider_name
    from .search import LOCALES, normalized as country_key
    country = subject.get("country_registration", "")
    places = [country] if country else []
    locale = LOCALES.get(country_key(country))
    if locale:
        try:
            places.append(Locale.parse(locale[0], sep='-').territories.get(locale[1], country))
            places.append(Locale.parse('en').territories.get(locale[1], country))
        except (UnknownLocaleError, ValueError):
            pass
    join = " OR " if provider == "gnews" else " | "
    conjunction = " AND " if provider == "gnews" else " + "
    contexts = join.join('"' + place.replace('"', '') + '"' for place in dict.fromkeys(places) if place)
    names = [provider_name(subject), *parse_aliases(subject.get("aliases", "")),
             subject.get("short_name", ""), subject.get("name", "")]
    seen, queries, terms = set(), [], []
    for value in names:
        name = " ".join(value.split()).replace('"', '')
        canonical = normalized(name)
        if len(canonical) < 3 or canonical in seen:
            continue
        seen.add(canonical)
        term = f'"{name}"'
        if ambiguous(name) and contexts:
            term = f'({term}{conjunction}({contexts}))'
        if len(term) + 2 > 200:
            continue
        if terms and len(join.join([*terms, term])) + 2 > 200:
            queries.append("(" + join.join(terms) + ")")
            terms = []
        terms.append(term)
    if terms:
        queries.append("(" + join.join(terms) + ")" if len(terms) > 1 else terms[0])
    if seen and not queries:
        raise ProviderError("company names and context exceed the provider query limit")
    return queries


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "RA-Credit-Monitor/0.1"})
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(3_000_001)
    except HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        failure = "network request failed"
        for error_type, message in ((TimeoutError, "network request timed out"),
                                    (socket.gaierror, "DNS lookup failed"),
                                    (ssl.SSLError, "TLS connection failed"),
                                    (ConnectionError, "network connection failed")):
            if isinstance(reason, error_type):
                failure = message
                break
        raise ProviderError(failure) from None
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
        snippets = dict.fromkeys(str(record[k]).strip() for k in ('description', 'snippet', 'content') if record.get(k))
        summary = '\n'.join(snippets)[:2500]
        if excluded_story(title, url, publisher=publisher, categories=record.get("categories") or (), summary=summary):
            continue
        published = record.get("publishedAt") if provider == "gnews" else record.get("published_at")
        articles.append({
            "title": title, "url": url, "publisher": publisher,
            "published_at": _published(published), "query": query,
            "summary": summary, "publisher_keywords": record.get("keywords") or "",
            "language": record.get("lang" if provider == "gnews" else "language") or "",
            "signal_terms": signal_terms(title), "source_kind": "news",
            "discovery_service": "GNews" if provider == "gnews" else "TheNewsAPI",
        })
    return articles


def search(provider: str, subject: dict, fetcher=fetch_json,
           reserve=db.reserve_news_api_request, now: datetime | None = None, *, window=None, queries=None, first_page_only=False) -> list[dict]:
    """Page alternative-name queries within the company's reserved share of daily usage."""
    key_env, _, _ = PROVIDERS[provider]
    key = os.environ.get(key_env, "").strip()
    if not key:
        return []
    if queries is None:
        queries = provider_queries(subject, provider)
    if not queries:
        return []
    now = now or datetime.now(timezone.utc)
    start, end = window or (now - timedelta(days=7), now)
    since = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if provider == "gnews":
        from .search import LOCALES, normalized
        locale = LOCALES.get(normalized(subject.get("country_registration", "")), ("en",))[0]
        expanded = []
        for query in queries:
            for term in excluded_query_terms(locale):
                exclusion = f' NOT "{term}"'
                if len(query + exclusion) <= 200:
                    query += exclusion
            expanded.append(query)
        queries = expanded
        params = {"max": 10, "from": since, "to": until, "sortby": "publishedAt", "apikey": key,
                  "in": "title,description,content", "nullable": "description,content,image"}
        endpoint, query_key, result_key, provider_cap = "https://gnews.io/api/v4/search", "q", "articles", 1000
    else:
        # Omitting limit uses the account's page size rather than imposing the free-tier size.
        params = {"published_after": since.removesuffix("Z"), "published_before": until.removesuffix("Z"),
                  "sort": "published_on", "api_token": key, "exclude_categories": "sports,entertainment"}
        endpoint, query_key, result_key, provider_cap = "https://api.thenewsapi.com/v1/news/all", "search", "data", 20000
    limit = subject.get("_news_request_limits", {}).get(provider, daily_limit(provider))
    pending = deque((query, 1) for query in queries)
    articles, urls, pages = [], set(), set()
    requests = 0
    gaps = []
    empty_pages = set()
    while pending:
        query, page = pending.popleft()
        try:
            if requests >= limit:
                raise ProviderError("company request allocation reached; additional pages or names remain unchecked")
            if not reserve(provider, daily_limit(provider)):
                raise ProviderError("daily request budget exhausted; additional pages or names remain unchecked")
            requests += 1
            payload = fetcher(endpoint + "?" + urlencode(params | {query_key: query, "page": page}))
            found = parse_articles(provider, payload, query)
            raw = payload[result_key]
            fingerprint = (query, hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest())
            if raw and fingerprint in pages:
                raise ProviderError("provider repeated a page; remaining results could not be retrieved")
            pages.add(fingerprint)
            added = 0
            for article in found:
                if article["url"] not in urls:
                    urls.add(article["url"])
                    articles.append(article)
                    added += 1
            meta = payload.get("meta") or {}
            if not isinstance(meta, dict):
                raise ProviderError("invalid pagination metadata; remaining results could not be retrieved")
            size = 10 if provider == "gnews" else meta.get("limit", 3)
            total = payload.get("totalArticles") if provider == "gnews" else meta.get("found")
            if type(size) is not int or size < 1 or (total is not None and (type(total) is not int or total < 0)):
                raise ProviderError("invalid pagination metadata; remaining results could not be retrieved")
            logging.getLogger(__name__).info("news_api_page %s", json.dumps(dict(
                run_id=subject.get("_news_run_id"), provider=provider, subject=subject.get("name"),
                query=query, page=page, total=total, returned=len(raw), accepted=len(found), added=added,
                in_window=sum(bool(a["published_at"] and start <= a["published_at"] <= end) for a in found))))
            removed = payload.get("articlesRemovedFromResponse")
            historical = provider == "gnews" and isinstance(removed, dict) and "historicalArticles" in removed
            if historical:
                gaps.append("GNews excluded historical articles under the free plan's 30-day archive limit; coverage is incomplete")
                if not raw:
                    # This query is newest first; older pages cannot restore the excluded history.
                    continue
            if total is not None and not raw and (page - 1) * size < total:
                gaps.append(f"provider reported {total} matches but returned no articles on page {page}; coverage is incomplete")
                if not first_page_only and query not in empty_pages and page * size < min(total, provider_cap):
                    pending.append((query, page + 1))
                    empty_pages.add(query)
                continue
            more = bool(raw) and (page * size < total if total is not None else len(raw) >= size)
            if more and not first_page_only:
                if page * size >= provider_cap:
                    raise ProviderError("provider pagination ceiling reached; narrow the date range for remaining articles")
                pending.append((query, page + 1))
        except ProviderError as exc:
            logging.getLogger(__name__).warning("news_api_incomplete %s", json.dumps(dict(
                run_id=subject.get("_news_run_id"), provider=provider, subject=subject.get("name"),
                page=page, requests=requests, retained=len(articles), reason=str(exc))))
            raise ProviderError("; ".join(dict.fromkeys([*gaps, str(exc)])), articles) from None
    if gaps:
        raise ProviderError("; ".join(dict.fromkeys(gaps)), articles)
    return articles
