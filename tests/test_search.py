from datetime import timezone
from io import BytesIO
from urllib.error import URLError
import ssl
import pytest

from ra.search import (
    FCA_WARNINGS_RSS_URL, fca_warning_list, has_uk_presence, heringer_official_notices,
    is_heringer, parse_feed, search_company, search_queries, signal_terms,
)


def test_feed_retries_handshake_timeouts_and_stops_after_three_attempts(monkeypatch):
    from ra import search
    calls, sleeps = [], []
    def fetch(*args, **kwargs):
        calls.append(kwargs['timeout'])
        if len(calls) < 3:
            raise URLError(TimeoutError('The handshake operation timed out'))
        return BytesIO(b'<rss/>')
    monkeypatch.setattr(search, 'urlopen', fetch)
    monkeypatch.setattr(search.time, 'sleep', sleeps.append)
    assert search.fetch_feed('https://news.google.com/rss/search?q=test') == b'<rss/>'
    assert calls == [20, 20, 20] and sleeps == [1, 2]
    def failed(*args, **kwargs):
        raise TimeoutError('read timed out')
    monkeypatch.setattr(search, 'urlopen', failed)
    sleeps.clear()
    with pytest.raises(TimeoutError):
        search.fetch_feed('https://news.google.com/rss/search?q=test')
    assert sleeps == [1, 2]


def test_feed_certificate_errors_are_reported_without_retries(monkeypatch):
    from ra import search
    def failed(*args, **kwargs):
        raise URLError(ssl.SSLCertVerificationError('certificate verify failed'))
    monkeypatch.setattr(search, 'urlopen', failed)
    monkeypatch.setattr(search.time, 'sleep', lambda *args: pytest.fail('Unexpected retry'))
    with pytest.raises(URLError):
        search.fetch_feed('https://news.google.com/rss/search?q=test')


def test_heringer_adapter_requires_brazilian_identity():
    same_name_elsewhere = {"name": "Fertilizantes Heringer", "country_registration": "Portugal", "registration_number": ""}
    pilot = {"name": "Fertilizantes Heringer", "country_registration": "Brazil", "registration_number": "", "tax_id": "22.266.175/0001-88"}
    assert not is_heringer(same_name_elsewhere)
    assert is_heringer(pilot)


def test_queries_use_owner_context_without_losing_exact_name():
    queries = search_queries({
        "name": "Fertilizantes Heringer", "key_owners": "Eurochem", "affiliates": "Other Co",
    })
    assert queries[0] == '"Fertilizantes Heringer"'
    assert '"Fertilizantes Heringer"' in queries[1]
    assert len(queries) <= 3


def test_google_news_queries_are_limited_to_seven_days():
    from urllib.parse import parse_qs, urlparse
    from ra.search import feed_url
    url, language = feed_url('"Acme Manufacturing"', "Brazil")
    query = parse_qs(urlparse(url).query)["q"][0]
    assert query.startswith('"Acme Manufacturing" ') and query.endswith(" when:7d")
    assert '-"futebol"' in query and '-"cinema"' in query
    assert language == "pt-BR"


def test_historical_queries_reach_company_and_exposure_feeds(monkeypatch):
    from datetime import datetime, timezone
    from urllib.parse import parse_qs, urlparse
    from ra import search, exposure
    window = (datetime(2026,10,12,tzinfo=timezone.utc), datetime(2026,10,18,23,59,59,tzinfo=timezone.utc))
    urls = []
    def fetch(url):
        urls.append(url)
        return b'<rss><channel></channel></rss>'
    monkeypatch.setattr(search.news_apis,"configured_providers",lambda: [])
    search.search_company({"name":"Acme Manufacturing","country_registration":"Brazil"},fetcher=fetch,window=window)
    exposure.search_topic("sector","fertilizer",fetcher=fetch,window=window)
    assert len(urls) >= 2
    for url in urls:
        query = parse_qs(urlparse(url).query)["q"][0]
        assert "after:2026-10-11 before:2026-10-19" in query and "when:7d" not in query


def test_rss_parsing_keeps_publisher_date_and_rejects_insecure_links():
    feed = b'''<rss><channel>
      <item><title>Company announces creditor mediation</title><link>https://example.com/a</link><source>Publisher</source><pubDate>Tue, 13 Oct 2026 07:00:00 GMT</pubDate></item>
      <item><title>Bad link</title><link>http://example.com/b</link></item>
    </channel></rss>'''
    results = parse_feed(feed, '"Company"', "en-US")
    assert len(results) == 1
    assert results[0]["publisher"] == "Publisher"
    assert results[0]["published_at"].tzinfo == timezone.utc
    assert results[0]["source_kind"] == "news"
    assert "debt / creditors" in results[0]["signal_terms"]


def test_official_notice_keeps_document_link_and_date():
    calls = []

    def fetcher(url, payload):
        calls.append(payload["year"])
        return {"success": True, "data": {"document_metas": [
            {"file_title": "Fato Relevante - Procedimento de Mediação",
             "permalink": "https://example.com/notice.pdf", "file_published_date": "2026-08-27T00:00:00.000Z"}
        ] if payload["year"] == 2026 else []}}

    results = heringer_official_notices(fetcher=fetcher, year=2026)
    assert calls == [2026, 2025]
    assert len(results) == 1
    assert results[0]["source_kind"] == "official"
    assert results[0]["published_at"].year == 2026
    assert "restructuring" in results[0]["signal_terms"]


def test_keyword_cues_are_explicitly_just_title_cues():
    assert "management change" in signal_terms("Fato Relevante - Renúncia do Diretor")


def test_fca_rss_is_used_for_uk_presence_and_only_keeps_name_matches_on_fca_domain():
    subject = {"name": "Acme Finance Limited", "short_name": "Acme Finance",
               "country_registration": "France", "primary_countries": "United Kingdom; France"}
    feed = b'''<rss><channel>
      <item><title>Acme Finance (clone of FCA authorised firm)</title><link>https://www.fca.org.uk/news/warnings/acme-finance</link></item>
      <item><title>Acme Finance elsewhere</title><link>https://example.com/imitation</link></item>
      <item><title>Unrelated Finance</title><link>https://www.fca.org.uk/news/warnings/unrelated</link></item>
    </channel></rss>'''
    calls = []

    def fetcher(url):
        calls.append(url)
        return feed if url == FCA_WARNINGS_RSS_URL else b"<rss><channel></channel></rss>"

    assert has_uk_presence(subject)
    articles, errors = search_company(subject, fetcher=fetcher)
    assert errors == []
    assert calls.count(FCA_WARNINGS_RSS_URL) == 1
    assert len(articles) == 1
    assert articles[0]["source_kind"] == "official"
    assert articles[0]["publisher"] == "Financial Conduct Authority"
    assert not has_uk_presence({"name": "Acme Finance", "country_registration": "France"})


def test_shared_exposures_fetch_each_topic_once_and_keep_paths_per_company():
    from ra.exposure import add_exposure_leads, check_topic, search_topic
    feed = b'''<rss><channel>
      <item><title>Brazil fertilizer tariffs rise - Daily</title><link>https://example.test/shared</link><source>Daily</source></item>
      <item><title>Tariffs rise - Brazil</title><link>https://example.test/no-topic</link><source>Brazil</source></item>
      <item><title>Brazil wins match</title><link>https://example.test/sport</link></item>
    </channel></rss>'''
    calls = []
    def search(branch, topic, *, edition):
        calls.append((branch, topic))
        if topic == "copper":
            raise OSError("feed unavailable")
        return search_topic(branch, topic, fetcher=lambda url: feed)
    def check(branch, topic):
        return check_topic(branch, topic, search)
    cache = {}
    first = {"country_registration": "Brazil", "primary_countries": "Brasil", "primary_industries": "Fertilizers; copper"}
    second = {"country_registration": "Brasil", "primary_industries": "fertilizer; Copper"}
    errors_a, errors_b = [], []
    direct = {"url": "https://example.test/shared", "source_kind": "official"}
    a, = add_exposure_leads(first, [direct], errors_a, cache, check)
    b, = add_exposure_leads(second, [], errors_b, cache, check)
    assert calls == [("country", "brazil"), ("sector", "fertilizer"), ("sector", "copper")]
    assert a["source_kind"] == "official" and b["source_kind"] == "context"
    assert len(a["exposure_paths"]) == len(b["exposure_paths"]) == 2
    assert "operates in" in a["exposure_paths"][0] and "operates in" not in b["exposure_paths"][0]
    assert "import costs" in b["exposure_paths"][1]
    assert errors_a == errors_b and "copper" in errors_a[0]
    prices = feed.replace(b"fertilizer tariffs", b"fertilizer prices")
    assert not search_topic("country", "brazil", fetcher=lambda url: prices)
    assert search_topic("sector", "fertilizer", fetcher=lambda url: prices)


def test_sector_editions_span_linked_companies_and_retain_partial_results():
    from urllib.parse import parse_qs, urlsplit
    from ra.exposure import check_topic, search_topic, topic_editions
    subjects = [
        {"country_registration": "Brasil", "primary_countries": "Latin America", "primary_industries": "fertilizers"},
        {"country_registration": "Vietnam", "primary_industries": "fertilizer"},
    ]
    plan = topic_editions(subjects)
    assert plan == topic_editions(reversed(subjects))
    editions = plan["sector", "fertilizer"]
    assert editions == {"brazil", "mexico", "vietnam"}
    assert plan["country", "brazil"] == {"brazil"}
    calls = []
    def fetch(url):
        params = parse_qs(urlsplit(url).query)
        language = params["hl"][0]
        calls.append(language)
        if language == "es-419":
            raise OSError("unavailable")
        if language == "en-US":
            return b"<rss><channel/></rss>"
        if language == "vi-VN":
            assert "phân bón" in params["q"][0]
            titles = ["Giá phân bón tăng", "Phân bón giả", "Giá tăng - phân bón"]
        else:
            assert "fertilizantes" in params["q"][0]
            titles = ["Preços de fertilizantes sobem"]
        return ("<rss><channel>" + "".join(
            f"<item><title>{title}</title><source>phân bón</source><link>https://example.test/{language}/{i}</link></item>"
            for i, title in enumerate(titles)) + "</channel></rss>").encode()
    hits, error = check_topic("sector", "fertilizer", lambda b, t, edition: search_topic(b, t, fetch, edition=edition), editions)
    assert calls == ["en-US", "pt-BR", "es-419", "vi-VN"]
    assert "mexico" in error and "unavailable" in error
    assert [item["language"] for item, _ in hits] == ["pt-BR", "vi-VN"]
    assert all(label == "selling prices and margins" for _, label in hits)


def test_sector_market_applies_to_each_company_in_shared_cache():
    from ra.exposure import add_exposure_leads, scope_sector_paths
    cache, calls = {}, []
    def check(branch, topic):
        calls.append((branch, topic))
        titles = ["India fertilizer prices rise", "Bangladesh fertilizer shortage",
                  "Brazilian fertilizer prices rise", "India exports fertilizer to Brasil",
                  "Fertilizer prices rise - Brazil", "World fertilizer supply tightens"]
        return [({"title": title, "publisher": "Brazil", "url": str(i)}, "input costs")
                for i, title in enumerate(titles) if branch == "sector"], ""
    def leads(country, presence):
        return add_exposure_leads({"country_registration": country, "primary_countries": presence,
                                   "primary_industries": "fertilizer"}, [], [], cache, check)
    brazil = leads("Brazil", "Latin America")
    india = leads("Vietnam", "India")
    assert [r["url"] for r in brazil] == ["2", "3"]
    assert [r["url"] for r in india] == ["0", "3"]
    assert calls.count(("sector", "fertilizer")) == 1
    stale = {"title": "Fertilizer prices in Bangladesh", "title_en": "Fertilizers in Brazil",
             "exposure_paths": ["Sector · fertilizer"], "language": "pt-BR"}
    assert scope_sector_paths(stale, {"country_registration": "Brazil"})["exposure_paths"] == []
    assert stale["exposure_paths"]


def test_factor_queries_are_scoped_localized_and_preserve_successful_leads(monkeypatch):
    from ra import risk_factors
    from ra.exposure import check_topic, search_topic
    from ra.search import company_search_plan
    rows = [
        dict(english="Currency devaluation (more than 10% in one week)", scope="country",
             translations={"pt": "Desvalorização cambial (mais de 10% em uma semana)"}, enabled=True),
        dict(english="A lender freezes company credit", scope="company",
             translations={"pt": "Banco suspende o crédito"}, enabled=True),
        dict(english="Disabled factor", scope="company", translations={}, enabled=False),
    ]
    monkeypatch.setattr(risk_factors, "_rows", rows)
    company = {"name": "Acme Manufacturing", "country_registration": "Brazil"}
    plan = company_search_plan(company)
    assert ('"Acme Manufacturing" Banco suspende o crédito', "Brazil") in plan
    assert not any("devaluation" in q or "Disabled" in q for q, _ in plan)
    calls = []
    def select(branch, topic, *, edition, factor=None):
        if not factor:
            return []
        def fetch(url):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(url).query)["q"][0]
            calls.append(query)
            if not edition:
                raise OSError("timeout")
            return ("<rss><channel><item><title>Brasil: real cai 12%</title><link>https://example.org/real</link>"
                    "</item></channel></rss>").encode()
        return search_topic(branch, topic, fetch, edition=edition, factor=factor)
    hits, error = check_topic("country", "brazil", select, ["brazil"], include_factors=True)
    assert len(hits) == 1 and rows[0]["english"] in hits[0][1]
    assert "timeout" in error and "10%" not in calls[0]
    assert "Brasil" in calls[1] and "Desvalorização cambial" in calls[1]
    assert check_topic("sector", "fertilizer", select, ["brazil"], include_factors=True) == ([], "")


def test_topics_dropped_at_intake_before_signals_and_result_limit(monkeypatch):
    from ra import news_apis, search
    from ra.news_topics import excluded_story

    def check_signals(title, language=""):
        assert "extends debt" in title
        return []

    monkeypatch.setattr(search, "signal_terms", check_signals)
    feed = b"""<rss><channel>
      <item><title>Acme reports losses</title><category>Sports</category><link>https://x.test/a</link></item>
      <item><title>Acme reports losses</title><link>https://x.test/culture/b</link></item>
      <item><title>Acme extends debt</title><link>https://x.test/c</link></item>
    </channel></rss>"""
    assert [r["title"] for r in parse_feed(feed, "Acme", "en", limit=1)] == ["Acme extends debt"]
    for provider, field in (("thenewsapi", "data"), ("gnews", "articles")):
        rows = [{"title": "Acme files an appeal", "url": "https://x.test/d", "description": "The football club lost."},
                {"title": "Acme files an appeal", "url": "https://x.test/e", "categories": ["entertainment"]},
                {"title": "Acme extends debt", "url": "https://x.test/f"}]
        assert [r["title"] for r in news_apis.parse_articles(provider, {field: rows}, "Acme")] == ["Acme extends debt"]
    for title in ("Campeonato brasileiro de futebol", "Champions League draw", "Liên hoan phim Việt Nam",
                  "Фестиваль в Баку", "İstanbul sanat sergisi", "مهرجان القاهرة", "Διεθνές φεστιβάλ",
                  "Futbol bo‘yicha terma jamoa", "Bakı şəhərində konsert"):
        assert excluded_story(title, "https://x.test/a"), title
    for title in ("Agriculture exports fall", "Bank reviews risk culture", "Polymer film factory closes"):
        assert not excluded_story(title, "https://x.test/a"), title
    assert excluded_story("Brazil names replacements", "https://x.test/b", publisher="ESPN Brasil")
    assert excluded_story("مايفوتكش.. تحديد أسماء قائمة منتخب مصر وغيابات الأهلي وتهديد الزمالك", "https://x.test/a")
    assert excluded_story("غيابات الأهلي", "https://x.test/a", publisher="الوطن سبورت")
    assert not excluded_story("البنك الأهلي يرفع أسعار الفائدة", "https://x.test/a")


def test_exposure_excludes_sport_and_culture_even_when_risk_factor_matches():
    from ra.exposure import search_topic
    feed = b"""<rss><channel>
      <item><title>Brazil fertilizer sponsor faces losses after football deal</title><link>https://x.test/s</link></item>
      <item><title>Brazil fertilizer sponsor funds a museum</title><link>https://x.test/c</link></item>
      <item><title>Brazil fertilizer shipments face restrictions</title><link>https://x.test/r</link></item>
    </channel></rss>"""
    for scope, topic in (("country", "brazil"), ("sector", "fertilizer")):
        hits = search_topic(scope, topic, fetcher=lambda _: feed, factor=("New restrictions", "restrictions"))
        assert [item["url"] for item, channel in hits] == ["https://x.test/r"]
