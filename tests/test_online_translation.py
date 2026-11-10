import pytest

from ra import online_translation as online


def test_public_translation_checks_response_and_never_uses_credentials(monkeypatch):
    import json
    from urllib.parse import parse_qs, urlsplit
    payload = [[["Юғары етәкселек", "Top management"]]]
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, maximum): return json.dumps(payload).encode()
    def fetch(request, *, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        assert query == {"client": ["gtx"], "sl": ["en"], "tl": ["ba"], "dt": ["t"], "q": ["Top management"]}
        assert not request.has_header("Authorization")
        return Response()
    monkeypatch.setattr(online, "urlopen", fetch)
    online.translate_public.cache_clear()
    assert online.translate_public("Top management", "en", "ba") == "Юғары етәкселек"
    online.translate_public.cache_clear()
    payload[0][0][1] = "Different source"
    with pytest.raises(online.ProviderError):
        online.translate_public("Top management", "en", "ba")
    online.translate_public.cache_clear()


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
