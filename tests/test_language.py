from ra.language import detect_language
from ra.model_store import country_languages


def test_greek_navigation_does_not_override_english_article():
    body = 'Απόψεις - Αναλύσεις\nFacebook\nMarket Comment\n' + (
        'The company reported higher annual revenue and operating profit. ' * 12)
    assert detect_language(body) == 'en'


def test_article_language_overrides_feed_edition():
    assert detect_language("Новые склады класса А откроются в Ташкенте - Spot.uz", "uz-UZ") == "ru"
    assert detect_language("Petrovietnam accelerates smart factory drive with AI", "vi-VN") == "en"
    assert detect_language("Νέος διευθύνων σύμβουλος για την εταιρεία", "el-GR") == "el"
    assert detect_language("Fato Relevante - Renúncia do Sr. Daniil Bazdyrev", "pt-BR") == "pt"


def test_multilingual_countries_expand_expected_model_list():
    assert {"uz", "ru"} <= set(country_languages("Uzbekistan"))
    assert {"kk", "ru"} <= set(country_languages("Kazakhstan"))
    assert {"de", "fr", "it"} <= set(country_languages("Switzerland"))
    assert "tr" in country_languages("Turkey")
