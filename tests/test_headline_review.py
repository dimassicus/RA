import pytest

from ra import headline_review as qa, translation
from ra.company_exports import title
from ra.scoring import input_hash


@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    def unavailable(*args):
        raise qa.ProviderError("offline test")
    monkeypatch.setattr(qa.online_translation, "translate", unavailable)


def test_fallback_abstains_before_trying_gemma_and_preserves_publisher(monkeypatch):
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
    assert calls == [qa.QWEN, qa.GEMMA]
    assert candidate == "The company repaid its loan. - Пресса" and qa.GEMMA in provider


def test_invalid_envelopes_leave_original_for_review(monkeypatch):
    monkeypatch.setattr(qa, "local_request", lambda *a, **kw: {"model":"other","choices":[]})
    candidate, provider = qa.fallback("سندات", "ar", "", "no Argos")
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
