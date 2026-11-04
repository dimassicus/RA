from datetime import datetime, timedelta, timezone

from ra.relevance import classify, classify_results, risk_signals


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
SUBJECT = {"name": "Acme Manufacturing S.A.", "short_name": "Acme Manufacturing"}


def article(title, **extra):
    return {"title": title, "published_at": NOW, "source_kind": "news", **extra}


def test_requires_both_identity_and_adverse_event():
    assert classify(article("Acme Manufacturing faces bankruptcy"), SUBJECT, NOW)["relevance"] == "risk"
    assert classify(article("Other company faces bankruptcy"), SUBJECT, NOW)["relevance"] == "review"
    assert classify(article("Acme Manufacturing wins sustainability award"), SUBJECT, NOW)["relevance"] == "background"
    assert classify(article("Vietnam national team prepares for ASEAN Cup"), SUBJECT, NOW)["relevance"] == "background"


def test_whole_word_cues_and_names_do_not_match_substrings_or_publisher():
    assert risk_signals("Gloss finish, simultaneous operations and successful cash flow") == []
    assert classify(article("Acme ManufacturingPlus faces losses"), SUBJECT, NOW)["relevance"] == "review"
    assert classify(article("Other business faces losses - Acme Manufacturing", publisher="Acme Manufacturing"), SUBJECT, NOW)["relevance"] == "review"


def test_official_notices_require_a_risk_cue_but_not_a_company_name():
    assert classify(article("Fato Relevante - Hibernação Plantas", source_kind="official"), SUBJECT, NOW)["relevance"] == "risk"
    assert classify(article("Comunicado - Atualização de Logomarca", source_kind="official"), SUBJECT, NOW)["relevance"] == "background"


def test_translation_and_native_language_cues_are_used():
    result = classify(article("Acme Manufacturing 财务消息", title_en="Acme Manufacturing reports losses"), SUBJECT, NOW)
    assert result["relevance"] == "risk"
    assert risk_signals("CMS JANT konkordato ilan etti") == ["restructuring"]
    assert risk_signals("Fertilizantes Heringer: prejuízo e recuperação judicial")
    untranslated = classify(article("Acme Manufacturing 财务消息", language="zh"), SUBJECT, NOW)
    assert untranslated["relevance"] == "review"


def test_old_missing_and_future_dates_are_not_recent_risks():
    title = "Acme Manufacturing faces bankruptcy"
    assert classify(article(title, published_at=NOW - timedelta(days=31)), SUBJECT, NOW)["relevance"] == "older"
    for date in (None, NOW + timedelta(days=2)):
        assert classify(article(title, published_at=date), SUBJECT, NOW)["relevance"] == "review"


def test_negation_and_short_ambiguous_alias_need_review():
    assert classify(article("Acme Manufacturing denies fraud"), SUBJECT, NOW)["relevance"] == "review"
    mars = {"name": "MARS-A", "country_registration": "Azerbaijan", "primary_industries": "Pipe production"}
    assert classify(article("Mars-A reports losses"), mars, NOW)["relevance"] == "review"
    assert classify(article("Mars-A in Azerbaijan reports losses"), mars, NOW)["relevance"] == "risk"


def test_classification_does_not_mutate_saved_leads_and_uses_run_snapshot():
    saved = article("Acme Manufacturing reports losses", subject_name=SUBJECT["name"], signal_terms=[])
    result = classify_results({"subjects": [SUBJECT], "created_at": NOW}, [saved])
    assert result[0]["relevance"] == "risk"
    assert saved["signal_terms"] == []
    assert "relevance" not in saved
