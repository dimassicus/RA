"""Grounded Gemini discovery for an issuer, scoped to the run's saved provider settings."""
import http.client
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from . import company_developments, decision_review, news_apis, recovery_providers, search_settings
from .aliases import discovery_names
from .article_content import canonical_url, public_target
from .news_topics import excluded_story

MODEL = 'gemini-4-carbon'
GOOGLE = 'generativelanguage.googleapis.com'
ENFORCEMENT_QUERY = ('Check regulator statements and dated local reporting for arrests, detention, or corruption '
                     'investigations involving the company or its current executives. Identify the executive, '
                     'employer, role and procedural stage. Omit unrelated cases.')
REDIRECTORS = {'vertexaisearch.cloud.google.com', 'grounding-api-redirect.googleusercontent.com'}
LINE = re.compile(r'^\s*(?:[-*•]|\d+[.)])?\s*\**(\d{4}-\d\d-\d\d)\**\s*\|\s*([^|\n]+?)\s*\|\s*([^\n]+)', re.M)
FINANCIAL = re.compile(r'financial statement|financial results|laporan keuangan|earnings|net profit|laba bersih', re.I)
PERIOD = re.compile(r'\b(Q[1-4]|H[12]|FY|9M)\s*[-/]?\s*(20\d\d)\b', re.I)
CHANGE = re.compile(r'\b(?:net profit|net income|laba bersih)\s+(?:fell|declined|dropped|turun|rose|grew|naik)\s+(?:by\s+)?(\d+(?:[.,]\d+)?)\s*%', re.I)


def request(path, body=None):
    client = http.client.HTTPSConnection(GOOGLE, timeout=180)
    try:
        client.request('POST' if body else 'GET', path, json.dumps(body) if body else None, {
            'x-goog-api-key': os.environ['RA_GOOGLE_API_KEY'], 'Api-Revision':'2026-05-20',
            'Content-Type':'application/json'})
        response = client.getresponse()
        if response.status != 200:
            raise ValueError(f'Google API HTTP {response.status}')
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('Google response exceeded size limit')
        return json.loads(raw)
    finally:
        client.close()


def publisher_url(url):
    try:
        parts = urlsplit(url)
        if parts.hostname in REDIRECTORS and parts.scheme == 'https' and parts.port in (None, 443):
            conn = http.client.HTTPSConnection(parts.hostname, timeout=10)
            try:
                conn.request('GET', parts.path + ('?' + parts.query if parts.query else ''))
                res = conn.getresponse()
                if res.status not in (301, 302, 303, 307, 308) or not res.getheader('Location'):
                    return ''
                url = urljoin(url, res.getheader('Location'))
            finally:
                conn.close()
        if urlsplit(url).hostname in REDIRECTORS:
            return ''
        public_target(url)
        return canonical_url(url)
    except (ValueError, OSError, http.client.HTTPException):
        return ''


def cited_rows(data, subject, bounds, query):
    for step in data.get('steps', []):
        if step.get('type') != 'model_output':
            continue
        for block in step.get('content', []):
            if block.get('type') != 'text':
                continue
            text = block['text']
            for line in LINE.finditer(text):
                # Google offsets are UTF-8 bytes. Bind citations to one complete record.
                lo, hi = (len(text[:i].encode()) for i in line.span())
                urls = {a['url'] for a in block.get('annotations', []) if a.get('type') == 'url_citation'
                        and isinstance(a.get('url'), str) and isinstance(a.get('start_index'), int)
                        and isinstance(a.get('end_index'), int) and lo <= a['start_index'] < a['end_index'] <= hi + 1}
                day = news_apis._published(line[1])
                if len(urls) != 1 or not day or not bounds[0] <= day <= bounds[1]:
                    continue
                url = publisher_url(urls.pop())
                title, summary = line[2].strip(' *')[:1000], line[3].strip(' *')[:2500]
                if not url or excluded_story(title, url, publisher=urlsplit(url).hostname, summary=summary):
                    continue
                row = dict(title=title, summary=summary, url=url, publisher=urlsplit(url).hostname,
                           source_kind='news', published_at=day, discovery_service='Google Gemini 4 Carbon',
                           query=query, signal_terms=[], language='')
                if search_settings.company_lead(row, subject):
                    yield row


def lookup(subject, bounds, query):
    prompt = (f"Use Google Search for {subject['name']} ({', '.join(discovery_names(subject, limit=None))}), "
              f"{subject.get('country_registration','')}. Search local-language, English and Russian sources published "
              f"between {bounds[0]:%Y-%m-%d} and {bounds[1]:%Y-%m-%d}. {query}. "
              "Return one line per original source with exactly one grounding citation attached to that same line. "
              "Format each line YYYY-MM-DD | original headline | English summary of the publisher’s explicit issuer or executive link and reported outcome. "
              "Use the publication date. Preserve proper names. Omit undated, uncited or unrelated records. Never infer the employer from the search query or country.")
    data = request('/v1beta/interactions', dict(model=MODEL, input=prompt, tools=[dict(type='google_search')]))
    deadline = time.monotonic() + 180
    while data.get('status') == 'in_progress' and time.monotonic() < deadline:
        if not re.fullmatch(r'[\w.-]+', data.get('id', '')):
            raise ValueError('Google returned an invalid interaction ID')
        time.sleep(3)
        data = request('/v1beta/interactions/' + data['id'])
    if data.get('status') != 'completed':
        raise ValueError('Google grounded search did not complete')
    return list(cited_rows(data, subject, bounds, query))


def searches(articles, coverage, bounds, primary, provider_gaps, adverse_focus=False, *, subject):
    start, end = bounds
    dated = [(r, news_apis._published(r['published_at']) if isinstance(r.get('published_at'), str)
              else r.get('published_at')) for r in articles]
    unresolved = {}
    amounts = defaultdict(list)
    for row, day in dated:
        if not day or not start <= day <= end:
            continue
        content = row.get('title','') + ' ' + (row.get('summary') or '')
        if company_developments.decision_notice(row) and not decision_review.OPERATIVE.search(row.get('summary') or ''):
            unresolved.setdefault(day.date(), []).append(row)
        metric, period = CHANGE.search(content), PERIOD.search(content)
        if metric and period:
            negative = re.search(r'fell|declined|dropped|turun', metric[0], re.I)
            value = float(metric[1].replace(',', '.')) * (-1 if negative else 1)
            amounts[(period[1].upper(), period[2])].append(value)
    for day, notices in unresolved.items():
        stamp = datetime.combine(day, datetime.min.time(), start.tzinfo)
        yield (max(start, stamp-timedelta(days=3)), min(end, stamp+timedelta(days=8))), (
            f"Find the operative ruling and remaining restrictions for {notices[0]['title'][:180]}"), notices
    for (period, year), values in amounts.items():
        if max(values) - min(values) > 1:
            yield bounds, f"Resolve conflicting reported net profit changes for {period} {year} against the issuer filing", []
    recent = max(start, end - timedelta(days=7))
    if (primary or provider_gaps or adverse_focus) and not any(
            day and recent <= day <= end and search_settings.enforcement_candidate(row, subject) for row, day in dated):
        yield (recent, end), ENFORCEMENT_QUERY, []
    cursor = start
    while cursor <= end:
        month = (cursor.month-1)//3*3+4
        boundary = cursor.replace(year=cursor.year+(month>12), month=(month-1)%12+1, day=1,
                                  hour=0, minute=0, second=0, microsecond=0)
        stop = min(end, boundary-timedelta(microseconds=1))
        in_period = [r for r, d in dated if d and cursor <= d <= stop]
        if primary:
            yield (cursor,stop), 'Find dated adverse developments, issuer disclosures, exchange inquiries and financial results', []
        elif provider_gaps or not in_period:
            yield (cursor,stop), 'Fill the discovery gap using dated issuer and exchange disclosures and adverse news', []
        elif stop-cursor >= timedelta(days=75) and not any(FINANCIAL.search(r.get('title','')) for r in in_period):
            yield (cursor,stop), 'Locate financial results published in this period, including comparable profit figures', []
        elif not any(c.get('status') == 'Index checked' for c in coverage) and any(
                c.get('status') in ('Unavailable','Incomplete','No confirmed dated index','Source discovery pending')
                or c.get('detail') for c in coverage):
            yield (cursor,stop), 'Locate filings missing from the accessible issuer archive, especially exchange inquiries and material notices', []
        elif adverse_focus and not any(re.search(r'volatili|unusual trading|exchange inquir|unusual market activity',
                r.get('title', '') + ' ' + (r.get('summary') or ''), re.I) for r in in_period):
            yield (cursor, stop), 'Check issuer and exchange archives for unusual trading inquiries and issuer responses', []
        cursor = boundary


def collect(subject, span, articles, coverage, *, primary, provider_gaps, progress=None, adverse_focus=False):
    if not recovery_providers.configured('Google'):
        return [], ['Google API key unavailable']
    now = datetime.now(timezone.utc)
    found, gaps = {}, []
    for bounds, query, notices in searches(articles, coverage, span or (now-timedelta(days=7), now), primary, provider_gaps, adverse_focus, subject=subject):
        enforcement = query == ENFORCEMENT_QUERY
        if adverse_focus and not enforcement:
            query += ('. Prioritise adverse events and material neutral filings, including falling profit despite revenue growth, '
                      'liquidity pressure, continuing court restrictions, volatility inquiries and audit committee changes. Cite original filings.')
        if progress:
            progress('Searching Google Gemini · ' + query)
        try:
            hits = lookup(subject, bounds, query)
            if not hits and not enforcement:
                gaps.append('Google found no admissible dated citation for ' + query)
            for row in hits:
                found.setdefault(row['url'], row)
                for notice in notices:
                    if row['url'] != canonical_url(notice['url']) and row['published_at'].date() >= news_apis._published(str(notice['published_at'])).date():
                        links = notice.setdefault('decision_followup_urls', [])
                        if row['url'] not in links:
                            links.append(row['url'])
        except (ValueError, OSError, http.client.HTTPException) as exc:
            gaps.append(str(exc) if str(exc).startswith('Google') else 'Google grounded search failed')
            break
    return list(found.values()), gaps
