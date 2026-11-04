"""Real PostgreSQL checks in a disposable schema; all changes roll back.

Opt in with RA_TEST_DATABASE_URL. No news APIs or translation downloads are used.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from ra import db, score_store, worker
from ra.app import create_app
from ra.scoring import week_start


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("RA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set RA_TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    conn = psycopg.connect(url, row_factory=dict_row)
    schema = "ra_scoring_test_" + uuid4().hex
    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(Path("ra/schema.sql").read_text())
        @contextmanager
        def borrowed():
            yield conn
        monkeypatch.setattr(db, "connect", borrowed)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def seed(conn, title="Acme Manufacturing files for bankruptcy", status="completed", url="https://example.test/article", published=True):
    company = conn.execute("SELECT * FROM companies LIMIT 1").fetchone()
    if not company:
        company = conn.execute("INSERT INTO companies (name,country_registration) VALUES ('Acme Manufacturing','Brazil') RETURNING *").fetchone()
    run_id = db.create_run("selected", [db.company_snapshot(company)])
    conn.execute("UPDATE search_runs SET status=%s WHERE id=%s", (status, run_id))
    row = conn.execute("INSERT INTO search_results (run_id,company_id,subject_key,subject_name,title,url,published_at,query,language) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,'test','en') RETURNING *",
                       (run_id, company["id"], str(company["id"]), company["name"], title, url,
                        datetime.now(timezone.utc)-timedelta(hours=1) if published else None)).fetchone()
    return company, run_id, row


def test_backfill_is_idempotent_and_saves_no_data_weeks(database):
    company, run_id, article = seed(database)
    empty = database.execute("INSERT INTO companies (name,country_registration) VALUES ('Empty Ltd','Brazil') RETURNING id").fetchone()
    first = score_store.refresh_scores()
    assert first["articles_updated"] == 1
    assert score_store.get_article(article["id"])["score"] == -3
    rows = score_store.weekly_history(company["id"])
    assert next(r for r in rows if r["week_start"] == week_start(article["published_at"]))["score"] is None
    assert score_store.weekly_history(empty["id"])[0]["score"] is None
    assert score_store.refresh_scores() == {"articles_updated": 0, "weeks_updated": 0}


def test_review_and_repeat_discovery_preserve_decision_and_weekly_audit(database):
    company, run_id, article = seed(database)
    seed(database)  # Same URL discovered in another run.
    score_store.refresh_scores()
    start = week_start(article["published_at"])
    before, _, history = score_store.week_detail(company["id"], start)
    assert before["metrics"]["article_count"] == 0
    score_store.review_score(article["id"], 2, True, "Source confirms completed recovery, headline was misleading")
    after, _, revisions = score_store.week_detail(company["id"], start)
    assert after["score"] == 2
    assert len(revisions) == len(history) + 1
    assert revisions[-1]["snapshot"]["score"] is None
    assert len(score_store.article_history(article["id"])) == 1
    assert score_store.refresh_scores()["articles_updated"] == 0
    assert score_store.get_article(article["id"])["score"] == 2
    score_store.review_score(article["id"], 0, False, "This article concerns another legal entity")
    assert score_store.week_detail(company["id"], start)[0]["score"] is None


def test_translation_change_reassesses_unreviewed_rows(database):
    _, _, article = seed(database, title="Acme Manufacturing company update")
    score_store.refresh_scores()
    assert score_store.get_article(article["id"])["score"] == 0
    database.execute("UPDATE search_results SET title_en='Acme Manufacturing rating upgraded' WHERE id=%s", (article["id"],))
    assert score_store.refresh_scores()["articles_updated"] == 1
    assert score_store.get_article(article["id"])["score"] == 2
    assert len(score_store.article_history(article["id"])) == 1


def test_review_validation_and_database_constraint(database):
    _, _, article = seed(database, published=False)
    score_store.refresh_scores()
    with pytest.raises(ValueError, match="publication date"):
        score_store.review_score(article["id"], 1, True, "Valid reasoning")
    for score in (-4, 4, 1.5, True):
        with pytest.raises(ValueError, match="whole number"):
            score_store.review_score(article["id"], score, False, "Valid reasoning")
    with pytest.raises(ValueError, match="Explain"):
        score_store.review_score(article["id"], 0, False, "")
    with database.transaction():
        with pytest.raises(psycopg.errors.CheckViolation):
            with database.transaction():
                database.execute("UPDATE search_results SET score=4 WHERE id=%s", (article["id"],))


def test_worker_scores_new_articles_without_network(database, monkeypatch):
    company, run_id, _ = seed(database, status="queued")
    monkeypatch.setattr(worker, "plan_subjects", lambda subjects, run_id: subjects)
    monkeypatch.setattr(worker, "translate_text", lambda title, *args: (title, "original English"))
    monkeypatch.setattr(worker, "detect_language", lambda *args: "en")
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    def searcher(subject):
        return [{"title": "Acme Manufacturing returns to profit", "language": "en", "publisher": "Test",
                 "url": "https://example.test/worker", "published_at": datetime.now(timezone.utc)-timedelta(hours=1),
                 "query": "test", "signal_terms": []}], []
    worker.process_run(run_id, searcher=searcher)
    assert db.get_run(run_id)["status"] == "completed"
    assert db.get_results(run_id)[0]["score"] == 2
    assert all(r["score"] is None for r in score_store.weekly_history(company["id"]))
    assert database.execute("SELECT count(*) AS n FROM verification_jobs").fetchone()["n"] == 1


def test_scoring_pages_api_and_csrf_protected_correction(database):
    company, run_id, article = seed(database)
    score_store.refresh_scores()
    with create_app().test_client() as client:
        assert client.get('/scores').status_code == 200
        assert client.get('/scores?weeks=bad').status_code == 400
        response = client.get(f'/runs/{run_id}?view=review')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'score--3' in body
        endpoint = f"/articles/{article['id']}/score"
        assert client.get(endpoint).status_code == 200
        assert client.post(endpoint, data={"score": "2", "reason": "Reviewed the source"}).status_code == 400
        with client.session_transaction() as session:
            token = session['csrf_token']
        response = client.post(endpoint, data={"score": "2", "included": "yes", "reason": "Reviewed the completed recovery", "csrf_token": token})
        assert response.status_code == 302
        data = client.get(f"/api/scores/weekly?company_id={company['id']}").get_json()
        assert data['timezone'] == 'Europe/Moscow'
        assert any(row['score'] == 2 for row in data['weeks'])
        start = week_start(article['published_at'])
        detail = client.get(f"/scores/{company['id']}/{start}")
        assert detail.status_code == 200
        assert b'Saved weekly revisions' in detail.data


def test_alias_edit_persists_without_rewriting_existing_run_snapshot(database, monkeypatch):
    from ra.app import form_company
    from ra.search import search_queries
    company, old_run, article = seed(database)
    client = create_app().test_client()
    monkeypatch.setattr("ra.app.model_store.schedule_countries", lambda countries: None)
    with client.session_transaction() as session:
        session["csrf_token"] = "aliases-test"
    response = client.post(f"/companies/{company['id']}/edit", data={
        **company, "csrf_token": "aliases-test", "aliases": "Acme Local; Акме Производство",
        "alias_notes": "Confirmed against the company website"})
    assert response.status_code == 302
    saved = db.get_company(company["id"])
    assert saved["aliases"] == "Acme Local\nАкме Производство"
    assert saved["alias_notes"] == "Confirmed against the company website"
    assert b"Acme Local" in client.get(f"/companies/{company['id']}/edit").data
    old_subject = database.execute("SELECT subjects FROM search_runs WHERE id=%s", (old_run,)).fetchone()["subjects"][0]
    assert old_subject["aliases"] == ""
    snapshot = db.company_snapshot(saved)
    assert snapshot["aliases"] == saved["aliases"]
    assert '"Акме Производство"' in search_queries(snapshot)
    invalid = client.post(f"/companies/{company['id']}/edit", data={
        **saved, "csrf_token": "aliases-test", "aliases": "x" * 151})
    assert invalid.status_code == 302
    assert db.get_company(company["id"])["aliases"] == saved["aliases"]


def test_alias_schema_upgrade_and_legacy_intake_hash_are_idempotent(database):
    import hashlib
    import json
    from psycopg.types.json import Jsonb
    # Model a draft staged before aliases were introduced.
    company = {field: "" for field in db.FIELDS if field not in ("aliases", "alias_notes")}
    company.update(name="Legacy Name", country_registration="Brazil")
    fingerprint = hashlib.sha256(json.dumps(["manual", company], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    database.execute("INSERT INTO company_intake (source,source_key,draft_data,status) VALUES ('manual',%s,%s,'ready')",
                     (fingerprint, Jsonb(company)))
    database.execute(Path("ra/schema.sql").read_text())
    assert db.stage_company(company, source="manual") is None
    assert database.execute("SELECT count(*) AS n FROM company_intake").fetchone()["n"] == 1


def test_source_cache_weekly_dedup_review_and_grouping_corrections(database):
    from ra import verification_store
    company, run_id, first = seed(database, url='https://first.example/article')
    _, _, second = seed(database, url='https://second.example/article')
    body = 'Acme Manufacturing has filed for bankruptcy protection after missing bond payments.'
    calls = []
    def fetcher(url):
        calls.append(url)
        return {'status': 'available', 'language': 'en', 'body': body, 'sha256': 'same-body-hash',
                'final_url': url, 'method': 'test document'}
    score_store.refresh_scores()
    start = week_start(first['published_at'])
    assert score_store.week_detail(company['id'], start)[0]['score'] is None
    for row in (first, second):
        assert verification_store.verify_article(row['id'], fetcher=fetcher)['status'] == 'supported'
    assert len(calls) == 2
    assert score_store.week_detail(company['id'], start)[0]['metrics']['event_count'] == 1
    assert score_store.week_detail(company['id'], start)[0]['metrics']['duplicate_count'] == 1
    count = database.execute('SELECT count(*) AS n FROM article_verification_history').fetchone()['n']
    verification_store.verify_article(first['id'], fetcher=lambda url: pytest.fail('Cache should avoid refetch'))
    assert database.execute('SELECT count(*) AS n FROM article_verification_history').fetchone()['n'] == count
    assert score_store.refresh_scores() == {'articles_updated': 0, 'weeks_updated': 0}
    verification_store.review_group(second['id'], 'separate', 0, 'Separate proceeding confirmed by analyst')
    assert score_store.week_detail(company['id'], start)[0]['metrics']['event_count'] == 2
    verification_store.review_group(second['id'], 'merge', first['id'], 'Same proceeding reported by another publisher')
    assert score_store.week_detail(company['id'], start)[0]['metrics']['event_count'] == 1
    score_store.review_score(first['id'], 2, True, 'Analyst confirms completed recovery after reading the full notice')
    verification_store.verify_article(first['id'], fetcher=fetcher)
    assert score_store.week_detail(company['id'], start)[0]['score'] == 2
    assert score_store.get_article(first['id'])['score_status'] == 'reviewed'
    assert database.execute('SELECT count(*) AS n FROM article_event_history').fetchone()['n'] > 2
    assert database.execute('SELECT count(*) AS n FROM weekly_score_history').fetchone()['n'] > 2
    _, _, repeated = seed(database, url=first['url'])
    score_store.refresh_scores()
    assert score_store.get_article(repeated['id'])['event_group'] == score_store.get_article(first['id'])['event_group']
    assert score_store.week_detail(company['id'], start)[0]['score'] == 2


def test_source_queue_limits_endpoints_and_failure_visibility(database):
    from ra import verification_store
    from psycopg.types.json import Jsonb
    company, run_id, first = seed(database)
    for i in range(12):
        database.execute("INSERT INTO search_results (run_id,company_id,subject_key,subject_name,title,url,query,language) "
                         "VALUES (%s,%s,%s,%s,'Company news',%s,'test','en')",
                         (run_id,company['id'],str(company['id']),company['name'],f'https://example.org/{i}'))
    score_store.refresh_scores()
    assert verification_store.enqueue_run(run_id) == 8
    assert verification_store.enqueue_run(run_id) == 5
    assert verification_store.enqueue_run(run_id) == 0
    assert verification_store.job_summary(run_id)['queued'] == 13
    client = create_app().test_client()
    endpoint = f"/articles/{first['id']}/verify"
    assert client.post(endpoint).status_code == 400
    with client.session_transaction() as session:
        session['csrf_token'] = 'test-source-token'
    data = {'csrf_token': 'test-source-token'}
    assert client.post(endpoint, data=data).status_code == 302
    assert client.get(f"/articles/{first['id']}/score").status_code == 200
    verification_store.verify_article(first['id'], fetcher=lambda url: {'status': 'unavailable', 'reason': 'Publisher access blocked'})
    saved = score_store.get_article(first['id'])
    assert saved['verification']['status'] == 'unavailable'
    assert not saved['score_included']
    assert b'Publisher access blocked' in client.get(f"/articles/{first['id']}/score").data
    assert company['name'].encode() not in client.get(f'/runs/{run_id}').data.split(b'<tbody>')[-1].split(b'</tbody>')[0]
    assert client.post(f'/runs/{run_id}/verify', data=data).status_code == 302
    with pytest.raises(ValueError):
        verification_store.enqueue(first['id'], 'https://user:password@example.org/article')
    with pytest.raises(ValueError):
        verification_store.review_group(first['id'], 'merge', first['id'], 'Cannot merge self')
    other = database.execute("INSERT INTO companies (name,country_registration) VALUES ('Other Ltd','Brazil') RETURNING *").fetchone()
    _, _, target = seed(database, url='https://other.org/article')
    database.execute('UPDATE search_results SET company_id=%s WHERE id=%s', (other['id'], target['id']))
    with pytest.raises(ValueError, match='same saved company'):
        verification_store.review_group(first['id'], 'merge', target['id'], 'Wrong legal company')


def test_verification_queue_resumes_interrupted_jobs_without_starting_searches(database, monkeypatch):
    from ra import verification_store
    _, _, article = seed(database)
    verification_store.enqueue(article['id'])
    database.execute("UPDATE verification_jobs SET status='running'")
    started = []
    class FakeThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            started.append(True)
        def is_alive(self):
            return True
    monkeypatch.setattr(verification_store, '_thread', None)
    monkeypatch.setattr(verification_store.threading, 'Thread', FakeThread)
    verification_store.start()
    verification_store.start()
    assert len(started) == 1
    assert database.execute('SELECT status FROM verification_jobs').fetchone()['status'] == 'queued'
    assert database.execute('SELECT count(*) AS n FROM search_runs').fetchone()['n'] == 1
