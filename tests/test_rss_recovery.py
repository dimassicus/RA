from datetime import timedelta
from email.utils import formatdate
from urllib.error import HTTPError

import pytest

from ra import db, exposure, rss_gate, rss_recovery, search, worker
from ra.reports import make_xlsx
from test_score_store import database, seed


def test_outage_only_probes_once_and_extends_cooldown(monkeypatch):
    gate = rss_gate.GoogleRSS()
    now = [10000.]
    calls = []
    outcomes = iter([503, 503, 503, 503, 200, 200])
    monkeypatch.setattr(rss_gate.time, 'time', lambda: now[0])
    monkeypatch.setattr(rss_gate.time, 'sleep', lambda _: None)

    def fetch(url):
        calls.append(url)
        if next(outcomes) == 503:
            raise HTTPError(url, 503, 'Unavailable', {}, None)
        return b'<rss><channel/></rss>'

    for i in range(3):
        with pytest.raises(rss_gate.Deferred):
            gate.read(fetch, f'query{i}', 'issuer')
    with pytest.raises(rss_gate.Deferred):
        gate.read(fetch, 'another-company', 'issuer')
    assert len(calls) == 3 and gate.until == now[0] + 300
    now[0] = gate.until
    with pytest.raises(rss_gate.Deferred):
        gate.read(fetch, 'query3', 'issuer')
    assert calls[-1] == 'issuer' and gate.until == now[0] + 600
    now[0] = gate.until
    assert gate.read(fetch, 'missed', 'issuer').startswith(b'<rss>')
    assert calls[-2:] == ['issuer', 'missed']
    assert gate.until == 0


@pytest.mark.parametrize('retry_after', ['900', formatdate(10900, usegmt=True)])
def test_retry_after_pauses_on_first_failure(monkeypatch, retry_after):
    monkeypatch.setattr(rss_gate.time, 'time', lambda: 10000.)
    gate = rss_gate.GoogleRSS()
    def fetch(url):
        raise HTTPError(url, 503, '', {'Retry-After': retry_after}, None)
    with pytest.raises(rss_gate.Deferred):
        gate.read(fetch, 'failed', 'probe')
    assert gate.until == 10900.


def test_replay_preserves_original_run_and_reviews(database, monkeypatch):
    company, rid, existing = seed(database)
    db.set_report_selection(rid,[existing['id']],scores={existing['id']: -1})
    run = db.get_run(rid)
    key = str(company['id'])
    span = rss_recovery.bounds(run)
    for q in ('first','second'):
        rss_recovery.record(database,rid,key,q,'Brazil',span)
    monkeypatch.setattr(worker, 'translate_text', lambda title,*args: (title,'English'))
    monkeypatch.setattr(rss_recovery.recovery_providers, 'configured', lambda _: True)
    monkeypatch.setattr(rss_recovery.news_apis, 'provider_queries', lambda *args: ['"Acme Manufacturing"'])
    rows = [dict(title='Acme Manufacturing filing',published_at=existing['published_at'],
                 publisher='Issuer',source_kind='news',signal_terms=[],language='en',query='first',
                 discovery_service='Brave Search',url=existing['url']+'?utm_medium=feed')]
    rows.append(dict(rows[0], url='https://example.test/new'))
    rows.append(dict(rows[0], url='https://example.test/old',published_at=span[0]-timedelta(days=2)))
    paid = []
    def brave(query, window, subject, *args, language):
        paid.append((query,window,language))
        return rows
    monkeypatch.setattr(rss_recovery.monthly_backfill,'brave_search',brave)
    monkeypatch.setattr(rss_recovery,'fetch_job',lambda *args: rows)
    database.execute("UPDATE search_runs SET status='running' WHERE id=%s",(rid,))
    assert rss_recovery.recover_one()
    assert not rss_recovery.recover_one()
    assert len(db.get_results(rid)) == 1
    database.execute("UPDATE search_runs SET status='partial' WHERE id=%s",(rid,))
    assert rss_recovery.recover_one()
    assert rss_recovery.recover_one()
    assert rss_recovery.recover_one()
    assert not rss_recovery.recover_one()
    assert paid == [('"Acme Manufacturing"',span,'pt')]
    saved = db.get_results(rid)
    assert len(saved) == 2 and db.get_run(rid)['result_count'] == 2
    original = next(r for r in saved if r['id']==existing['id'])
    assert original['report_selected'] and original['score_status']=='reviewed' and original['score']==-1
    assert not next(r for r in saved if r['id']!=existing['id'])['report_selected']
    assert database.execute('SELECT count(*) AS n FROM verification_jobs').fetchone()['n'] == 1
    assert '2 deferred searches, 2 recovered, 0 awaiting retry' in db.get_errors(rid)[0]['message']


def test_legacy_import_uses_original_editions_and_flags_ambiguity(database, monkeypatch):
    company,rid,_ = seed(database)
    span = rss_recovery.bounds(db.get_run(rid))
    monkeypatch.setattr(search,'planned_feeds',lambda *_: [(q,e,span) for q in ('both','one') for e in ('','Brazil')])
    for q in ('both','both','one'):
        database.execute('INSERT INTO search_errors(run_id,subject_name,message) VALUES (%s,%s,%s)',
                         (rid,company['name'],q+rss_recovery.LEGACY_SUFFIX))
    assert rss_recovery.import_legacy(rid)==3 and rss_recovery.import_legacy(rid)==0
    jobs = database.execute('SELECT request,status FROM rss_recovery ORDER BY id').fetchall()
    assert [(r['request']['edition'],r['status']) for r in jobs]==[('','pending'),('Brazil','pending'),(None,'unknown_edition')]
    assert len(db.get_errors(rid))==1 and db.get_run(rid)['error_count']==1


def test_shared_outage_keeps_context_request_for_each_affected_company(monkeypatch):
    captured = []
    def down(_):
        raise rss_gate.Deferred('cooldown')
    assert exposure.search_topic('country','brazil',down,edition='brazil',factor=('Risk','"risco"'),
                                 on_deferred=lambda *args:captured.append(args))==[]
    query, edition, _ = captured[0]
    assert 'risco' in query and edition=='brazil'


def test_company_search_defers_only_failed_google_queries_and_keeps_other_sources(database, monkeypatch):
    from ra import disclosures, idx_filings, news_apis
    company, rid, _ = seed(database)
    run = db.get_run(rid)
    span = rss_recovery.bounds(run)
    subject = run['subjects'][0]
    monkeypatch.setattr(rss_gate, 'google', rss_gate.GoogleRSS())
    queries = ['good', 'fail1', 'fail2', 'fail3', 'deferred']
    monkeypatch.setattr(search, 'planned_feeds', lambda *_: [(q,'Brazil',span) for q in queries])
    sent = []
    def fetch(url):
        sent.append(url)
        if len(sent) == 1:
            return b'<rss><channel/></rss>'
        raise HTTPError(url,503,'Unavailable',{},None)
    monkeypatch.setattr(search,'_fetch_feed',fetch)
    monkeypatch.setattr(news_apis,'configured_providers',lambda: ['gnews','thenewsapi'])
    monkeypatch.setattr(news_apis,'search',lambda provider,*a,**kw:[dict(url='https://example.test/'+provider)])
    monkeypatch.setattr(disclosures,'collect',lambda *a,**kw: ([dict(url='https://example.test/issuer')],[]))
    monkeypatch.setattr(idx_filings,'collect',lambda *a,**kw:([],[]))
    rows,errors = search.search_company(subject,window=span,on_deferred=lambda *args:rss_recovery.defer(run,[subject],*args))
    assert len(sent)==4 and not errors
    assert {r['url'].split('/')[-1] for r in rows}=={'issuer','gnews','thenewsapi'}
    pending = database.execute('SELECT request FROM rss_recovery ORDER BY id').fetchall()
    assert [r['request']['query'] for r in pending]==queries[1:]
    assert all('after%3A' in r['request']['url'] and r['request']['edition']=='Brazil' for r in pending)


def test_shared_topic_recovery_fetches_once_and_attributes_each_company(database, monkeypatch):
    from email.utils import format_datetime
    companies = database.execute("""INSERT INTO companies(name,country_registration)
        VALUES ('First Issuer','Brazil'),('Second Issuer','Brazil') RETURNING *""").fetchall()
    subjects = [db.company_snapshot(company) for company in companies]
    rid = db.create_run('all', subjects)
    database.execute("UPDATE search_runs SET status='partial' WHERE id=%s",(rid,))
    database.execute("INSERT INTO exposure_checks(run_id,branch,topic) VALUES (%s,'country','brazil')",(rid,))
    run = db.get_run(rid)
    span = rss_recovery.bounds(run)
    ctx = dict(branch='country',topic='brazil',factor=['Trade restrictions','"tariffs"'])
    rss_recovery.defer(run,subjects,'Brazil tariffs','',span,ctx)
    calls = []
    def fetch(url, *, probe):
        calls.append(url)
        date = format_datetime(span[1]-timedelta(hours=1))
        return f'<rss><channel><item><title>Brazil announces tariffs</title><pubDate>{date}</pubDate><source>News</source><link>https://example.test/tariffs</link></item></channel></rss>'.encode()
    monkeypatch.setattr(search,'fetch_feed',fetch)
    monkeypatch.setattr(rss_recovery.recovery_providers,'configured',lambda _: False)
    monkeypatch.setattr(worker,'translate_text',lambda title,*args:(title,'English'))
    assert rss_recovery.recover_one() and not rss_recovery.recover_one()
    assert len(calls)==1
    rows=db.get_results(rid)
    assert {r['company_id'] for r in rows}=={c['id'] for c in companies}
    assert all('Trade restrictions' in r['exposure_paths'][0] for r in rows)
    assert db.exposure_coverage(rid)[0]['leads']==1
    assert db.get_run(rid)['error_count']==0
    assert make_xlsx(db.get_run(rid), rows, db.get_errors(rid)).getbuffer().nbytes > 0


def test_exhausted_feed_timeout_is_deferred_and_shares_outage_counter(monkeypatch):
    gate = rss_gate.GoogleRSS()
    def fetch(url):
        raise TimeoutError('read timed out')
    for i in range(3):
        with pytest.raises(rss_gate.FeedTimeout):
            gate.read(fetch, str(i), 'issuer')
    assert gate.until > 0
    with pytest.raises(rss_gate.Deferred):
        gate.read(lambda _: pytest.fail('unexpected request'), 'fourth', 'issuer')


def test_closed_history_cannot_requeue_work_but_new_runs_can(database, monkeypatch):
    from ra import score_store, translation, verification_store
    from ra.app import create_app
    company, rid, article = seed(database)
    database.execute("UPDATE search_runs SET search_settings=search_settings || %s,event_analysis_status='complete' WHERE id=%s",
                     (db.Jsonb({'processing_stopped_at': '2026-11-12T18:00:00+03:00'}), rid))
    database.execute("UPDATE search_results SET language='ru' WHERE id=%s", (article['id'],))
    run = db.get_run(rid)
    rss_recovery.record(database, rid, str(company['id']), 'original search', 'Brazil', rss_recovery.bounds(run))
    monkeypatch.setattr(rss_recovery.recovery_providers, 'configured', lambda _: True)
    assert not rss_recovery.recover_one()
    assert '1 stopped by operator' in db.get_errors(rid)[0]['message']
    monkeypatch.setattr(db, 'init_schema', lambda: None)
    assert translation.backfill() == (0, 0)
    with pytest.raises(ValueError, match='closed'):
        verification_store.enqueue_run(rid)
    score_store.review_score(article['id'], -1, True, 'Analyst review after closure')
    assert db.get_run(rid)['event_analysis_status'] == 'complete'
    client = create_app().test_client()
    with client.session_transaction() as session:
        session['csrf_token'] = 'test'
    for stage in ('translations', 'sources', 'search'):
        assert client.post(f'/setup/runs/{rid}/retry', data={'csrf_token':'test', 'stage':stage}).status_code == 409
    assert 'Run processing stopped by the operator.' in client.get(f'/runs/{rid}').text
    newer = db.create_run('selected', [db.company_snapshot(company)])
    assert db.get_run(newer)['status'] == 'queued' and 'processing_stopped_at' not in db.get_run(newer)['search_settings']
