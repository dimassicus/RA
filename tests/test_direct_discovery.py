"""Regressions for primary notices missed by the Google News index."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ra import governance_sources as sources, search


TIRYAKI = dict(name='TIRYAKI AGRO GIDA SANAYI VE TICARET ANONIM SIRKETI',
              country_registration='Turkey', tax_id='TR8450302285')
CMS = dict(name='CMS JANT VE MAKINA SANAYII ANONIM SIRKETI',
           country_registration='Turkey', tax_id='2060074145')


def fixture(name):
    return json.loads((Path(__file__).parent / 'fixtures' / name).read_text())


def window(month, day):
    return (datetime(2026, month, day, tzinfo=timezone.utc),
            datetime(2026, month, day + 6, 23, 59, 59, tzinfo=timezone.utc))


@pytest.mark.parametrize('subject,file,bounds,expected', [
    (TIRYAKI, 'tiryaki-kap-disclosures.json', window(11, 2), '/1692307'),
    (CMS, 'cms-university-posts.json', window(10, 12), '/cms-jant-ile-ime-protokolu-imzalandi/'),
])
def test_company_discovery_finds_primary_notices_without_google_results(monkeypatch, subject, file, bounds, expected):
    calls = []
    def fetch(url):
        calls.append(url)
        if urlparse(url).hostname == 'news.google.com':
            return b'<rss><channel/></rss>'
        return json.dumps(fixture(file)).encode()
    monkeypatch.setattr(search.news_apis, 'configured_providers', lambda: [])
    monkeypatch.setattr(search, 'company_search_plan', lambda _: [('company', 'Turkey')])
    articles, errors = search.search_company(subject, fetcher=fetch, window=bounds)
    assert not errors
    assert any(a['url'].endswith(expected) for a in articles)
    assert all(a['source_kind'] == 'official' and a['language'] == 'tr' for a in articles)
    assert all(bounds[0] <= a['published_at'] <= bounds[1] for a in articles)
    assert len(calls) == 2
    assert not sources.notice_source(dict(subject, tax_id='wrong'))
    assert not sources.notice_source(dict(subject, country_registration='Portugal'))
    assert not sources.notice_source(dict(subject, name='Different company'))


def test_kap_keeps_exact_related_stock_and_filters_dates_and_identity():
    rows = fixture('tiryaki-kap-disclosures.json')
    wrong = deepcopy(rows[1])
    wrong['disclosureBasic'].update(mkkMemberOid='other', relatedStocks='NOTTRYKI, TRYKI2')
    rows.append(wrong)
    publisher, url = sources.notice_source(TIRYAKI)
    items = sources.recent_notices(TIRYAKI, publisher, url, lambda _: json.dumps(rows).encode(), window=window(11, 2))
    assert len(items) == 2
    assert all(a['published_at'].utcoffset().total_seconds() == 10800 for a in items)
    assert not sources.recent_notices(TIRYAKI, publisher, url, lambda _: json.dumps(rows).encode(), window=window(10, 12))
    next(r['disclosureBasic'] for r in rows if r['disclosureBasic']['disclosureIndex'] == 1692307)['companyTitle'] = 'Different issuer'
    with pytest.raises(ValueError, match='identity changed'):
        sources.recent_notices(TIRYAKI, publisher, url, lambda _: json.dumps(rows).encode(), window=window(11, 2))


def test_university_paginates_and_preserves_success_when_later_page_fails():
    rows = fixture('cms-university-posts.json')
    unrelated = deepcopy(rows[0])
    unrelated['title']['rendered'] = 'New content management system'
    unrelated['excerpt']['rendered'] = '<p>CMS software update</p>'
    calls, errors = [], []
    def fetch(url):
        query = parse_qs(urlparse(url).query)
        calls.append(query)
        if query['page'] == ['2']:
            raise TimeoutError('later page failed')
        return json.dumps(rows + [unrelated] * 99).encode()
    publisher, url = sources.notice_source(CMS)
    items = sources.recent_notices(CMS, publisher, url, fetch, errors=errors, window=window(10, 12))
    assert len(items) == 1 and len(calls) == 2
    assert items[0]['published_at'].isoformat() == '2026-10-13T18:28:24+00:00'
    assert calls[0]['search'] == ['CMS'] and 'after' in calls[0] and 'before' in calls[0]
    assert errors == [publisher + ': later page failed']


@pytest.mark.parametrize('raw', [b'{}', b'<html>maintenance</html>', b'x' * 2_000_001, b'[{}]'])
def test_malformed_primary_index_reports_discovery_error(raw):
    errors = []
    publisher, url = sources.notice_source(CMS)
    assert not sources.recent_notices(CMS, publisher, url, lambda _: raw, errors=errors, window=window(10, 12))
    assert len(errors) == 1 and errors[0].startswith(publisher + ':')


def test_university_rejects_offsite_links_and_out_of_window_results():
    rows = fixture('cms-university-posts.json')
    publisher, url = sources.notice_source(CMS)
    assert not sources.recent_notices(CMS, publisher, url, lambda _: json.dumps(rows).encode(), window=window(11, 2))
    rows[0]['link'] = 'https://example.org/copied-story'
    with pytest.raises(ValueError, match='left its source'):
        sources.recent_notices(CMS, publisher, url, lambda _: json.dumps(rows).encode(), window=window(10, 12))
