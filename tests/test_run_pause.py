"""Exercise run controls with isolated PostgreSQL data and two local feed queries."""
from datetime import timezone
from email.utils import format_datetime
from functools import partial

import pytest

from ra import db, disclosures, search, worker, verification_store
from ra.app import create_app
from test_score_store import database, seed


@pytest.mark.parametrize('scope,language', [('all', 'en'), ('selected', 'ru')])
def test_pause_between_queries_and_resume_same_leads(database, monkeypatch, scope, language):
    company, _, _ = seed(database)
    rid = db.create_run(scope, [db.company_snapshot(company)])
    path = f'/runs/{rid}'
    client = create_app().test_client()
    client.set_cookie('ra_language', language)
    page = client.get(path)
    pause, resume = ('Pause', 'Resume') if language == 'en' else ('Приостановить', 'Продолжить')
    header = page.get_data(as_text=True).split('class="hero run-hero">')[1].split('</section>')[0]
    assert f'value="pause">{pause}</button>' in header
    with client.session_transaction() as session:
        token = session['csrf_token']
    def submit(action):
        return client.post(path + '/control', data={'action': action, 'csrf_token': token})
    assert client.post(path + '/control', data={'action': 'pause'}).status_code == 400
    queue = []
    monkeypatch.setattr(worker, 'enqueue', queue.append)
    monkeypatch.setattr(worker, '_suspended', {})
    monkeypatch.setattr(worker, 'plan_subjects', lambda subjects, _: subjects)
    monkeypatch.setattr(search, 'planned_feeds', lambda subject, window, focus: [('first', '', window), ('second', '', window)])
    monkeypatch.setattr(disclosures, 'collect', lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(search.news_apis, 'configured_providers', lambda: [])
    monkeypatch.setattr(search, 'has_uk_presence', lambda _: False)
    database.execute("""UPDATE search_runs SET search_settings=search_settings ||
        '{"use_perplexity":false,"use_google":false}'::jsonb WHERE id=%s""", (rid,))
    calls = []
    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            assert submit('pause').status_code == 303
        # Use the app's fixed window; this article existed when the search started.
        stamp = format_datetime(db.get_run(rid)['created_at'].astimezone(timezone.utc), usegmt=True)
        return (f'<rss><channel><item><title>Acme Manufacturing filing {len(calls)}</title>'
                f'<link>https://example.test/{len(calls)}</link><pubDate>{stamp}</pubDate>'
                '</item></channel></rss>').encode()
    monkeypatch.setattr(worker, 'search_company_steps', partial(search.search_company_steps, fetcher=fetch))
    def work():
        worker.process_run(rid, topic_searcher=lambda *a, **kw: [], roster_check=lambda *a: None)
    work()
    assert db.run_paused(rid) and len(calls) == 1
    assert f'value="resume">{resume}</button>' in client.get(path).get_data(as_text=True)
    assert client.get(f'/api/runs/{rid}/status').json['paused']
    work()  # A stale queue entry must not advance the suspended iterator.
    assert len(calls) == 1
    assert submit('resume').status_code == submit('resume').status_code == 303
    assert queue == [rid]
    work()
    assert len(set(calls)) == len(calls) == 2
    run = db.get_run(rid)
    assert run['completed_companies'] == 1 and run['result_count'] == 2
    assert {row['url'] for row in db.get_results(rid)} == {'https://example.test/1', 'https://example.test/2'}


def test_paused_source_checks_stay_queued_and_finished_runs_reject_pause(database, monkeypatch):
    _, rid, lead = seed(database)
    database.execute("UPDATE search_runs SET event_analysis_status='complete' WHERE id=%s", (rid,))
    database.execute('INSERT INTO verification_jobs(result_id) VALUES (%s)', (lead['id'],))
    db.set_run_paused(rid, True)
    verification_store.process_jobs(rid)
    assert verification_store.next_run() is None
    assert verification_store.job_summary(rid)['queued'] == 1
    db.set_run_paused(rid, False)
    assert verification_store.next_run() == rid
    retrieved = []
    def fetch(url, hint):
        retrieved.append(url)
        db.set_run_paused(rid, True)
        return dict(status='unavailable', final_url=url, reason='Offline test')
    monkeypatch.setattr(verification_store, 'retrieve', fetch)
    verification_store.process_jobs(rid)
    assert verification_store.job_summary(rid)['queued'] == 1
    assert db.run_paused(rid)
    db.set_run_paused(rid, False)
    verification_store.process_jobs(rid)
    assert verification_store.job_summary(rid)['completed'] == 1
    assert retrieved == [lead['url']]
    with pytest.raises(ValueError):
        db.set_run_paused(rid, True)
    assert 'id="run-control"' not in create_app().test_client().get(f'/runs/{rid}').get_data(as_text=True)
