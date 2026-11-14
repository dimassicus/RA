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



def test_google_backup_checks_every_chunk(monkeypatch):
    calls = []
    def translate(text, source, target):
        calls.append(text)
        if "unavailable" in text:
            raise online.ProviderError("offline")
        return text.upper()
    monkeypatch.setattr(online, "translate_public", translate)
    text = "source words. " * 70
    assert online.google_passage(text, "pt", "en") == text.strip().upper()
    assert len(calls) > 1 and all(len(chunk) <= 500 for chunk in calls)
    with pytest.raises(online.ProviderError):
        online.google_passage(text + "unavailable", "pt", "en")
