from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from ra import news_apis


def test_network_failure_diagnostics_never_expose_url_or_key(monkeypatch):
    import socket
    import ssl
    import pytest
    from urllib.error import URLError

    secret = "https://provider.test/?api_key=secret-value"
    for reason, expected in ((TimeoutError(secret), "network request timed out"),
                             (socket.gaierror(secret), "DNS lookup failed"),
                             (ssl.SSLError(secret), "TLS connection failed"),
                             (ConnectionResetError(secret), "network connection failed"),
                             (secret, "network request failed")):
        def reject(request, timeout):
            raise URLError(reason)
        monkeypatch.setattr(news_apis, "urlopen", reject)
        with pytest.raises(news_apis.ProviderError) as error:
            news_apis.fetch_json(secret)
        assert str(error.value) == expected


def test_both_providers_keep_direct_links_language_and_provenance(monkeypatch):
    monkeypatch.setenv("RA_GNEWS_API_KEY", "private-gnews-key")
    monkeypatch.setenv("RA_THENEWSAPI_API_KEY", "private-news-key")
    subject = {"name": "Fertilizantes Heringer"}
    captured = []

    def fetcher(url):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        captured.append((parsed.path, params))
        if "gnews.io" in parsed.netloc:
            return {"articles": [{"title": "Heringer anuncia dívida", "url": "https://publisher.test/one",
                                  "source": {"name": "Jornal"}, "lang": "pt",
                                  "publishedAt": "2026-11-03T08:00:00Z"}]}
        return {"data": [{"title": "Heringer creditor update", "url": "https://other.test/two",
                          "source": "other.test", "language": "en",
                          "published_at": "2026-11-03T07:00:00.000000Z"}]}

    reserve = lambda provider, limit: limit == 95
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    gnews = news_apis.search("gnews", subject, fetcher=fetcher, reserve=reserve, now=now)
    the_news = news_apis.search("thenewsapi", subject, fetcher=fetcher, reserve=reserve, now=now)
    assert gnews[0]["discovery_service"] == "GNews"
    assert gnews[0]["publisher"] == "Jornal"
    assert gnews[0]["language"] == "pt"
    assert the_news[0]["discovery_service"] == "TheNewsAPI"
    assert the_news[0]["url"] == "https://other.test/two"
    assert captured[0][1]["q"][0].startswith('"Fertilizantes Heringer" NOT "sports"')
    assert captured[1][1]["exclude_categories"] == ["sports,entertainment"]
    assert captured[1][1]["search"] == ['"Fertilizantes Heringer"']
    assert captured[0][1]["from"] == ["2026-10-27T12:00:00Z"]
    assert captured[1][1]["published_after"] == ["2026-10-27T12:00:00"]
    assert captured[0][1]["max"] == ["10"]
    assert "limit" not in captured[1][1]  # Use the account's maximum page size.
    assert "private-gnews-key" not in str(gnews)
    from datetime import timedelta
    window = (now-timedelta(weeks=4),now-timedelta(weeks=3))
    for provider in ("gnews","thenewsapi"):
        news_apis.search(provider,subject,fetcher=fetcher,reserve=reserve,now=now,window=window)
    assert captured[2][1]["from"] == ["2026-10-06T12:00:00Z"]
    assert captured[2][1]["to"] == ["2026-10-13T12:00:00Z"]
    assert captured[3][1]["published_after"] == ["2026-10-06T12:00:00"]
    assert captured[3][1]["published_before"] == ["2026-10-13T12:00:00"]


def test_large_watchlist_is_rotated_and_free_budget_is_respected(monkeypatch):
    monkeypatch.setenv("RA_GNEWS_API_KEY", "example")
    monkeypatch.setenv("RA_THENEWSAPI_API_KEY", "example")
    monkeypatch.setattr(news_apis.db, "remaining_news_api_requests", lambda limits: limits)
    subjects = [{"name": f"Company {i}"} for i in range(200)]
    planned = news_apis.plan_subjects(subjects, 7, datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert len(planned) == 200
    assert sum(item["_news_providers"] == ["gnews"] for item in planned) == 95
    assert sum(item["_news_providers"] == ["thenewsapi"] for item in planned) == 95
    assert sum(not item["_news_providers"] for item in planned) == 10
    assert planned[0]["name"] != subjects[0]["name"]


def test_provider_http_error_does_not_expose_key_or_url(monkeypatch):
    def reject(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(news_apis, "urlopen", reject)
    try:
        news_apis.fetch_json("https://gnews.io/api/v4/search?apikey=secret-value")
    except news_apis.ProviderError as exc:
        assert str(exc) == "HTTP 403"
        assert "secret-value" not in str(exc)
    else:
        raise AssertionError("Expected provider error")


def test_exhausted_budget_never_calls_provider(monkeypatch):
    import pytest
    monkeypatch.setenv("RA_GNEWS_API_KEY", "secret")
    def unexpected_fetch(url):
        raise AssertionError("No HTTP request should be made")
    with pytest.raises(news_apis.ProviderError, match="budget exhausted"):
        news_apis.search("gnews", {"name": "Acme"}, fetcher=unexpected_fetch,
                         reserve=lambda provider, limit: False)


def test_error_payload_and_missing_list_are_not_silent_success():
    import pytest
    for payload in ({"error": {"message": "contains-secret"}}, {}, {"data": None}):
        with pytest.raises(news_apis.ProviderError) as error:
            news_apis.parse_articles("thenewsapi", payload, '"Acme"')
        assert "contains-secret" not in str(error.value)


def test_malformed_publication_value_does_not_discard_other_provider_articles():
    from ra.news_apis import parse_articles
    rows = parse_articles('gnews', {'articles': [
        {'title': 'Acme business update', 'url': 'https://example.test/a', 'publishedAt': 12345},
        {'title': 'Acme financial results', 'url': 'https://example.test/b', 'publishedAt': '2026-11-07T10:00:00Z'},
    ]}, 'Acme')
    assert len(rows) == 2
    assert rows[0]['published_at'] is None
    assert rows[1]['published_at'].isoformat() == '2026-11-07T10:00:00+00:00'


def test_acron_alternatives_are_separate_and_short_names_have_local_context():
    subject = dict(name='Public joint-stock company ACRON', short_name='Acron',
                   country_registration='Russian Federation',
                   aliases='Акрон\nAcron\nAcron Holding\nПубличное акционерное общество АКРОН')
    for provider in news_apis.PROVIDERS:
        queries = news_apis.provider_queries(subject, provider)
        assert all(len(query) <= 200 for query in queries)
        joined = ' '.join(queries)
        assert '"Акрон"' in joined and '"Acron"' in joined and '"Acron Holding"' in joined
        assert 'Россия' in joined
        assert '"Russia"' in joined
        assert '"Public joint-stock company ACRON"' in joined
        assert 'Акрон, Acron' not in joined


def test_long_alias_sets_split_without_dropping_names():
    aliases = [f'Distinct company trading name {i}' for i in range(8)]
    subject = dict(name='Full company legal name', aliases='\n'.join(aliases))
    for provider in news_apis.PROVIDERS:
        queries = news_apis.provider_queries(subject, provider)
        assert len(queries) > 1 and all(len(query) <= 200 for query in queries)
        assert all('"' + name + '"' in ' '.join(queries) for name in aliases + [subject['name']])


def api_record(provider, i, title=None):
    return dict(title=title or f'Acme Manufacturing reports financial losses {i}',
                url=f'https://publisher.test/{i}', source={'name': 'Daily'} if provider == 'gnews' else 'Daily',
                **({'publishedAt': '2026-11-03T08:00:00Z'} if provider == 'gnews'
                   else {'published_at': '2026-11-03T08:00:00Z'}))


def api_page(provider, rows, page=1, size=10, total=12):
    return ({'articles': rows, 'totalArticles': total} if provider == 'gnews' else
            {'data': rows, 'meta': {'page': page, 'limit': size, 'found': total, 'returned': len(rows)}})


import pytest


@pytest.mark.parametrize('provider', ['gnews', 'thenewsapi'])
def test_pagination_uses_metadata_preserves_bounds_and_deduplicates(monkeypatch, provider, caplog):
    monkeypatch.setenv(news_apis.PROVIDERS[provider][0], 'secret-key')
    calls, reserved = [], []
    start, end = datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc)
    def fetch(url):
        args = parse_qs(urlparse(url).query); calls.append(args)
        page = int(args['page'][0])
        rows = [api_record(provider, i) for i in (range(10) if page == 1 else [9, 10])]
        return api_page(provider, rows, page=page)
    with caplog.at_level('INFO', logger=news_apis.__name__):
        results = news_apis.search(provider, {'name': 'Acme Manufacturing', '_news_run_id': 45}, fetcher=fetch,
                                  reserve=lambda *args: reserved.append(args) or True, window=(start, end))
    assert len(results) == 11
    assert len(calls) == len(reserved) == 2
    assert [c['page'] for c in calls] == [['1'], ['2']]
    assert all(c.get('from', c.get('published_after'))[0].startswith('2026-11-02') for c in calls)
    assert all(c.get('to', c.get('published_before'))[0].startswith('2026-11-08') for c in calls)
    assert 'secret-key' not in caplog.text
    assert '"returned": 10' in caplog.text and '"run_id": 45' in caplog.text


@pytest.mark.parametrize('provider', ['gnews', 'thenewsapi'])
def test_filtered_first_page_does_not_hide_later_results(monkeypatch, provider):
    monkeypatch.setenv(news_apis.PROVIDERS[provider][0], 'secret-key')
    def fetch(url):
        page = int(parse_qs(urlparse(url).query)['page'][0])
        return api_page(provider, [api_record(provider, i, 'Football match results') for i in range(10)] if page == 1
                        else [api_record(provider, 10)], page=page, total=11)
    results = news_apis.search(provider, {'name': 'Acme Manufacturing'}, fetcher=fetch, reserve=lambda *args: True)
    assert [a['url'] for a in results] == ['https://publisher.test/10']


@pytest.mark.parametrize('failure', ['budget', 'http', 'repeat', 'allocation'])
def test_incomplete_pagination_keeps_already_fetched_articles(monkeypatch, failure):
    monkeypatch.setenv('RA_GNEWS_API_KEY', 'secret-key')
    calls, reservations = [], []
    def fetch(url):
        calls.append(url)
        if len(calls) == 2 and failure == 'http':
            raise news_apis.ProviderError('HTTP 429')
        return api_page('gnews', [api_record('gnews', i) for i in range(10)], total=20)
    def reserve(*args):
        reservations.append(args)
        return failure != 'budget' or len(reservations) == 1
    subject = {'name': 'Acme Manufacturing'}
    if failure == 'allocation':
        subject['_news_request_limits'] = {'gnews': 1}
    with pytest.raises(news_apis.ProviderError) as error:
        news_apis.search('gnews', subject, fetcher=fetch, reserve=reserve)
    assert len(error.value.articles) == 10
    assert 'secret-key' not in str(error.value)
    assert len(calls) == (1 if failure in ('budget', 'allocation') else 2)


def test_watchlist_pagination_shares_remaining_budget(monkeypatch):
    for key, _, _ in news_apis.PROVIDERS.values():
        monkeypatch.setenv(key, 'test-key')
    monkeypatch.setattr(news_apis.db, 'remaining_news_api_requests', lambda _: {'gnews': 22, 'thenewsapi': 33})
    planned = news_apis.plan_subjects([{'name': f'Company {i}'} for i in range(11)], 45)
    assert all(row['_news_request_limits'] == {'gnews': 2, 'thenewsapi': 3} for row in planned)
    assert all(row['_news_run_id'] == 45 for row in planned)


def test_company_search_preserves_partial_provider_results_and_shows_error(monkeypatch):
    from ra import disclosures, search
    monkeypatch.setattr(disclosures, 'collect', lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(search, 'company_search_plan', lambda _: [])
    monkeypatch.setattr(news_apis, 'configured_providers', lambda: ['gnews'])
    retained = news_apis.parse_articles('gnews', {'articles': [api_record('gnews', 1)]}, 'Acme')
    def incomplete(*args, **kwargs):
        raise news_apis.ProviderError('HTTP 429', retained)
    monkeypatch.setattr(news_apis, 'search', incomplete)
    articles, errors = search.search_company({'name': 'Acme Manufacturing'})
    assert articles == retained
    assert errors == ['gnews: HTTP 429']


def test_usable_alias_survives_an_overlong_legal_name():
    subject = {'name': 'Long legal company name ' * 12, 'aliases': 'Acme Manufacturing'}
    assert news_apis.provider_queries(subject, 'gnews') == ['"Acme Manufacturing"']


def test_first_page_of_every_name_group_precedes_deeper_pages(monkeypatch):
    monkeypatch.setenv('RA_GNEWS_API_KEY', 'test')
    subject = {'name': 'Full legal company name', 'aliases': '\n'.join(f'Distinct company trading name {i}' for i in range(8))}
    calls = []
    def fetch(url):
        args = parse_qs(urlparse(url).query); calls.append(args)
        page = int(args['page'][0])
        return api_page('gnews', [api_record('gnews', f'{len(calls)}-{i}') for i in range(10 if page==1 else 1)], total=11)
    news_apis.search('gnews', subject, fetcher=fetch, reserve=lambda *args: True)
    n = len(news_apis.provider_queries(subject, 'gnews'))
    assert [row['page'][0] for row in calls] == ['1'] * n + ['2'] * n
