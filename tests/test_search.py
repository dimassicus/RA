from datetime import timezone

from ra.search import (
    FCA_WARNINGS_RSS_URL, fca_warning_list, has_uk_presence, heringer_official_notices,
    is_heringer, parse_feed, search_company, search_queries, signal_terms,
)


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
