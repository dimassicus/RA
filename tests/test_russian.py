"""Russian display copies preserve the canonical data even when translation fails."""
from unittest.mock import Mock

import pytest

from ra import russian


@pytest.fixture
def generate(monkeypatch):
    russian._passage.cache_clear()
    monkeypatch.setattr(russian.translation, "_model", lambda source, target: None)
    monkeypatch.setattr(russian.model_store, "ensure_model", lambda source, target: (False, "offline"))
    model = Mock()
    monkeypatch.setattr(russian.keywords, "model_json", model)
    yield model
    russian._passage.cache_clear()


def test_long_summary_preserves_all_passages_layout_company_names_and_links(generate):
    subject = {"name": "АО Альфа", "short_name": "Alpha", "aliases": "ALFA"}
    lines = [" ".join(f"{name} repaid USD {n} million." for n in range(60)) for name in ("АО Альфа", "Alpha", "ALFA")]
    text = "\n\n".join(lines + ["Filing AB-123: https://example.test/filings/123"])
    def render(passage):
        return passage.replace("repaid", "погасила").replace("million", "млн").replace("Filing", "Документ")
    generate.side_effect = lambda prompt, source, schema, **kwargs: {"russian": render(source)}

    assert russian.translate(text, subject) == render(text)
    calls = generate.call_count
    assert calls > len(lines) + 1
    assert russian.translate(text, subject) == render(text)
    assert generate.call_count == calls


@pytest.mark.parametrize(("source", "translation"), [
    ("Acme won a USD 25 million contract.", "Acme выиграла контракт на 25 млн долларов."),
    ("Рост for Acme was 14 percent.", "Рост у Acme составил 14 %."),
    ("Acme will try to repay USD 5 million.", "Acme попробует погасить 5 млн долларов."),
    ("Acme paid 1 Brazilian real.", "Acme заплатила 1 реал."),
    ("Acme borrowed TRY 8 million.", "Acme привлекла 8 млн турецких лир."),
    ("Acme borrowed BRL 12 million.", "Acme привлекла 12 млн бразильских реалов."),
])
def test_mixed_language_financial_text_accepts_equivalent_russian(source, translation, generate):
    generate.return_value = {"russian": translation}
    assert russian.translate(source) == translation


@pytest.mark.parametrize(("before", "after"), [
    ("ZXQNAME0ZXQ", "Другая компания"),
    ("AB-123", "AC-123"),
    ("−12", "12"),
    ("USD", "EUR"),
    ("миллионов", "миллиардов"),
    ("5%", "5"),
])
def test_failed_passage_returns_no_partial_copy_and_can_be_retried(before, after, generate):
    text = "Overview\n\nAcme reported −12 million USD and 5% for filing AB-123."
    good = "ZXQNAME0ZXQ указала −12 миллионов USD и 5% в документе AB-123."
    generate.side_effect = [{"russian": "Обзор"}, {"russian": good.replace(before, after)}, {"russian": ""}, {"russian": good}]

    assert russian.translate(text, "Acme") == ""
    assert russian.translate(text, "Acme") == "Обзор\n\n" + good.replace("ZXQNAME0ZXQ", "Acme")
    assert generate.call_count == 4


def test_unavailable_model_does_not_poison_retry(generate):
    generate.side_effect = [OSError("model unavailable"), {"russian": "Стоимость снизилась."}]
    assert russian.translate("Costs declined.") == ""
    assert russian.translate("Costs declined.") == "Стоимость снизилась."


def test_offline_model_precedes_local_fallback_and_preserves_currency(monkeypatch, generate):
    tokenizer, translator = Mock(), Mock()
    monkeypatch.setattr(russian.translation, "_model", lambda *_: (tokenizer, translator, ""))
    tokenizer.encode.side_effect = lambda source, **kwargs: [source]
    tokenizer.decode_pieces.side_effect = lambda pieces: pieces[0]
    translator.translate_batch.side_effect = [
        [Mock(hypotheses=[["Долг: 4 млн USD"]])],
        [Mock(hypotheses=[["Долг: 6 млн EUR"]])],
    ]
    generate.return_value = {"russian": "Долг: 6 млн USD"}
    assert russian.translate("Debt: USD 4 million") == "Долг: 4 млн USD"
    generate.assert_not_called()
    assert russian.translate("Debt: USD 6 million") == "Долг: 6 млн USD"
    generate.assert_called_once()


def test_repair_protects_original_amount_and_rechecks_result(generate):
    generate.side_effect = [
        {"russian": "Сумма составила 100 000 долларов."},
        {"russian": "Сумма составила ZXQVALUE0ZXQ долларов."},
    ]
    assert russian.translate("The total was $100,000.") == "Сумма составила 100,000 долларов."
    assert "ZXQVALUE0ZXQ" in generate.call_args.args[1]


def test_currency_prefix_and_plural_percentages(generate):
    generate.return_value = {"russian": "Проценты в Европе указаны в евро."}
    assert russian.translate("Percentages in Europe are stated in euros.") == generate.return_value["russian"]
    with pytest.raises(ValueError, match="currency"):
        russian._checked("Paid in euros", "Оплачено в Европе", {})


def test_truncated_issuer_name_preserves_only_verified_prefix(generate):
    assert russian.translate("ACME INDUSTRIES ...", "Acme Industries Limited") == "ACME INDUSTRIES ..."
    generate.assert_not_called()
