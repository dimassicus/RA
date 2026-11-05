import pytest

from ra import keywords
from ra.relevance import classify
from ra.scoring import score_article


def test_keyword_matches_language_and_sentiment_without_establishing_company_impact(monkeypatch):
    rows = [
        dict(english="covenant breach", direction="adverse", enabled=True, translations={"pt": "quebra de cláusula"}),
        dict(english="debt relief", direction="favorable", enabled=True, translations={"pt": "alívio da dívida"}),
        dict(english="closure", direction="adverse", enabled=False, translations={}),
    ]
    monkeypatch.setattr(keywords, "_rules", keywords.compile_terms(rows))
    assert not keywords.matches("quebra de cláusula", "en")
    assert not keywords.matches("closure or covenant breaches")
    article = dict(title="Acme Manufacturing anuncia alívio da dívida", language="pt-BR", verification={})
    company = {"name": "Acme Manufacturing"}
    result = classify(article, company)
    assert result["keyword_hits"] == [{"term": "debt relief", "direction": "favorable"}]
    assert result["relevance"] == "review"
    assert not score_article(article, company)["score_included"]


def test_foreign_or_unverified_english_rejected_before_insert(monkeypatch):
    monkeypatch.setattr(keywords, "model_json", lambda *args: {"english": False})
    for term in ["закрытие", "quebra de contrato"]:
        with pytest.raises(ValueError, match="English"):
            keywords.save(0, term, "neutral", True, "")


def test_starter_library_has_a_literal_translation_for_every_initial_market():
    codes = {"ar", "az", "el", "pt", "ru", "tr", "uz", "vi"}
    assert set(keywords.starter_translations()) == set(keywords.SEEDS)
    for row in keywords.defaults():
        for code in codes:
            phrase = row["translations"][code]
            assert any(term == row["english"] for term, _, _ in keywords.matches(phrase, code))
