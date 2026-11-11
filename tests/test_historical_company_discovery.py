from datetime import datetime, timedelta, timezone

import pytest

from ra import disclosures, governance_sources, news_apis, search, search_settings

LAUTAN = dict(name='PT Lautan Luas Tbk', short_name='Lautan Luas', aliases='LTLS',
              country_registration='Indonesia')
START = datetime(2026, 5, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 10, tzinfo=timezone.utc)


def test_only_company_linked_adverse_news_passes(monkeypatch):
    from ra import keywords
    monkeypatch.setattr(keywords, '_rules', keywords.compile_terms([
        dict(english='closure', direction='adverse', enabled=True, translations={'id': 'penutupan'})]))
    row = dict(title='IHSG Menguat 0,32% pada Penutupan 4 November 2026 - Peradaban.id',
               title_en='IDX Composite strengthened 0.32% at the close', publisher='Peradaban.id', language='id')
    assert not search_settings.adverse_lead(row, LAUTAN)
    assert not search_settings.adverse_lead(dict(row, title='Lautan Luas pada penutupan perdagangan'), LAUTAN)
    assert not search_settings.adverse_lead(dict(row, summary='Lautan Luas publishes earnings. Other Corp files for bankruptcy.'), LAUTAN)
    assert search_settings.adverse_lead(dict(row, summary='Lautan Luas files for bankruptcy.'), LAUTAN)
    assert search_settings.adverse_lead(dict(row, title='Lautan Luas: penutupan pabrik'), LAUTAN)
    assert search_settings.adverse_lead(dict(row, title='Laba Bersih LTLS Anjlok 23 Persen'), LAUTAN)
    assert not search_settings.adverse_lead(dict(row, title='Laba Bersih LTLS Meningkat 23 Persen'), LAUTAN)
    assert not search_settings.adverse_lead(dict(row, query='"Lautan Luas" bankruptcy'), LAUTAN)


def test_long_name_feed_reads_older_slices_filters_dates_before_cap(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from ra import idx_filings
    monkeypatch.setattr(idx_filings, 'collect', lambda *a, **kw: ([], []))
    monkeypatch.setattr(disclosures, 'collect', lambda *a, **kw: ([], []))
    monkeypatch.setattr(news_apis, 'configured_providers', lambda: [])
    monkeypatch.setattr(governance_sources, 'notice_source', lambda s: None)
    monkeypatch.setattr(search, 'company_search_plan', lambda s: [('"Lautan Luas"', 'Indonesia'), ('targeted risk', '')])
    calls = []
    def fetch(url):
        q = parse_qs(urlsplit(url).query)['q'][0]
        calls.append(q)
        old = '<item><title>Old</title><link>https://example.test/old</link><pubDate>Fri, 01 May 2020 12:00:00 GMT</pubDate></item>'
        hit = '<item><title>Lautan Luas</title><link>https://example.test/may</link><pubDate>Fri, 15 May 2026 12:00:00 GMT</pubDate></item>'
        return ('<rss><channel>' + old * 101 + hit + '</channel></rss>').encode()
    rows, errors = search.search_company(LAUTAN, fetcher=fetch, window=(START, END))
    assert not errors and [r['url'] for r in rows] == ['https://example.test/may']
    assert len([q for q in calls if q.startswith('"Lautan Luas"')]) == 7
    assert len([q for q in calls if q.startswith('targeted risk')]) == 1


def test_empty_positive_provider_count_is_explicit_partial_coverage(monkeypatch):
    monkeypatch.setenv('RA_GNEWS_API_KEY', 'test-key')
    pages = iter([[], [dict(title='Lautan Luas files for bankruptcy', url='https://example.test/retained')]])
    with pytest.raises(news_apis.ProviderError, match='reported 12 matches but returned no articles') as error:
        news_apis.search('gnews', LAUTAN, fetcher=lambda _: {'articles': next(pages), 'totalArticles': 12}, reserve=lambda *a: True)
    assert [r['url'] for r in error.value.articles] == ['https://example.test/retained']


def test_ksei_archive_matches_full_issuer_and_respects_intraday_cutoff():
    source = governance_sources.notice_source(LAUTAN)
    assert source[1] == governance_sources.KSEI_MINUTES
    def fetch(url):
        assert 'Month=05&Year=2026' in url
        def row(name, num):
            path = '/publications/corporate-action-schedules/minutes-of-meeting'
            return f'<tr><td><a href="{path}?Month=05&amp;Year=2026&amp;file={num}">PDF</a></td><td>Risalah RUPST {name}</td><td>07 Mei 2026</td></tr>'
        return ('<th>Download</th><th>Perihal</th><th>Tanggal</th>' + row('LAUTAN LUAS Tbk, PT', 420)
                + row('BUMI LAUTAN LUAS Tbk, PT', 422) + row('LAUTAN LUAS CHEMICALS Tbk, PT', 421)).encode()
    end = START + timedelta(days=9)
    rows = governance_sources.recent_notices(LAUTAN, *source, fetch, window=(START, end))
    assert len(rows) == 1 and rows[0]['url'].endswith('?file=420')
    assert rows[0]['published_at'].day == 7
    assert not governance_sources.recent_notices(LAUTAN, *source, fetch, window=(START, START + timedelta(days=6)))
    assert governance_sources.notice_source(dict(LAUTAN, country_registration='Brazil')) is None


def test_legacy_disclosure_note_is_not_a_failed_request():
    subject = dict(LAUTAN, id=1)
    state = dict(identity=disclosures.identity(subject), gaps=['HTTP 403', disclosures.COVERAGE_NOTE], sources=[])
    assert disclosures.collect(dict(subject, disclosure_sources=state)) == ([], ['HTTP 403'])


from test_score_store import database


def test_company_only_run_saves_summary_evidence_and_filter_counts(database, monkeypatch):
    from ra import db, worker
    search_settings.save(dict(include_country=False, include_industry=False, adverse_only=True))
    run = db.create_run('one_off', [LAUTAN])
    date = db.get_run(run)['created_at'] - timedelta(hours=1)
    article = dict(title='Quarterly update', language='en', publisher='Trade paper', query='Lautan Luas',
                   signal_terms=[], url='https://example.test/lautan', published_at=date)
    good = dict(article, summary='Lautan Luas faces a lawsuit.')
    bad = dict(article, title='Other issuer faces a lawsuit', url='https://example.test/other')
    monkeypatch.setattr(worker, 'translate_text', lambda title, *args: (title, 'English'))
    monkeypatch.setattr(worker, 'detect_language', lambda *args: 'en')
    worker.process_run(run, searcher=lambda _: ([good, bad], []), topic_searcher=lambda *a, **kw: ([], ''),
                       roster_check=lambda *args: None)
    assert db.get_run(run)['candidates_in_window'] == 2
    assert db.get_run(run)['result_count'] == 1
    rows = db.get_results(run)
    assert len(rows) == 1 and rows[0]['discovery_summary'] == good['summary']


def test_ksei_month_failure_preserves_other_months():
    from urllib.error import HTTPError
    from ra.governance_sources import KSEI_MINUTES, recent_notices

    def fetch(url):
        if 'Month=06' in url:
            raise HTTPError(url, 500, 'Internal Server Error', {}, None)
        path = '/publications/corporate-action-schedules/minutes-of-meeting'
        return (f'<th>Download</th><th>Perihal</th><th>Tanggal</th><tr><td><a href="{path}?file=420">'
                'Download</a></td><td>Risalah RUPST LAUTAN LUAS Tbk, PT</td><td>07 Mei 2026</td></tr>').encode()

    errors = []
    rows = recent_notices(LAUTAN, 'KSEI shareholder meeting minutes', KSEI_MINUTES, fetch,
                          window=(START, START + timedelta(days=33)), errors=errors)
    assert len(rows) == 1 and rows[0]['url'].endswith('?file=420')
    assert len(errors) == 1 and '2026-06' in errors[0] and '500' in errors[0]


def test_gnews_reports_archive_limit_and_keeps_available_newer_articles(monkeypatch):
    monkeypatch.setenv('RA_GNEWS_API_KEY', 'test-key')
    calls = []
    def fetch(url):
        calls.append(url)
        rows = [dict(title='Issuer notice',url='https://example.test/notice')] if len(calls)==1 else []
        return {'articles':rows,'totalArticles':30,'articlesRemovedFromResponse':{
            'historicalArticles':{'message':'Older matches left out'}}}
    with pytest.raises(news_apis.ProviderError, match="30-day archive") as error:
        news_apis.search('gnews', LAUTAN, fetcher=fetch, reserve=lambda *args: True)
    assert len(calls) == 2 and len(error.value.articles) == 1
    assert str(error.value).count('30-day archive') == 1


def test_index_gap_recovers_only_dated_issuer_disclosure(monkeypatch):
    from ra import idx_filings, recovery_providers
    s = dict(LAUTAN, disclosure_sources={"identity": disclosures.identity(LAUTAN), "sources": [
        {"url":"https://www.example.test", "kind":"candidate", "evidence":"https://www.wikidata.org/wiki/Q1"}]})
    hit = dict(title="Information regarding company assets - Lautan Luas", url="https://www.example.test/march-notice",
               description="The Company’s assets had been subject to seizure.",page_age="2026-03-12T02:27:17")
    def request(provider, path):
        assert provider == "Brave" and "2026-01-01to2026-03-26" in path
        return {"web":{"results":[hit,dict(hit,url="https://other.test"), dict(hit,title="Other issuer"),
                                  dict(hit,page_age="2026-04-20"), dict(hit,page_age=None)]}}
    monkeypatch.setattr(recovery_providers,"configured",lambda provider: True)
    monkeypatch.setattr(recovery_providers,"request",request)
    page = ('<h1>LTLS · LAUTAN LUAS Tbk, PT</h1><section id="pengumuman"><li><span>27 Mar 2026</span>'
            '<a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/202603/m.pdf">'
            'RUPS</a></li></section>')
    rows,gaps=idx_filings.collect(s,lambda _:page.encode(),(START.replace(month=1),END))
    lead, = [r for r in rows if r["discovery_service"]==idx_filings.ARCHIVE_SERVICE]
    assert lead["published_at"].astimezone(timezone.utc).day == 12 and lead["source_kind"] == "news"
    assert search_settings.adverse_lead(lead,s)
    assert not search_settings.adverse_lead(dict(lead,summary="Company assets increased."),s)
    assert len(gaps)==1 and "2026-03-27" in gaps[0]
