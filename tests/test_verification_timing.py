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
    from ra import article_translation, headline_review
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
                         (headline_review, 'gemma_translation')]:
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
    from ra import article_translation, headline_review
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
    for module, name in [(headline_review, 'audit'), (headline_review, 'fallback'), (headline_review, 'gemma_translation')]:
        monkeypatch.setattr(module, name, lambda *args, **kwargs: pytest.fail("Unexpected model call"))
    prepare = article_translation.prepare
    def checked_prepare(source, subject):
        assert ('translation' in source) == (changed is None)
        return prepare(source, subject)
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


@pytest.mark.parametrize("per_document", [True, False])
def test_source_budget_and_drafts_use_run_policy(database, monkeypatch, per_document):
    from ra import article_translation, event_analysis
    company, run_id, article = seed(database)
    if not per_document:
        database.execute("UPDATE search_runs SET per_document_limit=false WHERE id=%s",(run_id,))
    title = company['name']
    body = 'External market news. ' * 500 + title + ' has defaulted on bond payments. ' + 'Further detail. ' * 200
    url = article['url']
    parent = {'status': 'available', 'language': 'en', 'body': 'Short cover.', 'sha256': 'wrapper',
              'final_url': url, 'linked_documents': [dict(status='available', language='en', body=body,
                                                          sha256='full-pdf', final_url=url + '/pdf')]}
    lengths = []
    def prepare(doc, subject):
        lengths.append(len(doc['body']))
        yield from ()
        return doc
    monkeypatch.setattr(article_translation, 'prepare_steps', prepare)
    monkeypatch.setattr(verification_store.risk_factors, 'records', lambda: [])
    check = verification_store.verify_article(article['id'], fetcher=lambda _: parent)
    expected = 5000 + (len('Short cover.') if per_document else 0)
    assert sum(lengths) == expected
    assert check['review_scope']['selected_length'] == expected
    assert check['review_scope']['original_length'] == len(body) + len('Short cover.')
    assert check['review_scope'].get('per_document_limit') == (5000 if per_document else None)
    selected = check['review_document']['linked_documents'][0]
    assert title in selected['body'] and selected['excerpt']['start'] > 0
    assert selected['body'] == body[selected['excerpt']['start']:][:len(selected['body'])]
    assert database.execute("SELECT length(document->'linked_documents'->0->>'body') AS n FROM article_documents WHERE url=%s",
                            (url,)).fetchone()['n'] == len(body)
    evidence = event_analysis.source_texts(parent, check, company)
    assert sum(len(s['text']) for s in evidence) == expected
    assert 'excerpt' in evidence[1]['kind']


def test_queue_batches_primary_sources_and_attachments_across_leads(database, monkeypatch):
    from itertools import groupby
    from ra import article_translation, headline_review, local_models
    issuer, run, article = seed(database)
    for i in range(6):
        database.execute("""INSERT INTO search_results (run_id,company_id,subject_key,subject_name,title,url,query,language,published_at)
            SELECT run_id,company_id,subject_key,subject_name,title,url||%s,query,language,published_at FROM search_results WHERE id=%s""",
            (str(i),article['id']))
    events=[]
    def retrieve(url,hint):
        def source(u,body):
            return dict(status='available', final_url=u, body=body, sha256=article_translation.fingerprint(body), language='id')
        return dict(source(url, 'first '+url),linked_documents=[source(url+'/pdf','replacement '+url)])
    monkeypatch.setattr(verification_store,'retrieve',retrieve)
    monkeypatch.setattr(headline_review,'readiness',lambda:{'ready':True})
    def generate(body,*args,**kwargs):
        events.append('Gemma')
        text = 'reject' if body.startswith('replacement') else 'Acme Manufacturing supplied a statement.'
        return text, local_models.GEMMA
    monkeypatch.setattr(headline_review,'gemma_translation',generate)
    def fallback(body,*args,**kwargs):
        events.append('Google')
        return 'Acme Manufacturing supplied a statement.', 'Google public translation'
    def audit(src,lang,en,**kwargs):
        events.append('Qwen')
        return dict(model=local_models.QWEN,reasoning='off',issues=[],status='material_error' if en=='reject' else 'faithful')
    monkeypatch.setattr(headline_review,'fallback',fallback)
    monkeypatch.setattr(headline_review,'audit',audit)
    monkeypatch.setattr(verification_store.risk_factors,'records',lambda:[])
    assert verification_store.enqueue_run(run,limit_per_company=None)==7
    verification_store.process_jobs(run)
    assert [name for name,_ in groupby(e for e in events if e != 'Google')]==['Gemma','Qwen']
    assert events.count('Google') == 7
    assert verification_store.job_summary(run)['completed']==7
    assert not database.execute("SELECT 1 FROM verification_jobs WHERE translation_checkpoint<>'{}'").fetchone()


def test_repeat_watchlist_rechecks_content_and_reuses_unchanged_work(database, monkeypatch):
    from ra import db, worker
    from ra.article_content import source_document

    company, _, seed_article = seed(database)
    subject = db.company_snapshot(company)
    title = company['name'] + ' reports losses'
    lead = dict(title=title, publisher='Test', discovery_service='Test', language='en',
                source_kind='news', published_at=seed_article['published_at'], query='issuer',
                signal_terms=[], url='https://example.test/repeat')
    body = [title + ' for the period.']
    attachments = []
    calls = {'translate': [], 'fetch': [], 'verify': []}
    verify = verification_store.verify
    def fetch(url, hint):
        calls['fetch'].append(url)
        final_url = lead['url'] if url.endswith('/redirect') else url
        return dict(source_document(body[0], 'HTML article/main text', final_url),
                    language='en', linked_documents=list(attachments))
    def check(doc, target, article):
        calls['verify'].append(article['id'])
        return verify(doc, target, article)
    def translate(title, *args):
        calls['translate'].append(title)
        return title, 'English'
    monkeypatch.setattr(worker, 'translate_text', translate)
    monkeypatch.setattr(worker, 'detect_language', lambda *_:'en')
    monkeypatch.setattr(verification_store,'retrieve',fetch)
    monkeypatch.setattr(verification_store,'verify',check)
    monkeypatch.setattr(verification_store.risk_factors,'records',lambda: [])
    def collect(articles):
        run = db.create_run('all',[subject])
        worker.process_run(run, searcher=lambda s:(articles,[]), topic_searcher=lambda *a,**k:[],
                           roster_check=lambda *a:None)
        verification_store.process_jobs(run)
        return db.get_results(run)
    subject['primary_industries'] = 'Agriculture, food processing'
    first, = collect([lead])
    assert not first['seen_before']
    # Existing runs store the publisher hash even if they predate the full-input fingerprint.
    database.execute("UPDATE search_results SET verification=verification-'source_key' WHERE id=%s", (first['id'],))
    subject['primary_industries'] = 'Agriculture; food processing'
    repeated = collect([dict(lead, query='other query'), dict(lead, title=title+' later', url=lead['url']+'/missed')])
    same = next(r for r in repeated if r['url']==lead['url'])
    assert same['verification']['reused_from']==first['id']
    assert len(calls['translate'])==len(calls['verify'])==2
    assert len(calls['fetch'])==3
    body[0] += ' The issuer has since filed for bankruptcy.'
    changed, = collect([lead])
    assert 'reused_from' not in changed['verification']
    assert changed['verification']['document_hash']!=first['verification']['document_hash']
    assert len(calls['translate'])==2 and len(calls['verify'])==3
    renamed, = collect([dict(lead,title=title+' update')])
    assert 'reused_from' not in renamed['verification']
    assert len(calls['translate'])==3 and len(calls['verify'])==4

    redirected, = collect([dict(lead, title=renamed['title'], url=lead['url']+'/redirect')])
    assert redirected['verification']['reused_from'] == renamed['id']
    assert len(calls['translate']) == 3
    attachments.append(dict(source_document('A new issuer filing.', 'PDF text', lead['url']+'/filing'), language='en'))
    updated, = collect([dict(lead, title=renamed['title'])])
    assert 'reused_from' not in updated['verification']
    assert len(updated['verification']['sources']) == 2

    # A newer lead can have different attachments. Use the older complete match
    # when the publisher returns the original set, even across an RSS redirect.
    attachments.clear()
    restored, = collect([dict(lead, title=renamed['title'])])
    assert restored['verification']['reused_from'] == redirected['id']
    before = len(calls['verify'])
    subject['primary_industries'] = 'Banking'
    different_profile, = collect([dict(lead, title=renamed['title'])])
    assert 'reused_from' not in different_profile['verification']
    assert len(calls['verify']) == before + 1


def test_prefetch_refreshes_each_runs_evidence_and_verification_consumes_it(database, monkeypatch):
    from ra import db
    _, run, article = seed(database)
    monkeypatch.setattr(verification_store.risk_factors, "records", lambda: [])
    calls = []
    def fetch(url, hint):
        calls.append(url)
        return dict(status='available',language='en',body='Issuer filing.',sha256='abc',final_url=url,method='HTML')
    monkeypatch.setattr(verification_store,'retrieve',fetch)
    verification_store.verify_article(article['id'], fetcher=fetch)
    _, other, later = seed(database)
    later = database.execute('UPDATE search_results SET published_at=%s WHERE id=%s RETURNING *',
                             (article['published_at'],later['id'])).fetchone()
    verification_store._retrieve_document(later, later['url'], fetch, prefetch=True)
    assert len(calls)==2
    result = verification_store.verify_article(later['id'],fetcher=fetch)
    assert result['reused_from']==article['id'] and len(calls)==2
