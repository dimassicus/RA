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
    assert set(country_languages("Russian Federation")) == {"ru", "ba", "tt"}


def test_concurrent_detection_does_not_use_partially_initialized_profiles(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace
    import langdetect
    from ra.language import _detected
    entered, second_started, overlap, release = Event(), Event(), Event(), Event()
    active = []
    def detect(text):
        if active:
            overlap.set()
        active.append(text)
        entered.set()
        assert release.wait(2)
        active.remove(text)
        return [SimpleNamespace(lang='pt', prob=1.0)]
    monkeypatch.setattr(langdetect, 'detect_langs', detect)
    def second():
        second_started.set()
        return _detected('second')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_detected, 'first')
        assert entered.wait(1)
        other = pool.submit(second)
        assert second_started.wait(1)
        try:
            assert not overlap.wait(.05)
        finally:
            release.set()
        assert first.result() == other.result() == ('pt', 1.0)
