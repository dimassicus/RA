"""Dated company discovery and follow-up coverage for published decisions."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import re
from urllib.parse import urlsplit

from . import company_developments, news_apis, recovery_providers, search_settings
from .aliases import discovery_names
from .article_content import canonical_url
from .idx_filings import text
from .news_topics import excluded_story
from .scoring import WEEK_TIMEZONE


def search(query, subject, start, end):
    payload = recovery_providers.request('Perplexity', '/search', body={
        'query': query, 'max_results': 10, 'max_tokens_per_page': 600,
        'search_after_date_filter': (start - timedelta(days=1)).strftime('%m/%d/%Y'),
        'search_before_date_filter': (end + timedelta(days=1)).strftime('%m/%d/%Y')})
    if not isinstance(payload.get('results'), list):
        raise ValueError('Perplexity returned an invalid results list')
    rows = []
    for hit in payload['results']:
        if not isinstance(hit, dict):
            continue
        url = hit.get('url')
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):
                continue
            url = canonical_url(url)
        except ValueError:
            continue
        title = text(str(hit.get('title') or ''))
        if re.search(r'/page/\d+/?$', parts.path) and title.casefold() in {
                'news', 'disclosure', 'disclosures', 'keterbukaan informasi'}:
            continue
        summary = text(str(hit.get('snippet') or ''))[:2500]
        stamp = news_apis._published(hit.get('date'))
        if stamp is None or not start <= stamp <= end:
            continue
        row = dict(url=url, title=title, summary=summary, publisher=parts.hostname, published_at=stamp,
                   discovery_service='Perplexity', source_kind='news', language='', query=query, signal_terms=[])
        if title and not excluded_story(title,url,publisher=parts.hostname,summary=summary) and search_settings.company_lead(row, subject):
            rows.append(row)
    return rows


def collect(subject, window, seeds=(), progress=None):
    if not recovery_providers.configured('Perplexity'):
        return [], []
    from .search import LOCALES, normalized
    now = datetime.now(timezone.utc)
    start, end = (v.astimezone(WEEK_TIMEZONE) for v in (window or (now-timedelta(days=7), now)))
    names = discovery_names(subject)
    if not names:
        return [], ['Perplexity: missing company identity']
    anchor = f'"{names[0]}"'
    locale = LOCALES.get(normalized(subject.get('country_registration','')), ('en',))[0].split('-')[0]
    aliases = names[1:]
    local_name = max(aliases, key=lambda s: any(ord(ch)>127 for ch in s), default=anchor)
    local_terms = company_developments.TERMS[company_developments.ASSETS].get(locale, ())
    from . import keywords
    terms = [*keywords.adverse_search_terms(locale, {'profit decline','asset seizure','bankruptcy'})[:3], *local_terms[:2]]
    queries = list(dict.fromkeys([f'{anchor} financial results debt credit risk issuer disclosures',
                                 f'{anchor} {local_name if local_name != anchor else ""} {subject.get("country_registration","")} {" ".join(terms) or "court ruling appeal enforcement outcome"}']))
    found, errors = {}, []
    def lookup(q, lo, hi, notice=None):
        if progress:
            progress('Searching Perplexity · '+q)
        try:
            for hit in search(q, subject, lo, hi):
                found.setdefault(hit['url'], hit)
                if notice and hit['url'] != canonical_url(notice['url']) and hit['published_at'] >= notice['published_at']:
                    links = notice.setdefault('decision_followup_urls', [])
                    if hit['url'] not in links:
                        links.append(hit['url'])
        except (ValueError, OSError, HTTPException) as exc:
            errors.append(str(exc) if isinstance(exc,ValueError) and str(exc).startswith('Perplexity') else 'Perplexity request failed')
            return False
        return True

    lo = start
    while lo <= end:
        next_quarter = (lo.month - 1) // 3 * 3 + 4
        boundary = lo.replace(year=lo.year + (next_quarter > 12), month=(next_quarter-1)%12+1,
                              day=1, hour=0, minute=0, second=0, microsecond=0)
        hi = min(end, boundary-timedelta(microseconds=1))
        for q in queries:
            if not lookup(q,lo,hi):
                return list(found.values()), errors
        lo = boundary
    seen = set()
    for article in [*seeds, *found.values()]:
        stamp = article.get('published_at')
        if isinstance(stamp,str):
            stamp = news_apis._published(stamp)
        decision = company_developments.decision_notice(article)
        petition = company_developments.petition_notice(article)
        if not stamp or not start <= stamp <= end or not article.get('url') or not (decision or petition):
            continue
        title = article['title'].split(' - ')[0][:170]
        key = (canonical_url(article['url']),stamp.date())
        if key in seen:
            continue
        seen.add(key)
        q = f'{anchor} {title} {stamp:%B %Y} decision outcome partial relief remaining restrictions'
        # The saved notice records which independent searches may corroborate its disposition.
        article['published_at'] = stamp
        if not lookup(q, max(start,stamp-timedelta(days=3)), min(end,stamp+timedelta(days=7)) if decision else end, article):
            break
    return list(found.values()), errors
