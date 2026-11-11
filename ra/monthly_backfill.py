"""Recover company-specific leads for empty calendar months in a historical run."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

from . import keywords, news_apis, recovery_providers, search_settings
from .aliases import ambiguous, provider_name, related_names
from .article_content import canonical_url
from .idx_filings import text
from .model_store import country_languages, EXCLUDED_LANGUAGES
from .news_topics import excluded_story
from .scoring import WEEK_TIMEZONE
from .search import LOCALES, normalized, query_groups, signal_terms

# The full catalog runs during primary discovery. Keep archive queries short enough
# for both providers, with separate searches for names and issuer-anchored titles.
ROLES = {"CEO", "chief executive officer", "CFO", "board of directors", "main shareholder",
         "major shareholder", "beneficial owner"}
RISKS = {"asset seizure", "corruption", "smuggling", "drug trafficking", "profit decline", "bankruptcy"}


def missing_months(start, end, covered):
    cursor = start.astimezone(WEEK_TIMEZONE)
    end = end.astimezone(WEEK_TIMEZONE)
    while cursor <= end:
        month = cursor.strftime("%Y-%m")
        next_month = (cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                      + timedelta(days=32)).replace(day=1)
        if month not in covered:
            yield month, (cursor, min(next_month - timedelta(microseconds=1), end))
        cursor = next_month


def archive_queries(subject, provider):
    news = provider == "thenewsapi"
    queries = []
    try:
        queries.extend(news_apis.provider_queries(subject, "thenewsapi" if news else "gnews"))
    except news_apis.ProviderError:
        pass  # Other usable anchors can still retrieve long legal names.
    join = " + " if news else " "
    def groups(terms, length):
        quoted = ['"' + term.replace('"', '') + '"' for term in terms if 0 < len(term) <= length - 4]
        for group in query_groups(quoted, limit=6, max_chars=length - 2):
            yield group.replace('" OR "', '" | "') if news else group

    queries.extend(groups(related_names(subject), 200))
    country = subject.get("country_registration", "")
    name = subject.get("short_name") or provider_name(subject)
    if ambiguous(name):
        name = subject["name"]
    anchor = '"' + name.replace('"', '') + '"'
    if ambiguous(name) and country:
        anchor += join + '"' + country.replace('"', '') + '"'
    places = [country, *subject.get("primary_countries", "").replace(";", ",").split(",")]
    codes = []
    for place in places:
        if place.strip():
            codes.extend([LOCALES.get(normalized(place.strip()), ("en",))[0].split("-")[0],
                          *sorted(country_languages(place.strip()))])
    for code in dict.fromkeys([*codes, "en"]):
        if code in EXCLUDED_LANGUAGES:
            continue
        for terms in (keywords.person_title_terms(code, ROLES), keywords.adverse_search_terms(code, RISKS)):
            queries.extend(anchor + join + group for group in groups(terms, 200 - len(anchor + join)))
    return list(dict.fromkeys(queries))


def publisher_date(url):
    try:
        result = subprocess.run([sys.executable, "-m", "ra.article_content", "--publication-date", url],
                                capture_output=True, text=True, timeout=12, check=True)
        return datetime.fromisoformat(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def brave_search(query, bounds, subject, dates, seen, stats, *, language=None):
    days = "to".join(stamp.astimezone(timezone.utc).strftime("%Y-%m-%d") for stamp in bounds)
    params = dict(q=query, freshness=days, count=20, country="ALL", spellcheck="false")
    # Unsupported editions still search the reviewed native company names.
    if language in {"ar","de","el","en","es","fr","id","pt","ru","tr","vi"}:
        params["search_lang"] = language
    params = urlencode(params)
    data = recovery_providers.request("Brave", "/res/v1/web/search?" + params)
    rows = []
    web = data.get("web", {})
    if not isinstance(web, dict) or not isinstance(web.get("results", []), list):
        raise ValueError("Brave returned an invalid results list")
    for hit in web.get("results", []):
        if not isinstance(hit, dict):
            continue
        url = hit.get("url") or ""
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        try:
            key = canonical_url(url)
            publisher = urlsplit(url).hostname
        except ValueError:
            continue
        if key in seen or not publisher:
            continue
        title = text(str(hit.get("title") or ""))
        summary = text(str(hit.get("description") or ""))[:1500]
        row = dict(title=title, summary=summary, url=url, publisher=publisher, source_kind="news",
                   language=hit.get("language") or "", discovery_service="Brave Search", query=query)
        if not title or excluded_story(title, url, publisher=publisher, summary=summary) or not search_settings.company_lead(row, subject):
            continue
        if key not in dates:
            dates[key] = publisher_date(url)
        stamp = dates[key]
        if stamp is None:
            stats.add(key)
        elif bounds[0] <= stamp <= bounds[1]:
            rows.append(dict(row, published_at=stamp, signal_terms=signal_terms(title, row["language"])))
    return rows


def recover(subject, gaps, seen, progress):
    """Round-robin search months so one sparse month cannot consume the whole daily budget."""
    notes = {month: {"requests": 0, "added": 0} for month in gaps}
    errors, rows, dates = [], [], {}
    undated = {month: set() for month in gaps}
    plans = {}
    if recovery_providers.configured("Brave"):
        plans["Brave"] = archive_queries(subject, "Brave")
    if "thenewsapi" in subject.get("_news_providers", news_apis.configured_providers()):
        plans["thenewsapi"] = archive_queries(subject, "thenewsapi")
    if not plans:
        errors.append("Monthly backfill unavailable: no allocated archive provider.")
    failed_months = set()
    for index in range(max(map(len, plans.values()), default=0)):
        for month, bounds in gaps.items():
            for provider, queries in list(plans.items()):
                if index >= len(queries) or (month, provider) in failed_months:
                    continue
                query = queries[index]
                progress(f"Checking missing month {month} · {provider}")
                try:
                    if provider == "Brave":
                        found = brave_search(query, bounds, subject, dates, seen, undated[month])
                    else:
                        found = news_apis.search(provider, subject, queries=[query], window=bounds, first_page_only=True)
                    notes[month]["requests"] += 1
                except (ValueError, OSError, HTTPException, news_apis.ProviderError) as exc:
                    reason = str(exc) if isinstance(exc, (ValueError, news_apis.ProviderError)) else type(exc).__name__
                    exhausted = any(term in reason.lower() for term in ("budget", "limit reached", "allocation", "401", "402", "403", "429"))
                    if exhausted:
                        del plans[provider]
                    else:
                        failed_months.add((month, provider))
                    scope = "all remaining months" if exhausted else month
                    errors.append(f"Monthly backfill · {provider} · {month}: {reason}. Remaining queries skipped for {scope}.")
                    found = getattr(exc, "articles", [])
                for row in found:
                    stamp = row.get("published_at")
                    if not stamp or not bounds[0] <= stamp <= bounds[1]:
                        continue
                    key = canonical_url(row["url"])
                    if key not in seen and search_settings.company_lead(row, subject):
                        seen.add(key)
                        rows.append(row)
            if not plans:
                break
        if not plans:
            break
    for month, urls in undated.items():
        notes[month]["unconfirmed_dates"] = len(urls)
    return rows, notes, errors
