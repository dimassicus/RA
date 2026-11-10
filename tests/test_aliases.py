from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from ra.aliases import normalized, parse_aliases, validate_aliases, provider_name
from ra.app import form_company
from ra.relevance import classify
from ra.scoring import score_article
from ra.search import search_queries
from ra import news_apis, db


VIETNAM = {"name": "PetroVietnam Fertilizer and Chemicals Corporation", "country_registration": "Vietnam",
           "aliases": "PVFCCo\nPhân bón và Hóa chất Dầu khí\nDPM"}
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def test_local_scripts_and_alias_validation():
    assert normalized("Hóa chất Dầu khí") == normalized("Hoa chat Dau khi")
    assert normalized("SANAYİİ ŞİRKETİ") == normalized("sanayii sirketi")
    assert normalized("gıda") == "gida"
    assert parse_aliases("PVFCCo; pvfcco\n  Phân bón và Hóa chất Dầu khí ") == ["PVFCCo", "Phân bón và Hóa chất Dầu khí"]
    assert validate_aliases('"PVFCCo"; PVFCCo') == "PVFCCo"
    for value in ("AB", "x" * 151, "\n".join(f"company{i}" for i in range(9))):
        with pytest.raises(ValueError):
            validate_aliases(value)
    assert form_company(VIETNAM)["aliases"] == VIETNAM["aliases"]


def test_alias_identity_is_shared_by_scoring_and_risk_filter():
    for title in ("PVFCCo reports losses", "Phan bon va Hoa chat Dau khi reports losses"):
        article = {"title": title, "language": "en", "published_at": NOW}
        assert classify(article, VIETNAM, NOW)["relevance"] == "risk"
        assert score_article(article, VIETNAM, NOW)["score"] == -1
        assert not score_article(article, VIETNAM, NOW)["score_included"]
    for title in ("DPM reports losses", "PVFCCoPlus reports losses", "Petrovietnam reports losses"):
        article = {"title": title, "language": "en", "published_at": NOW}
        assert classify(article, VIETNAM, NOW)["relevance"] == "review"
        assert not score_article(article, VIETNAM, NOW)["score_included"]
    contextual = {"title": "DPM in Vietnam reports losses", "language": "en", "published_at": NOW}
    assert score_article(contextual, VIETNAM, NOW)["score"] == -1
    assert not score_article(contextual, VIETNAM, NOW)["score_included"]


def test_publishers_and_affiliates_are_not_company_aliases():
    subject = {**VIETNAM, "affiliates": "Eurochem", "key_owners": "Petrovietnam"}
    for title in ("Eurochem files for bankruptcy", "Petrovietnam reports losses", "Other company reports losses - PVFCCo"):
        article = {"title": title, "publisher": "PVFCCo", "published_at": NOW}
        assert not score_article(article, subject, NOW)["score_included"]


def test_searches_keep_legal_name_and_local_alias_without_extra_requests():
    queries = search_queries(VIETNAM)
    assert queries == ['"PetroVietnam Fertilizer and Chemicals Corporation"', '"PVFCCo"', '"Phân bón và Hóa chất Dầu khí"']
    assert len(search_queries({**VIETNAM, "aliases": "\n".join(f"Company {i}" for i in range(8))})) == 3
    short = {"name": "Full Legal Company", "aliases": "DPM", "country_registration": "Vietnam"}
    assert '"DPM" "Vietnam"' in search_queries(short)
    assert provider_name(short) == "Full Legal Company"


@pytest.mark.parametrize("provider,env,param,payload", [
    ("gnews", "RA_GNEWS_API_KEY", "q", {"articles": []}),
    ("thenewsapi", "RA_THENEWSAPI_API_KEY", "search", {"data": []}),
])
def test_provider_searches_alternative_names_with_native_query_syntax(monkeypatch, provider, env, param, payload):
    monkeypatch.setenv(env, "fixture-key")
    calls, reservations = [], []
    def fetch(url):
        calls.append(parse_qs(urlparse(url).query))
        return payload
    def reserve(provider, limit):
        reservations.append((provider, limit))
        return True
    news_apis.search(provider, VIETNAM, fetcher=fetch, reserve=reserve, now=NOW)
    assert len(calls) == len(reservations) == 1
    query = calls[0][param][0]
    assert '"PVFCCo"' in query and '"Phân bón và Hóa chất Dầu khí"' in query
    assert '"PetroVietnam Fertilizer and Chemicals Corporation"' in query
    assert (' OR ' if provider == 'gnews' else ' | ') in query
    assert '"DPM"' in query and 'Vietnam' in query
    assert 'lang' not in calls[0] and 'language' not in calls[0]


def test_empty_new_fields_preserve_pre_alias_intake_fingerprint():
    old = {"name": "Company", "tax_id": "123"}
    assert db.intake_hash_data({**old, "aliases": "", "alias_notes": ""}) == old
    assert db.intake_hash_data({**old, "aliases": "Company Brand"})["aliases"] == "Company Brand"


def test_sourced_review_preserves_existing_notes_and_is_idempotent():
    from scripts.review_company_aliases import planned_changes
    review = {"changes": {"short_name": ["Old", "New"]}, "aliases": ["New Name"],
              "note": "Reviewed source", "sources": ["https://example.test/about"]}
    company = {"id": 1, "short_name": "Old", "aliases": "Existing Name", "alias_notes": "Analyst note"}
    changes = planned_changes(company, review)
    assert changes["aliases"] == "Existing Name\nNew Name"
    assert changes["alias_notes"].startswith("Analyst note\n\n")
    assert planned_changes({**company, **changes}, review) == {}
    with pytest.raises(ValueError, match="changed since review"):
        planned_changes({**company, "short_name": "Unrelated Name"}, review)
