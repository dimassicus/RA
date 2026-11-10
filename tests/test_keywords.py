import pytest

from ra import keywords
from ra.relevance import classify
from ra.scoring import score_article


def test_translation_retry_keeps_one_worker_and_exposes_pending_pass(monkeypatch):
    started = []
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            started.append(True)
    monkeypatch.setattr(keywords.threading, "Thread", Thread)
    monkeypatch.setattr(keywords, "_running", False)
    monkeypatch.setattr(keywords, "_requested", False)
    monkeypatch.setattr(keywords, "_progress", ("Idle", 0))
    keywords.schedule()
    keywords.schedule()
    status = keywords.translation_status()
    assert len(started) == 1
    assert status["running"] and status["retry_requested"]
    assert status["message"] == "Translation pass queued."


def test_batch_timeout_does_not_skip_other_languages_or_keywords(monkeypatch):
    from ra import risk_factors, exposure
    calls = []
    rows = [{"id": i, "english": f"Term {i}", "enabled": True, "translations": {}} for i in range(9)]
    monkeypatch.setattr(keywords, "_requested", True)
    monkeypatch.setattr(keywords, "_running", True)
    monkeypatch.setattr(keywords, "last_error", "")
    monkeypatch.setattr(keywords, "_progress", ("Idle", 0))
    monkeypatch.setattr(keywords.db, "list_companies", lambda active: [])
    monkeypatch.setattr(exposure, "topic_editions", lambda subjects: {("country", "russia"): {"russia"}})
    monkeypatch.setattr(risk_factors, "records", lambda: rows)
    monkeypatch.setattr(risk_factors, "languages", lambda: ["ce", "ru"])
    monkeypatch.setattr(keywords, "records", lambda: rows[:1])
    monkeypatch.setattr(keywords, "languages", lambda: ["ru"])
    monkeypatch.setattr(risk_factors, "reload", lambda: None)
    monkeypatch.setattr(keywords, "reload_rules", lambda: None)
    monkeypatch.setattr(risk_factors, "expand_queries", lambda code: None)
    def translate(batch, code, *, factors):
        calls.append((code, factors, len(batch)))
        if code == "ce" and batch[0]["id"] == 0:
            raise TimeoutError("timed out")
    monkeypatch.setattr(keywords, "translate_batch", translate)
    keywords._work()
    assert calls[0][:2] == ("ru", False)
    assert ("ru", True, 8) in calls and ("ru", True, 1) in calls
    assert ("ce", True, 1) in calls
    assert "Chechen" in keywords.last_error
    assert not keywords.translation_status()["running"]


@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
def test_large_keyword_batch_retries_only_unchanged_unsaved_items(monkeypatch, failure):
    from copy import deepcopy
    from ra import risk_factors, exposure
    rows = [{"id": i, "english": f"Term {i}", "enabled": True, "translations": {}} for i in range(19)]
    calls = []
    monkeypatch.setattr(keywords, "_requested", True)
    monkeypatch.setattr(keywords, "_running", True)
    monkeypatch.setattr(keywords, "last_error", "")
    monkeypatch.setattr(keywords, "_progress", ("Idle", 0))
    monkeypatch.setattr(keywords.db, "list_companies", lambda active: [])
    monkeypatch.setattr(exposure, "topic_editions", lambda subjects: {})
    monkeypatch.setattr(risk_factors, "records", lambda: [])
    monkeypatch.setattr(risk_factors, "languages", lambda: [])
    monkeypatch.setattr(risk_factors, "expand_queries", lambda code: None)
    monkeypatch.setattr(keywords, "records", lambda: deepcopy(rows))
    monkeypatch.setattr(keywords, "languages", lambda: ["ru"])
    monkeypatch.setattr(keywords, "reload_rules", lambda: None)
    def translate(batch, code, *, factors):
        assert not factors
        calls.append([row["id"] for row in batch])
        if len(calls) == 1:
            rows[0]["translations"]["ru"] = "Already approved"
            rows[1]["english"] = "Analyst changed wording"
            rows[2]["enabled"] = False
            rows.pop(3)
            raise failure("Large request failed")
    monkeypatch.setattr(keywords, "translate_batch", translate)
    keywords._work()
    assert [len(batch) for batch in calls] == [16, 8, 4, 3]
    assert [row_id for batch in calls[1:] for row_id in batch] == list(range(4, 19))
    assert not keywords.last_error


def test_approved_translations_survive_a_later_retry_timeout(monkeypatch):
    from ra import translation
    writes = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            writes.append((sql, params))
            return type("Result", (), {"rowcount": 1})()
    monkeypatch.setattr(keywords.db, "connect", Connection)
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda terms, code: {"First": "Первый", "Second": "Второй"})
    monkeypatch.setattr(keywords, "review_translations", lambda *args: {"First": True, "Second": False})
    def timeout(*args, **kwargs): raise TimeoutError("retry timed out")
    monkeypatch.setattr(keywords, "model_json", timeout)
    monkeypatch.setattr(keywords, "_progress", ("Idle", 0))
    with pytest.raises(TimeoutError):
        keywords.translate_batch([{"id": 1, "english": "First"}, {"id": 2, "english": "Second"}], "ru")
    assert len(writes) == 1
    sql, params = writes[0]
    assert params[1:] == (1, "First", "ru")
    assert "AND NOT (translations ? %s)" in sql


def test_ambiguous_keyword_meanings_reach_generator_and_reviewer(monkeypatch):
    from ra import translation
    from ra.news_apis import ProviderError
    calls, writes = [], []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            writes.append(params)
            return type("Result", (), {"rowcount": 1})()
    def unavailable(*args): raise ProviderError("Unavailable")
    def model(prompt, data, schema, **kwargs):
        calls.append(data)
        assert "court" in data["intended_meanings"]["conviction"]
        assert "physical explosion" in data["intended_meanings"]["explosion"]
        return {"conviction": "putusan bersalah", "explosion": "ledakan"} if "terms" in data else {"conviction": True, "explosion": True}
    monkeypatch.setattr(keywords.db, "connect", Connection)
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda *args: {})
    monkeypatch.setattr(keywords.online_translation, "translate", unavailable)
    monkeypatch.setattr(keywords, "model_json", model)
    monkeypatch.setattr(keywords, "_progress", ("Idle", 0))
    keywords.translate_batch([{"id": 1, "english": "conviction"}, {"id": 2, "english": "explosion"}], "id")
    assert len(calls) == 2 and len(writes) == 2


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
