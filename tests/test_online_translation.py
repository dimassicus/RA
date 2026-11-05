import pytest

from ra import online_translation as online


def test_requires_matching_source_and_target_even_when_provider_offers_fuzzy_result(monkeypatch):
    match = {"segment": "different debt", "translation": "wrong amount", "target": "en-GB"}
    response = {"responseStatus": 200, "matches": [match]}
    monkeypatch.setattr(online, "fetch_json", lambda url: response)
    online.translate.cache_clear()
    with pytest.raises(online.ProviderError):
        online.translate("actual debt", "uz", "en")
    match.update(segment="actual debt", target="ru")
    with pytest.raises(online.ProviderError):
        online.translate("actual debt", "uz", "en")
    match.update(target="en-GB", translation="Tom &amp; Co repaid")
    assert online.translate("actual debt", "uz", "en") == "Tom & Co repaid"
    response["quotaFinished"] = True
    with pytest.raises(online.ProviderError):
        online.translate("another debt", "uz", "en")
    with pytest.raises(online.ProviderError):
        online.translate("я" * 251, "ru", "en")
