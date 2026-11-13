import json
from datetime import datetime, timezone

from ra import disclosures


SUBJECT = {"id": 77, "name": "Example Chemicals", "country_registration": "Germany", "active": True}


def catalog(**params):
    if params["action"] == "wbsearchentities":
        return {"search": [{"id": "Q1", "label": "Example Chemicals"}]}
    if params["ids"] == "Q1":
        values = {"P17": {"id": "Q2"}, "P856": "http://example.test"}
    else:
        values = {"P297": "DE"}
    return {"entities": {params["ids"]: {"claims": {key: [{"mainsnak": {"datavalue": {"value": value}}}]
                                                               for key, value in values.items()}}}}


def test_discovers_new_company_without_a_company_specific_adapter():
    def fetch(url):
        return (b'<html>Example Chemicals<a href="/investors/disclosures">Investor disclosures</a>'
                b'<link type="application/rss+xml" href="/feed/">'
                b'<a href="https://other.test/news">Group news</a></html>', "text/html", url)
    result = disclosures.discover(SUBJECT, lookup=catalog, fetch=fetch)
    assert {item["url"] for item in result["sources"]} == {"https://example.test/investors/disclosures", "https://example.test/feed/"}
    assert result["identity"] == disclosures.identity(SUBJECT)
    assert result["status"] == "partial" and not result["gaps"]


def test_wrong_country_or_unconfirmed_page_does_not_become_a_source():
    assert disclosures.website_candidates(SUBJECT | {"country_registration": "Japan"}, catalog) == []
    result = disclosures.discover(SUBJECT, lookup=catalog,
        fetch=lambda url: (b'Unrelated issuer<a href="/news">News</a>', "text/html", url))
    assert result["status"] == "missing" and all(item["kind"] == "candidate" for item in result["sources"])
    assert any("not confirmed" in gap for gap in result["gaps"])


def test_disclosure_collection_respects_cutoff_and_keeps_entity_verification():
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    end = datetime(2026, 10, 12, tzinfo=timezone.utc)
    state = {"identity": disclosures.identity(SUBJECT), "gaps": [], "sources": [
        {"url": "https://example.test/disclosures", "kind": "page"}]}
    entries = [dict(headline="Published disclosure", datePublished=date, url=url) for date, url in [
        ("2026-10-11T12:00:00Z", "/within"), ("2026-10-13T12:00:00Z", "/outside"),
        ("2026-10-11T12:00:00", "/unknown-zone"), ("2026-10-11T12:00:00Z", "https://other.test/wrong")]]
    body = ('<script type="application/ld+json">' + json.dumps({"@graph": entries}) + '</script>').encode()
    results, gaps = disclosures.collect(SUBJECT | {"disclosure_sources": state}, window=(start, end),
        fetch=lambda url: (body, "text/html", url))
    assert [item["url"] for item in results] == ["https://example.test/within"]
    assert results[0]["source_kind"] == "news"
    assert not gaps


def test_identity_change_and_undated_page_remain_visible_gaps():
    state = {"identity": disclosures.identity(SUBJECT), "gaps": [], "sources": [
        {"url": "https://example.test/disclosures", "kind": "page"}]}
    def no_fetch(url):
        raise AssertionError("Changed identity must not reuse the source")
    results, gaps = disclosures.collect(SUBJECT | {"name": "Another company", "disclosure_sources": state}, fetch=no_fetch)
    assert not results and "identity has changed" in gaps[0]
    results, gaps = disclosures.collect(SUBJECT | {"disclosure_sources": state},
        fetch=lambda url: (b'<html>No machine readable dates</html>', "text/html", url))
    assert not results and "does not establish" in gaps[0]


def test_independent_archives_overlap_and_keep_coverage_order():
    import threading

    rendezvous = threading.Barrier(2)
    first, second = "https://example.test/feed", "https://example.test/archive"
    state = {"identity": disclosures.identity(SUBJECT), "sources": [
        {"url": first, "kind": "page"}, {"url": second, "kind": "page"}]}
    def fetch(url):
        rendezvous.wait(timeout=5)
        if url == first:
            raise OSError("archive offline")
        return b'<script type="application/ld+json">{"headline":"Filing","datePublished":"2026-11-07T12:00:00Z","url":"/filing"}</script>', "text/html", url

    coverage = []
    rows, gaps = disclosures.collect({**SUBJECT, "disclosure_sources": state}, coverage=coverage, fetch=fetch)
    assert [row["url"] for row in coverage] == [first, second]
    assert [row["url"] for row in rows] == ["https://example.test/filing"]
    assert gaps == [f"{first}: OSError: archive offline"]
