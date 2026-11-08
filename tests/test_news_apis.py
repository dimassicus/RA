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
    assert captured[1][1]["limit"] == ["3"]
    assert "private-gnews-key" not in str(gnews)


def test_large_watchlist_is_rotated_and_free_budget_is_respected(monkeypatch):
    monkeypatch.setenv("RA_GNEWS_API_KEY", "example")
    monkeypatch.setenv("RA_THENEWSAPI_API_KEY", "example")
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
