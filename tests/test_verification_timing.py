"""Source timing uses disposable PostgreSQL state; never calls external services."""
import json
import logging
from types import SimpleNamespace

import pytest

from ra import verification_store
from test_score_store import database, seed


def timing_records(caplog):
    return [json.loads(record.getMessage().removeprefix("source_timing "))
            for record in caplog.records if record.name == verification_store.__name__]


def test_source_timings_cover_stages_without_changing_assessment(database, monkeypatch, caplog):
    _, _, article = seed(database)
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [])
    monkeypatch.setattr(verification_store, "time", SimpleNamespace(perf_counter=iter(range(12)).__next__))
    caplog.set_level(logging.INFO, logger=verification_store.__name__)
    body = "Acme Manufacturing has filed for bankruptcy protection after missing bond payments."
    result = verification_store.verify_article(article['id'], fetcher=lambda url: {
        "status": "available", "language": "en", "body": body, "sha256": "test-body",
        "final_url": url, "method": "test",
    })
    before = database.execute("SELECT score_input_hash FROM search_results WHERE id=%s", (article['id'],)).fetchone()
    history = database.execute("SELECT count(*) AS n FROM article_verification_history").fetchone()
    repeated = verification_store.verify_article(article['id'], fetcher=lambda url: pytest.fail("Expected cached source"))
    assert result == repeated
    assert database.execute("SELECT score_input_hash FROM search_results WHERE id=%s", (article['id'],)).fetchone() == before
    assert database.execute("SELECT count(*) AS n FROM article_verification_history").fetchone() == history
    first, cached = timing_records(caplog)
    assert first == {
        "result_id": article['id'], "cache_hit": False, "completed": True, "failed_stage": None,
        "seconds": {stage: 1.0 for stage in ("retrieval_cache", "translation_review", "attribution", "themes", "persistence")},
        "total_seconds": 5.0,
    }
    assert cached == {**first, "cache_hit": True}
    assert body not in caplog.text and article['url'] not in caplog.text
    assert "seconds" not in result and "total_seconds" not in result


def test_failed_source_stage_is_timed_without_logging_exception_content(database, monkeypatch, caplog):
    _, _, article = seed(database)
    monkeypatch.setattr(verification_store, "time", SimpleNamespace(perf_counter=iter([10, 13]).__next__))
    caplog.set_level(logging.INFO, logger=verification_store.__name__)
    def fetcher(url):
        raise RuntimeError("secret request content")
    with pytest.raises(RuntimeError, match="secret request content"):
        verification_store.verify_article(article['id'], fetcher=fetcher)
    assert timing_records(caplog) == [{
        "result_id": article['id'], "cache_hit": False, "completed": False, "failed_stage": "retrieval_cache",
        "seconds": {"retrieval_cache": 3.0}, "total_seconds": 3.0,
    }]
    assert "secret request content" not in caplog.text and article['url'] not in caplog.text


def cache_v4(database, article, **changes):
    from datetime import datetime, timedelta, timezone
    from psycopg.types.json import Jsonb
    document = {
        "status": "available", "body": "Original company financial statement.", "language": "vi",
        "sha256": "retained-body-hash", "method": "HTML article/main text", "final_url": article['url'],
        "reader_version": "public-source-v4",
        "source_hint": {"title": article['title'], "publisher": article['publisher'],
                        "published_at": article['published_at'].isoformat()},
        **changes,
    }
    fetched_at = datetime.now(timezone.utc) - timedelta(hours=1)
    database.execute("INSERT INTO article_documents (url,document,fetched_at) VALUES (%s,%s,%s)",
                     (article['url'], Jsonb(document), fetched_at))
    return document, fetched_at


@pytest.mark.parametrize('reader_version', ['public-source-v4', 'public-source-v5', 'public-source-v6'])
def test_cache_upgrade_keeps_complete_translation_and_original_age(database, monkeypatch, reader_version):
    from psycopg.types.json import Jsonb
    from ra import article_translation, headline_review, translation
    _, _, article = seed(database)
    document, fetched_at = cache_v4(database, article, reader_version=reader_version)
    part = {"source": document['body'], "english": "Acme Manufacturing reports net losses of 40 million dollars.",
            "provider": "Argos test", "review": {"status": "faithful", "issues": [], "model": headline_review.QWEN,
                                                  "reasoning": "off"}}
    document['translation'] = {"version": article_translation.VERSION, "source_hash": article_translation.fingerprint(document['body']),
                               "language": "vi", "status": "faithful", "parts": [part]}
    database.execute("UPDATE article_documents SET document=%s WHERE url=%s", (Jsonb(document), article['url']))
    detected = []
    monkeypatch.setattr(verification_store, "detect_language", lambda body: detected.append(body) or "vi")
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [])
    for module, name in [(headline_review, 'readiness'), (headline_review, 'audit'), (headline_review, 'fallback'),
                         (translation, 'argos_translate')]:
        monkeypatch.setattr(module, name, lambda *args, **kwargs: pytest.fail("Reviewed translation must be reused"))
    result = verification_store.verify_article(article['id'], fetcher=lambda url: pytest.fail("Fresh v4 source should not refetch"))
    saved = database.execute("SELECT document,fetched_at FROM article_documents WHERE url=%s", (article['url'],)).fetchone()
    assert result['status'] == 'supported'
    assert saved['fetched_at'] == fetched_at
    assert saved['document']['reader_version'] == verification_store.READER_VERSION
    assert saved['document']['body'] == document['body'] and saved['document']['sha256'] == document['sha256']
    assert saved['document']['translation']['parts'] == [part]
    assert detected == [document['body']]


@pytest.mark.parametrize('old_language', ['el', 'en'])
def test_cache_language_correction_removes_obsolete_translation_evidence(database, monkeypatch, old_language):
    _, _, article = seed(database)
    cache_v4(database, article, language=old_language, translation={'language': 'el', 'parts': []})
    monkeypatch.setattr(verification_store, 'detect_language', lambda body: 'en')
    monkeypatch.setattr(verification_store.risk_factors, 'records', lambda: [])
    verification_store.verify_article(article['id'], fetcher=lambda url: pytest.fail('Cache must be reused'))
    saved = database.execute('SELECT document FROM article_documents WHERE url=%s', (article['url'],)).fetchone()['document']
    assert saved['language'] == 'en' and 'translation' not in saved


def test_v4_corrupt_pdf_skips_body_models_but_keeps_headline_review(database, monkeypatch):
    from ra import headline_review, keywords
    _, _, article = seed(database)
    document, fetched_at = cache_v4(database, article, body="broken \ufffd" * 100, method="PDF text (no OCR)",
                                   translation={'language': 'vi', 'parts': []})
    monkeypatch.setattr(headline_review, 'readiness', lambda: pytest.fail("Corrupt body must not be translated"))
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [
        {"id": 1, "english": "Bankruptcy", "scope": "company", "enabled": True}])
    requests = []
    monkeypatch.setattr(keywords, 'model_json', lambda prompt, data, schema: requests.append(data) or {"matches": []})
    result = verification_store.verify_article(article['id'], fetcher=lambda url: pytest.fail("Validate cached PDF locally"))
    saved = database.execute("SELECT document,fetched_at FROM article_documents WHERE url=%s", (article['url'],)).fetchone()
    assert result['status'] == 'unavailable' and 'corrupt character encoding' in result['reason']
    assert saved['document']['status'] == 'unavailable' and saved['fetched_at'] == fetched_at
    assert 'translation' not in saved['document']
    assert requests and all(request['source_text'] == '' for request in requests)


@pytest.mark.parametrize("invalid_cache", ['stale', 'different_hint', 'v3'])
def test_reader_upgrade_does_not_reuse_ineligible_cache(database, monkeypatch, invalid_cache):
    from psycopg.types.json import Jsonb
    _, _, article = seed(database)
    document, _ = cache_v4(database, article)
    if invalid_cache == 'stale':
        database.execute("UPDATE article_documents SET fetched_at=now()-interval '8 days'")
    else:
        if invalid_cache == 'different_hint':
            document['source_hint']['title'] = "Different article"
        else:
            document['reader_version'] = 'public-source-v3'
        database.execute("UPDATE article_documents SET document=%s", (Jsonb(document),))
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [])
    calls = []
    result = verification_store.verify_article(article['id'], fetcher=lambda url: calls.append(url) or {
        "status": "unavailable", "reason": "Test publisher unavailable"})
    assert calls == [article['url']]
    assert result['status'] == 'unavailable'


@pytest.mark.parametrize("changed", [None, 'body', 'sha256', 'language'])
def test_refetch_carries_review_only_for_identical_source(database, monkeypatch, changed):
    from psycopg.types.json import Jsonb
    from ra import article_translation, headline_review, translation
    _, _, article = seed(database)
    document, _ = cache_v4(database, article)
    part = {"source": document['body'], "english": "Acme Manufacturing reports net losses of 40 million dollars.",
            "provider": "Argos test", "review": {"status": "faithful", "issues": [], "model": headline_review.QWEN,
                                                  "reasoning": "off"}}
    document['translation'] = {"version": article_translation.VERSION, "source_hash": article_translation.fingerprint(document['body']),
                               "language": "vi", "status": "faithful", "parts": [part]}
    database.execute("UPDATE article_documents SET document=%s,fetched_at=now()-interval '8 days'", (Jsonb(document),))
    stale_at = database.execute("SELECT fetched_at FROM article_documents").fetchone()['fetched_at']
    fresh = {key: value for key, value in document.items() if key not in ('translation', 'source_hint', 'reader_version')}
    if changed:
        fresh[changed] = 'ru' if changed == 'language' else fresh[changed] + ' changed'
    fetches, readiness = [], []
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [])
    monkeypatch.setattr(headline_review, 'readiness', lambda: readiness.append(True) or {'ready': False, 'message': 'Offline test'})
    for module, name in [(headline_review, 'audit'), (headline_review, 'fallback'), (translation, 'argos_translate')]:
        monkeypatch.setattr(module, name, lambda *args, **kwargs: pytest.fail("Unexpected model call"))
    prepare = article_translation.prepare
    def checked_prepare(source):
        assert ('translation' in source) == (changed is None)
        return prepare(source)
    monkeypatch.setattr(article_translation, 'prepare', checked_prepare)
    verification_store.verify_article(article['id'], fetcher=lambda url: fetches.append(url) or fresh)
    saved = database.execute("SELECT document,fetched_at FROM article_documents").fetchone()
    assert fetches == [article['url']] and saved['fetched_at'] > stale_at
    assert readiness == ([] if changed is None else [True])
    if changed is None:
        assert saved['document']['translation']['parts'] == [part]


def test_kap_v6_navigation_cache_is_refetched(database, monkeypatch):
    _, _, article = seed(database, url='https://www.kap.org.tr/tr/Bildirim/1693892')
    cache_v4(database, article, reader_version='public-source-v6')
    monkeypatch.setattr(verification_store.risk_factors, 'records', lambda: [])
    calls = []
    verification_store.verify_article(article['id'], fetcher=lambda url: calls.append(url) or {
        'status': 'unavailable', 'reason': 'Test filing unavailable'})
    assert calls == [article['url']]
