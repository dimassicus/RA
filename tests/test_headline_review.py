import pytest

from ra import headline_review as qa, translation
from ra.company_exports import title
from ra.scoring import input_hash


@pytest.mark.parametrize('output', ['Company repays debt - ZXQPUBLERZXQ', 'Company repays debt'])
def test_corrupted_or_dropped_protection_markers_trigger_fallback(monkeypatch, output):
    from types import SimpleNamespace
    tokenizer = SimpleNamespace(encode=lambda text, **kw: [text], decode_pieces=lambda pieces: pieces[0])
    translator = SimpleNamespace(translate_batch=lambda *a, **kw: [SimpleNamespace(hypotheses=[[output]])])
    monkeypatch.setattr(translation, '_model', lambda *a: (tokenizer, translator, ''))
    monkeypatch.setattr(qa, 'fallback', lambda *a: ('Company repays debt - Brand', 'fallback'))
    assert translation.translate_text('Empresa paga dívida - Brand', 'pt', publisher='Brand') == (
        'Company repays debt - Brand', 'fallback')


def test_intact_markers_restore_original_names(monkeypatch):
    from types import SimpleNamespace
    tokenizer = SimpleNamespace(encode=lambda text, **kw: [text], decode_pieces=lambda pieces: pieces[0])
    translator = SimpleNamespace(translate_batch=lambda tokens, **kw: [SimpleNamespace(hypotheses=[tokens[0]])])
    monkeypatch.setattr(translation, '_model', lambda *a: (tokenizer, translator, ''))
    assert translation.argos_translate('Acme informa - Brand', 'pt', 'Acme', 'Brand') == (
        'Acme informa - Brand', 'Argos offline model')


@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    def unavailable(*args):
        raise qa.ProviderError("offline test")
    monkeypatch.setattr(qa.online_translation, "translate", unavailable)


def test_fallback_uses_only_gemma_and_preserves_publisher(monkeypatch):
    import json
    calls = []
    def local(port, path, body, timeout):
        model = body["model"]
        calls.append(model)
        assert body["reasoning_effort"] == "none"
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        supported = model == qa.GEMMA
        return {"model": model, "choices": [{"finish_reason": "stop", "message": {"content":
                json.dumps({"supported": supported, "english": "The company repaid its loan." if supported else ""})}}]}
    monkeypatch.setattr(qa, "local_request", local)
    monkeypatch.setattr(translation, "argos_translate", lambda *a: ("", "no model"))
    candidate, provider = translation.translate_text("Компания погасила кредит - Пресса", "ru", publisher="Пресса")
    assert calls == [qa.GEMMA]
    assert candidate == "The company repaid its loan. - Пресса" and qa.GEMMA in provider


def test_invalid_envelopes_leave_original_for_review(monkeypatch):
    monkeypatch.setattr(qa, "local_request", lambda *a, **kw: {"model":"other","choices":[]})
    candidate, provider = qa.fallback("سندات", "ar", "", "no Argos")
    assert not candidate and "needs review" in provider


def test_gemma_failure_does_not_fall_back_to_qwen(monkeypatch):
    calls = []
    def unavailable(port, path, body, timeout):
        calls.append(body["model"])
        raise ConnectionRefusedError()
    monkeypatch.setattr(qa, "local_request", unavailable)
    candidate, provider = qa.fallback("Empresa paga dívida", "pt", "", "no Argos")
    assert calls == [qa.GEMMA]
    assert not candidate and "needs review" in provider


def test_unusable_review_never_promotes_candidate_or_changes_score_hash(monkeypatch):
    row = {"title": "Crédito em queda", "language": "pt", "title_en": "Credit rises"}
    before = input_hash(row, {})
    def reviewer(*a, **kw):
        assert kw["enabled"] is False
        raise qa.ReviewError("Missing quoted evidence", {})
    monkeypatch.setattr(qa, "review", reviewer)
    row["headline_review"] = qa.audit(row["title"], "pt", row["title_en"])
    assert row["headline_review"]["status"] == "inconclusive"
    assert title({"article": row}) == row["title"]
    assert before == input_hash(row, {})
    assert qa.audit("English headline", "en", "English headline") == {}


def test_legacy_argos_archive_installs_without_downloading_again(monkeypatch, tmp_path):
    from ra import model_store
    from zipfile import ZipFile
    monkeypatch.setattr(model_store, "MODEL_ROOT", tmp_path)
    monkeypatch.setattr(model_store, "package_index", lambda: [{"from_code":"ar","to_code":"en",
                         "links":["https://argos-net.com/model"]}])
    with ZipFile(tmp_path / ".ar_en.download", "w") as pkg:
        pkg.writestr("ar_en/model/model.bin", b"test")
        pkg.writestr("ar_en/sentencepiece.model", b"test")
        pkg.writestr("ar_en/../../bad", b"test")
    monkeypatch.setattr(model_store, "_download", lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert model_store.ensure_model("ar")[0]
    assert model_store.installed("ar")


def test_missing_argos_uses_online_for_any_language_before_local(monkeypatch):
    monkeypatch.setattr(translation, "argos_translate", lambda *args: ("", "no model"))
    calls = []
    def online(text, source, target):
        calls.append((text, source, target))
        return "Company repays debt"
    monkeypatch.setattr(qa.online_translation, "translate", online)
    for lang in ("uz", "sw-KE"):
        assert translation.translate_text("Headline - Brand", lang, publisher="Brand") == (
            "Company repays debt - Brand", "MyMemory online")
    assert calls == [("Headline", "uz", "en"), ("Headline", "sw", "en")]


def test_readiness_requires_api_and_downloaded_qwen_without_loading(monkeypatch):
    monkeypatch.setattr(qa.local_models, "cli_path", lambda: "/lms")
    def disconnected(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(qa, "local_request", disconnected)
    assert not qa.readiness()["ready"]
    assert "1234" in qa.readiness()["message"]
    monkeypatch.setattr(qa, "local_request", lambda *a, **kw: {"data": [{"id": qa.GEMMA, "state": "loaded"}]})
    assert not qa.readiness()["ready"]
    monkeypatch.setattr(qa, "local_request", lambda *a, **kw: {"data": [{"id": qa.QWEN, "state": "loaded"}]})
    assert qa.readiness()["ready"]
    monkeypatch.setattr(qa, "local_request", lambda *a, **kw: {"data": [{"id": qa.QWEN, "state": "not-loaded"}]})
    assert qa.readiness()["ready"]
    monkeypatch.setattr(qa.local_models, "_activity", "Loading Gemma 31B…")
    assert qa.readiness()["message"] == "Loading Gemma 31B…"


def test_successful_audit_requests_reasoning_off(monkeypatch):
    qa._audit.cache_clear()
    def reviewer(*args, **kwargs):
        assert kwargs == {"enabled": False, "shadow_only": True, "retry_reason": "", "request_fn": qa.local_request}
        return {"assessments": [{"verdict": "faithful", "issues": []}]}, {"model": qa.QWEN, "protocol": "shadow-v1"}
    monkeypatch.setattr(qa, "review", reviewer)
    assert qa.audit("Empresa paga dívida", "pt", "Company repays debt")["reasoning"] == "off"
    qa._audit.cache_clear()


def test_retry_feedback_keeps_reasoning_off_and_evidence_validation(monkeypatch):
    import json
    from scripts.translation_review import simple
    answer = {'reviews': [{'candidate': 'A', 'verdict': 'uncertain', 'issues': []}]}
    def local(port, path, body, timeout):
        assert body['reasoning_effort'] == 'none'
        assert body['chat_template_kwargs']['enable_thinking'] is False
        assert 'previous response failed validation' in body['messages'][0]['content']
        issue = body['response_format']['json_schema']['schema']['properties']['reviews']['items']['properties']['issues']['items']
        assert issue['properties']['source']['minItems'] == 1
        assert issue['properties']['source']['maxItems'] == 6
        return {'model': qa.QWEN, 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}
    monkeypatch.setattr(simple, 'local_request', local)
    result, _ = simple.review('original', 'es', 'en', {'A': 'translation'}, qa.QWEN, qa.PORT,
                             enabled=False, shadow_only=True, retry_reason='Evidence does not match an original text span')
    assert result['assessments'][0]['verdict'] == 'uncertain'
    answer['reviews'][0] = {'candidate': 'A', 'verdict': 'material_error', 'issues': [{
        'category': 'identity', 'source': ['invented'], 'translation': ['translation'], 'explanation': 'Mismatch'}]}
    with pytest.raises(qa.ReviewError):
        simple.review('original', 'es', 'en', {'A': 'translation'}, qa.QWEN, qa.PORT,
                      enabled=False, shadow_only=True, retry_reason='Evidence does not match an original text span')
