import pytest

from ra import keywords
from ra.relevance import classify
from ra.scoring import score_article


@pytest.mark.parametrize('valid_later_quote', [True, False])
def test_later_direct_theme_evidence_upgrades_partial_only_when_valid(monkeypatch, valid_later_quote):
    from ra import risk_factors
    first, later = 'Some conditions apply.', 'All conditions now apply.'
    body = first + ' ' * (6000 - len(first)) + later
    answers = iter([
        {'matches': [{'id': 1, 'kind': 'partial', 'quote': first, 'connection': 'Some conditions.'}]},
        {'matches': [{'id': 1, 'kind': 'direct', 'quote': later if valid_later_quote else 'Invented',
                      'connection': 'All conditions.'}]},
    ])
    monkeypatch.setattr(keywords, 'model_json', lambda *a: next(answers))
    result = risk_factors.assess({'title': 'Update'}, {'status': 'available', 'body': body}, {},
                                [{'id': 1, 'english': 'Theme', 'scope': 'company'}])
    assert len(result['matches']) == 1
    assert result['matches'][0]['kind'] == ('direct' if valid_later_quote else 'partial')
    assert result['status'] == ('checked' if valid_later_quote else 'inconclusive')


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


def test_semantic_review_reads_original_body_and_rejects_invented_evidence(monkeypatch):
    from ra import risk_factors
    factor = dict(id=41, english="Central bank tightens capital controls", scope="country")
    sentence = "Importadores no Brasil agora precisam de autorização para pagar fornecedores no exterior."
    calls = []
    def review(prompt, data, schema):
        calls.append(data)
        return {"matches": [
            {"id":41, "kind":"partial", "quote":sentence, "connection":"Prior approval for payments to overseas suppliers restricts transfers."},
            {"id":41, "kind":"partial", "quote":"A new weekly cap was imposed.", "connection":"Unsupported claim."}]}
    monkeypatch.setattr(keywords, "model_json", review)
    review = risk_factors.assess({"title":"Monthly update"}, {"status":"available", "body":sentence},
                                 {"name":"Acme", "country_registration":"Brazil"}, [factor])
    assert calls[0]["source_text"] == sentence and calls[0]["themes"] == [factor]
    assert review["status"] == "inconclusive" and len(review["matches"]) == 1
    assert review["matches"][0]["quote"] == sentence and review["matches"][0]["from"] == "source text"
    monkeypatch.setattr(keywords, "model_json", lambda *args: (_ for _ in ()).throw(OSError()))
    review = risk_factors.assess({"title":"Company news"}, {}, {}, [factor])
    assert review == {"status":"incomplete", "coverage":"headline only", "matches":[]}
