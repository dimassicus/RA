import pytest

from ra import article_content as reader, recovery_providers as providers
from ra.verification import verify

URL = 'https://gazeta.example/news/123'
HINT = {'title': 'Crédito aprovado - Gazeta', 'publisher': 'Gazeta', 'published_at': '2026-11-05T10:00:00+00:00'}
TEXT = 'Acme Manufacturing has reported a financial loss after a decline in demand. ' * 6
META = '<meta property="og:title" content="Crédito aprovado"><meta name="datePublished" content="2026-11-05T09:00:00Z">'


@pytest.fixture
def setup(monkeypatch):
    for name in providers.HOSTS:
        monkeypatch.setenv('RA_' + name.upper() + '_API_KEY', 'fake')
    monkeypatch.setattr(providers, 'public_target', lambda url: None)
    return monkeypatch


def test_brave_after_failed_rss_checks_publisher_page(setup):
    def fetch(url):
        if 'bing.com' in url:
            raise TimeoutError()
        raw = (META + '<article>' + TEXT + '</article>').encode() if url == URL else b''
        return raw, 'text/html', url
    setup.setattr(reader, 'fetch_bytes', fetch)
    hits = [{'title': 'Crédito aprovado | Gazeta', 'profile': {'name':'Gazeta'}, 'url':URL},
            {'title': 'Crédito aprovado', 'profile': {'name':'Syndicate'}, 'url':'https://other.example/123'}]
    setup.setattr(providers, 'request', lambda provider, path: {'web': {'results': hits}})
    result = reader._retrieve('https://news.google.com/rss/articles/missing', HINT)
    assert result['status'] == 'available' and result['recovery'].startswith('Brave')
    assert result['body'] == TEXT.strip() and not result['review_only']
    hits.append(dict(hits[0], url=URL+'-ambiguous'))
    assert not providers.brave_destination(HINT)


def test_firecrawl_after_jina_failure_cannot_supply_automatic_score(setup):
    calls = []
    def request(provider, path, **kwargs):
        calls.append(provider)
        if provider == 'Jina':
            raise ValueError('Jina HTTP 409')
        return {'data': {'metadata': {'title':'Crédito aprovado', 'url':URL, 'statusCode':200}, 'markdown':TEXT}}
    setup.setattr(providers, 'request', request)
    setup.setattr(reader, 'fetch_bytes', lambda url: (META.encode(), 'text/html', url))
    result = reader._retrieve(URL, HINT)
    assert calls == ['Jina','Firecrawl'] and result['body'] == TEXT.strip()
    assert verify(result, {'name':'Acme Manufacturing'}, {'url':URL})['status'] == 'needs_review'


@pytest.mark.parametrize('change', [
    {'url':'https://other.example/article'}, {'title':'Other story'}, {'publishedTime':'2024-11-05T10:00:00Z'},
    {'content':'Only a headline'}, {'content':TEXT+' Subscribe to continue reading'}
])
def test_reader_rejects_unconfirmed_or_gated_content(setup, change):
    setup.delenv('RA_FIRECRAWL_API_KEY')
    data = dict(url=URL,title='Crédito aprovado',publishedTime=HINT['published_at'],content=TEXT)
    setup.setattr(providers, 'request', lambda *args, **kwargs: {'data': {**data, **change}})
    setup.setattr(reader, 'fetch_bytes', lambda url: (META.encode(), 'text/html', url))
    assert reader._retrieve(URL, HINT)['status'] == 'unavailable'


def test_publisher_access_denial_stops_before_paid_readers(setup):
    def denied(url):
        raise ValueError('Source returned HTTP 403; access was not bypassed')
    setup.setattr(reader, 'fetch_bytes', denied)
    setup.setattr(providers, 'request', lambda *a, **kw: pytest.fail('unexpected API call'))
    assert 'HTTP 403' in reader._retrieve(URL, HINT)['reason']


def test_quota_exhaustion_does_not_make_external_request(setup):
    setup.setattr(providers.db, 'reserve_news_api_request', lambda name, limit: False)
    with pytest.raises(ValueError, match='daily request limit'):
        providers.request('Brave', '/res/v1/web/search')


def test_launcher_accepts_existing_recovery_labels(tmp_path, setup):
    from scripts import serve_local
    env = tmp_path / '.env'
    env.write_text('BRAVE API=b\nJINA API=j\nFirecrawler API=f\nFMP API=unused\n')
    setup.setattr(serve_local, 'ENV_FILE', env)
    result = {}
    serve_local.load_local_env(result)
    assert result == {'RA_BRAVE_API_KEY':'b', 'RA_JINA_API_KEY':'j', 'RA_FIRECRAWL_API_KEY':'f'}


def test_uncapped_jina_and_monthly_firecrawl(setup):
    from unittest.mock import Mock
    import datetime

    day = datetime.date(2026, 11, 6)
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = b'{}'
    setup.setattr(providers.http.client, 'HTTPSConnection', lambda *args, **kwargs: connection)
    daily = Mock(return_value=True)
    setup.setattr(providers.db, 'reserve_news_api_request', daily)
    setup.setenv('RA_JINA_DAILY_LIMIT', '20')
    for _ in range(21):
        providers.request('Jina', '/article')
    daily.assert_called_with('jina', None)
    setup.setenv('RA_FIRECRAWL_DAILY_LIMIT', '0')
    setup.setattr(providers.db, 'reserve_firecrawl_credit', Mock(side_effect=[day, None, day, day]))
    release = Mock()
    setup.setattr(providers.db, 'release_firecrawl_credit', release)
    providers.request('Firecrawl', '/v2/scrape', body={'url': URL})
    with pytest.raises(ValueError, match='monthly credit limit'):
        providers.request('Firecrawl', '/v2/scrape', body={'url': URL})
    assert connection.request.call_count == 22
    release.assert_not_called()
    response.status = 429
    with pytest.raises(ValueError, match='HTTP 429'):
        providers.request('Firecrawl', '/v2/scrape', body={'url': URL})
    release.assert_called_once_with(day)
    response.status = 200
    response.read.return_value = b'{"data":{"metadata":{"creditsUsed":0}}}'
    providers.request('Firecrawl', '/v2/scrape', body={'url': URL})
    assert release.call_count == 2


@pytest.mark.parametrize('paid', [False, True])
def test_one_hop_reads_linked_filing_and_ignores_related_navigation(setup, paid):
    linked = URL + '/document'
    parent = (META + '<div class="elementor-widget-theme-post-content"><a href="' + linked +
              '">Full report</a></div><footer><article><a href="/other.pdf">Other</a></article></footer>')
    calls = []
    def fetch(url):
        calls.append(url)
        if url == URL:
            return (META if paid else parent).encode(), 'text/html', url
        assert url == linked
        return ('<article>' + TEXT + '<a href="/grandchild.pdf">Filing</a></article>').encode(), 'text/html', url
    setup.setattr(reader, 'fetch_bytes', fetch)
    setup.setattr(providers, 'request', lambda *args, **kw: {'data': {
        'title': 'Crédito aprovado', 'url': URL, 'publishedTime': HINT['published_at'],
        'content': f'[Full report]({linked})\n## Related news\n[Download](https://example.com/other.pdf)'}})
    doc = reader._retrieve(URL, HINT)
    assert calls == [URL, linked]
    assert doc['status'] == 'available' and doc['final_url'] == linked
    assert 'decline in demand' in doc['body'] and not doc['review_only']
