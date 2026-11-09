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
    from ra import headline_review
    monkeypatch.setattr(headline_review, "readiness", lambda: {"ready": False, "message": "Offline test"})
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
    assert score_store.get_article(article["id"])["score"] == -2
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
    for score in (-4, -3, 3, 4, 1.5, True):
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
    worker.process_run(run_id, searcher=searcher, topic_searcher=lambda *args, **kwargs: [])
    assert db.get_run(run_id)["status"] == "completed"
    assert db.get_results(run_id)[0]["score"] == 2
    assert all(r["score"] is None for r in score_store.weekly_history(company["id"]))
    assert database.execute("SELECT count(*) AS n FROM verification_jobs").fetchone()["n"] == 1


def test_scoring_pages_api_and_csrf_protected_correction(database):
    company, run_id, article = seed(database)
    score_store.refresh_scores()
    with create_app().test_client() as client:
        score_page = client.get('/scores')
        assert score_page.status_code == 200
        assert b'Five grades' in score_page.data and b'Strong negative' in score_page.data
        assert b'There is no universal percentage threshold' in score_page.data
        assert b'Severe negativity' not in score_page.data
        assert client.get('/scores?weeks=bad').status_code == 400
        response = client.get(f'/runs/{run_id}?view=review')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'score--2' in body
        endpoint = f"/articles/{article['id']}/score"
        review_page = client.get(endpoint)
        assert review_page.status_code == 200
        import re
        select = re.search(r'<select name="score"[^>]*>(.*?)</select>', review_page.get_data(as_text=True)).group(1)
        assert [int(value) for value in re.findall(r'<option value="(-?\d+)"', select)] == [-2, -1, 0, 1, 2]
        assert b'limited impact' in review_page.data
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


def test_theme_cache_survives_new_run_and_invalidates_changed_factors(database, monkeypatch):
    from ra import risk_factors, verification_store
    _, _, first = seed(database, url='https://first.example/same-news')
    _, _, second = seed(database, url='https://second.example/same-news')
    factor = database.execute("INSERT INTO risk_factors(english,scope) VALUES ('Debt stress','company') RETURNING *").fetchone()
    calls = []
    def assess(*args):
        calls.append(args)
        return {'status': 'checked', 'coverage': 'headline and source text', 'matches': []}
    monkeypatch.setattr(risk_factors, 'assess', assess)
    def fetcher(url):
        return {'status': 'available', 'language': 'en', 'body': 'Acme Manufacturing files for bankruptcy.',
                'sha256': 'same-body', 'final_url': url, 'method': 'test document'}
    for row in (first, second):
        verification_store.verify_article(row['id'], fetcher=fetcher)
    assert len(calls) == 1
    a = score_store.get_article(first['id'])['risk_factor_review']
    b = score_store.get_article(second['id'])['risk_factor_review']
    assert a == b and a['input_hash']
    database.execute("UPDATE risk_factors SET english='Liquidity stress' WHERE id=%s", (factor['id'],))
    verification_store.verify_article(second['id'], fetcher=fetcher)
    assert len(calls) == 2
    assert score_store.get_article(second['id'])['risk_factor_review']['input_hash'] != a['input_hash']


def test_multilingual_source_cache_retries_and_analyst_decisions(database, monkeypatch):
    from ra import headline_review, translation, verification_store
    company, run_id, article = seed(database)
    source = {'status': 'available', 'language': 'vi', 'body': 'Original company financial statement.',
              'sha256': 'original-body-hash', 'final_url': article['url'], 'method': 'test document'}
    monkeypatch.setattr(headline_review, 'readiness', lambda: {'ready': False, 'message': 'Start LM Studio'})
    result = verification_store.verify_article(article['id'], fetcher=lambda url: source)
    assert result['status'] == 'needs_review' and not score_store.get_article(article['id'])['score_included']
    assert verification_store.job_summary(run_id)['source_retryable'] == 1
    assert verification_store.retry_failed_run(run_id) == 1
    monkeypatch.setattr(headline_review, 'readiness', lambda: {'ready': True})
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: (
        'Acme Manufacturing reports net losses of 40 million dollars.', 'Argos test'))
    calls = []
    def audit(*args, **kwargs):
        calls.append(args)
        return {'status': 'faithful', 'issues': [], 'model': headline_review.QWEN, 'reasoning': 'off'}
    monkeypatch.setattr(headline_review, 'audit', audit)
    no_fetch = lambda url: pytest.fail('Saved source must be reused')
    result = verification_store.verify_article(article['id'], fetcher=no_fetch)
    assert result['status'] == 'supported' and result['score'] == -1
    assert result['evidence'][0]['original_passages'] == [source['body']]
    assert score_store.get_article(article['id'])['score_included']
    cached = database.execute('SELECT document FROM article_documents WHERE url=%s', (article['url'],)).fetchone()['document']
    assert cached['body'] == source['body'] and cached['translation']['status'] == 'faithful'
    with create_app().test_client() as client:
        page = client.get(f"/articles/{article['id']}/score").text
        assert 'Original source passage' in page and source['body'] in page
        assert '1/1 passages approved' in page
    score_store.review_score(article['id'], 0, False, 'Analyst excludes this source after further checks')
    assert verification_store.verify_article(article['id'], fetcher=no_fetch) == result
    assert len(calls) == 1
    saved = score_store.get_article(article['id'])
    assert saved['score_status'] == 'reviewed' and saved['score'] == 0 and not saved['score_included']


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
    risk_page = client.get(f'/runs/{run_id}').data
    assert first["title"].encode() not in risk_page and b"No leads in this view" in risk_page
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


def test_shared_news_only_changes_the_company_with_source_evidence(database):
    from ra import verification_store
    subjects = [
        db.company_snapshot(database.execute(
            "INSERT INTO companies(name,country_registration,primary_industries) VALUES (%s,'Brazil','fertilizers') RETURNING *",
            (name,)).fetchone())
        for name in ("Acme Manufacturing", "Other Industries")
    ]
    run_id = db.create_run("all", subjects)
    calls = []
    def search(branch, topic, *, edition):
        calls.append((branch, topic, edition))
        return [({"title": "Brazil fertilizer tariffs rise", "url": "https://example.test/tariffs",
                  "published_at": datetime.now(timezone.utc) - timedelta(hours=1), "query": topic,
                  "language": "en", "publisher": "Test", "source_kind": "context", "signal_terms": []},
                 "import costs and access to customers")]
    worker.process_run(run_id, searcher=lambda company: ([], []), topic_searcher=search)
    assert calls == [("country", "brazil", ""), ("country", "brazil", "brazil"),
                     ("sector", "fertilizer", ""), ("sector", "fertilizer", "brazil")]
    # A worker replay uses the saved topics, including their publication dates.
    database.execute("UPDATE search_runs SET status='queued' WHERE id=%s", (run_id,))
    worker.process_run(run_id, searcher=lambda company: ([], []), topic_searcher=search)
    assert len(calls) == 4
    leads = db.get_results(run_id)
    assert len(leads) == 2 and all(len(a["exposure_paths"]) == 2 for a in leads)
    assert all(not a["score_included"] and a["score"] == 0 for a in leads)
    with pytest.raises(ValueError, match="impact"):
        score_store.review_score(leads[0]["id"], -2, True, "The whole sector is exposed")
    for lead in leads:
        verification_store.verify_article(lead["id"], fetcher=lambda url: {
            "status": "available", "language": "en", "body": "Acme Manufacturing announces plant closure following the tariff increase."})
    scored = {a["subject_name"]: a for a in db.get_results(run_id)}
    assert scored["Acme Manufacturing"]["score_included"] and scored["Acme Manufacturing"]["score"] == -2
    assert not scored["Other Industries"]["score_included"]
    assert score_store.weekly_history(subjects[0]["id"])[0]["score"] == -2
    assert score_store.weekly_history(subjects[1]["id"])[0]["score"] is None
    client = create_app().test_client()
    page = client.get(f"/runs/{run_id}?view=exposure")
    assert page.status_code == 200 and b"Acme Manufacturing" in page.data and b"Other Industries" in page.data
    assert b"import costs and access to customers" in page.data
    assert client.get(f"/runs/{run_id}/download.xlsx").status_code == 200


def test_backfilled_company_report_uses_later_reviewed_scores(database):
    from ra.company_report import load_report
    from ra.company_exports import company_pdf, company_xlsx
    from pypdf import PdfReader
    from openpyxl import load_workbook
    company, run_id, article = seed(database)
    end = datetime.now(timezone.utc) - timedelta(weeks=2)
    database.execute('UPDATE search_runs SET window_start=%s,window_end=%s WHERE id=%s',
                     (end-timedelta(days=6),end,run_id))
    database.execute('UPDATE search_results SET published_at=%s WHERE id=%s', (end,article['id']))
    score_store.refresh_scores(run_id)
    score_store.review_score(article['id'], -1, True, 'Test reviewed source evidence')
    run = db.get_run(run_id)
    report = load_report(run, run['subjects'][0])
    assert report['ratings'][0][2]['score'] == -1
    assert report['ratings'][0][2]['event_count'] == 1
    assert 'Retrospective assessment' in ' '.join(p.extract_text() for p in PdfReader(company_pdf(report)).pages)
    assert any('Retrospective assessment' in str(cell.value) for cell in load_workbook(company_xlsx(report)).active['A'])


def test_finished_run_company_exports_are_scoped_and_historical(database):
    from ra.company_report import load_report
    company, run_id, a = seed(database)
    score_store.refresh_scores(run_id)
    now = db.get_run(run_id)["created_at"]
    baseline = now - timedelta(weeks=1)
    for instant, score in [(baseline - timedelta(seconds=1), -1), (baseline + timedelta(seconds=1), -2)]:
        database.execute("INSERT INTO weekly_score_history(company_id,week_start,snapshot,created_at) "
                         "VALUES (%s,%s,jsonb_build_object('score',%s::int,'event_count',1,'search_count',1,"
                         "'negative_count',1,'positive_count',0,'neutral_count',0,'source_errors',0),%s)",
                         (company["id"], week_start(baseline), score, instant))
    run = db.get_run(run_id)
    report = load_report(run, run["subjects"][0])
    assert report["ratings"][1][2]["score"] == -1
    client = create_app().test_client()
    prefix = f"/runs/{run_id}/company-report"
    assert client.get(f"{prefix}?company={company['id']}&format=pdf").data.startswith(b"%PDF")
    assert client.get(f"{prefix}?company={company['id']}&format=xlsx").data.startswith(b"PK")
    assert client.get(f"{prefix}?company=1234567&format=pdf").status_code == 404
    database.execute("UPDATE search_runs SET status='running' WHERE id=%s", (run_id,))
    assert client.get(f"{prefix}?company={company['id']}&format=pdf").status_code == 409
    adhoc = db.create_run("one_off", [{"name":"One Off", "country_registration":"Brazil"}])
    database.execute("UPDATE search_runs SET status='completed' WHERE id=%s", (adhoc,))
    assert client.get(f"/runs/{adhoc}/company-report?company=one-off&format=pdf").data.startswith(b"%PDF")


def test_progress_measures_entire_run_and_separates_translation_from_source(database):
    company, run_id, article = seed(database)
    client = create_app().test_client()
    endpoint = f"/api/runs/{run_id}/status"
    for i in range(2):
        database.execute("INSERT INTO search_results (run_id,subject_key,subject_name,title,url,query,language,title_en) "
                         "VALUES (%s,'x','X','texto original',%s,'q','es','translation')",
                         (run_id, f'https://example.org/spanish/{i}'))
    database.execute("INSERT INTO verification_jobs(result_id,status) VALUES (%s,'failed')", (article['id'],))
    database.execute("UPDATE search_results SET headline_review='{\"status\":\"material_error\"}' "
                     "WHERE url='https://example.org/spanish/1'")
    v = client.get(endpoint).json['verification']
    assert (v['percent'], v['total'], v['failed'], v['attention']) == (33, 3, 1, 1)
    assert (v['translation_percent'], v['translations'], v['reviewed']) == (50, 2, 1)
    database.execute("UPDATE search_results SET verification='{\"status\":\"unavailable\"}' WHERE language='es'")
    v = client.get(endpoint).json['verification']
    assert v['percent'] == 100 and v['attention'] == 3
    database.execute("UPDATE search_results SET headline_review='{\"status\":\"unavailable\"}' "
                     "WHERE url='https://example.org/spanish/0'")
    database.execute("INSERT INTO search_results (run_id,subject_key,subject_name,title,url,query,language) "
                     "VALUES (%s,'x','X','missing','https://example.org/missing','q','es')", (run_id,))
    v = client.get(endpoint).json['verification']
    assert (v['translation_percent'], v['translation_total'], v['reviewed']) == (33, 3, 1)
    assert (v['review_failed'], v['missing'], v['review_attention']) == (1, 1, 1)
    assert (v['review_pending'], v['review_exhausted']) == (2, 0)
    assert v['revision']
    database.execute("UPDATE search_results SET headline_review=headline_review || '{\"retry_count\":3}' "
                     "WHERE language='es' AND title_en<>''")
    v = client.get(endpoint).json['verification']
    # A completed material-error verdict is not an exhausted review.
    assert (v['review_pending'], v['review_exhausted']) == (1, 1)


def test_translation_retries_resume_after_reconnection_and_preserve_user_edits(database, monkeypatch):
    from ra import headline_review as qa, translation
    _, run_id, article = seed(database)
    database.execute("UPDATE search_results SET language='es',title_en='English',headline_review='{\"status\":\"unavailable\"}' WHERE id=%s", (article['id'],))
    database.execute("UPDATE search_runs SET retry_translations=FALSE WHERE id=%s", (run_id,))
    calls = []
    def audit(*args, **kwargs):
        calls.append(args)
        return {"status": "faithful", "reasoning": "off"}
    monkeypatch.setattr(qa, "audit", audit)
    qa.retry_pending()
    assert calls == []
    monkeypatch.setattr(qa, "readiness", lambda: {"ready": True})
    qa.retry_pending()
    assert calls == []
    assert qa.request_retry(run_id) == 1
    qa.retry_pending()
    assert len(calls) == 1
    assert db.get_results(run_id)[0]['headline_review']['status'] == 'faithful'
    qa.retry_pending()
    assert len(calls) == 1

    database.execute("UPDATE search_results SET title_en='',headline_review='{}' WHERE id=%s", (article['id'],))
    def translate(*args):
        database.execute("UPDATE search_results SET title_en='User correction' WHERE id=%s", (article['id'],))
        return 'Automatic translation', 'test'
    monkeypatch.setattr(translation, "translate_text", translate)
    qa.retry_pending()
    assert db.get_results(run_id)[0]['title_en'] == 'User correction'


def test_translation_retry_limits_and_backoff(database, monkeypatch):
    from ra import headline_review as qa, translation
    _, run_id, article = seed(database)
    database.execute("UPDATE search_results SET language='es',headline_review='{}' WHERE id=%s", (article['id'],))
    monkeypatch.setattr(qa, "readiness", lambda: {"ready": True})
    calls = []
    def translate(*args):
        calls.append(args)
        return '', 'unsupported'
    monkeypatch.setattr(translation, 'translate_text', translate)
    for _ in range(3):
        qa.retry_pending()
        qa.retry_pending()
        database.execute("UPDATE search_results SET headline_review=headline_review-'retry_after'")
    qa.retry_pending()
    assert len(calls) == 3
    assert db.get_results(run_id)[0]['headline_review']['retry_count'] == 3


def test_setup_retries_sources_without_duplicate_jobs_or_reusing_failed_cache(database):
    from ra import verification_store
    _, run_id, article = seed(database)
    database.execute("UPDATE search_results SET verification='{\"status\":\"unavailable\"}' WHERE id=%s", (article['id'],))
    database.execute("INSERT INTO verification_jobs(result_id,status,source_url) VALUES (%s,'failed','https://publisher.example/original')", (article['id'],))
    database.execute("INSERT INTO article_documents(url,document) VALUES ('https://publisher.example/original','{\"status\":\"unavailable\"}')")
    assert verification_store.job_summary(run_id)['source_retryable'] == 1
    assert verification_store.retry_failed_run(run_id) == 1
    assert verification_store.retry_failed_run(run_id) == 0
    job = database.execute('SELECT * FROM verification_jobs WHERE result_id=%s', (article['id'],)).fetchone()
    assert job['status'] == 'queued' and job['source_url'] == 'https://publisher.example/original'
    assert database.execute('SELECT count(*) n FROM article_documents').fetchone()['n'] == 0
    assert verification_store.job_summary(run_id)['source_retryable'] == 0


def test_single_article_retry_refetches_failure_but_keeps_successful_cache(database):
    from ra import verification_store
    _, _, article = seed(database)
    url = 'https://publisher.example/original'
    database.execute("INSERT INTO article_documents(url,document) VALUES (%s,'{\"status\":\"unavailable\"}')", (url,))
    verification_store.enqueue(article['id'], url)
    assert database.execute('SELECT count(*) n FROM article_documents').fetchone()['n'] == 0
    database.execute("INSERT INTO article_documents(url,document) VALUES (%s,'{\"status\":\"available\"}')", (url,))
    database.execute("UPDATE verification_jobs SET status='completed' WHERE result_id=%s", (article['id'],))
    verification_store.enqueue(article['id'], url)
    assert database.execute('SELECT count(*) n FROM article_documents').fetchone()['n'] == 1


def test_score_refresh_reconsiders_future_publication_without_changing_analyst_decisions(database, monkeypatch):
    from ra import scoring
    _, _, article = seed(database)
    published = article['published_at']
    class Clock(datetime):
        instant = published - timedelta(seconds=1)
        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)
    monkeypatch.setattr(scoring, 'datetime', Clock)
    database.execute("UPDATE search_results SET verification=%s WHERE id=%s", (db.Jsonb({
        'status': 'supported', 'score': -3, 'evidence': [], 'reason': 'Source confirms bankruptcy'}), article['id']))
    score_store.refresh_scores()
    assert not score_store.get_article(article['id'])['score_included']
    assert score_store.refresh_scores()['articles_updated'] == 0
    Clock.instant = published
    assert score_store.refresh_scores()['articles_updated'] == 1
    assert score_store.get_article(article['id'])['score_included']
    score_store.review_score(article['id'], 0, False, 'Analyst excludes this event after source review')
    Clock.instant += timedelta(days=1)
    assert score_store.refresh_scores()['articles_updated'] == 0
    assert not score_store.get_article(article['id'])['score_included']


def test_setup_routes_retry_only_requested_stage_and_preserve_original_search(database, monkeypatch):
    from ra import headline_review as qa
    _, run_id, article = seed(database, status='partial')
    database.execute("UPDATE search_results SET language='es',title_en='English',headline_review='{\"status\":\"inconclusive\",\"retry_count\":3}' WHERE id=%s", (article['id'],))
    client = create_app().test_client()
    endpoint = f'/setup/runs/{run_id}/retry'
    assert client.get('/setup').status_code == 200
    assert client.post(endpoint, data={'stage': 'translations'}).status_code == 400
    with client.session_transaction() as session:
        session['csrf_token'] = 'token'
    assert client.post(endpoint, data={'stage': 'translations', 'csrf_token': 'token'}).status_code == 302
    assert 'retry_count' not in db.get_results(run_id)[0]['headline_review']
    queued = []
    monkeypatch.setattr(worker, 'enqueue', queued.append)
    assert client.post(endpoint, data={'stage': 'search', 'csrf_token': 'token'}).status_code == 302
    assert len(queued) == 1 and queued[0] != run_id
    assert db.get_run(queued[0])['subjects'] == db.get_run(run_id)['subjects']
    assert len(db.get_results(run_id)) == 1
    assert db.get_run(run_id)['status'] == 'partial'
    assert client.post(endpoint, data={'stage': 'unknown', 'csrf_token': 'token'}).status_code == 400
    database.execute("UPDATE search_runs SET status='running' WHERE id=%s", (run_id,))
    assert client.post(endpoint, data={'stage': 'translations', 'csrf_token': 'token'}).status_code == 409
    with pytest.raises(ValueError):
        qa.request_retry(run_id)


def test_keyword_edit_translate_and_disable_in_review(database, monkeypatch):
    from ra import keywords, translation
    monkeypatch.setattr(keywords, "_rules", keywords._rules)
    monkeypatch.setattr(keywords, "schedule", lambda: None)
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda terms, code: {})
    def offline(*args):
        raise keywords.ProviderError("offline test")
    monkeypatch.setattr(keywords.online_translation, "translate", offline)
    term = "covenant breach"
    def model(prompt, data, schema, **kwargs):
        if "english" in schema["properties"]:
            return {"english": True}
        if schema["properties"][term]["type"] == "boolean":
            assert not kwargs
            return {term: data["translations"][term] == "violação de cláusula"}
        from ra.headline_review import GEMMA
        assert kwargs == {"model": GEMMA}
        return {term: "violação de cláusula" if isinstance(data["terms"], dict) else "unrelated wording"}
    monkeypatch.setattr(keywords, "model_json", model)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = dict(csrf_token="token", english=term, direction="adverse", enabled="yes")
    assert client.post("/keywords", data=form).status_code == 302
    row, = keywords.records()
    keywords.translate_batch([row], "pt")
    keywords.reload_rules()
    assert keywords.matches("violação de cláusula", "pt") == [(term, term, "adverse")]
    edit = dict(form, id=row["id"], previous=term, english="debt relief", direction="favorable")
    client.post("/keywords", data=edit)
    row, = keywords.records()
    assert row["translations"] == {} and not keywords.matches(term)
    assert keywords.matches("debt relief") == [("debt relief", "debt relief", "favorable")]
    client.post("/keywords", data={**edit, "previous": "debt relief", "enabled": ""})
    assert not keywords.matches("debt relief")
    keywords.seed(database)
    count = len(keywords.records())
    keywords.seed(database)
    assert count == len(keywords.records())


def test_registry_history_is_per_issuer_idempotent_and_reported(database):
    from ra import governance_roster as registry
    from ra.company_report import load_report
    company, first, _ = seed(database)
    # A synthetic CNPJ keeps this registry fixture independent of Heringer's live IR site.
    company = dict(company, tax_id='12345678000195')
    key = str(company['id'])
    member = {'nome_socio':'Jane Example', 'qualificacao_socio':'Diretor'}
    def registry_data(url):
        return {'razao_social':company['name'], 'cnpj':company['tax_id'], 'qsa':[member]}
    registry.check(first, key, company, registry_data)
    registry.check(first, key, company, lambda u: pytest.fail('duplicate fetch'))
    assert registry.observations(first)[0]['notes'][0].startswith('First observation')
    database.execute("UPDATE governance_rosters SET observed_at=now()-interval '7 days' WHERE run_id=%s",(first,))
    _, second, _ = seed(database)
    member['nome_socio'] = 'Another Example'
    registry.check(second, key, company, registry_data)
    notes = load_report(db.get_run(second), company)['rosters'][0]['notes']
    assert notes == ['Newly listed · Another Example · Diretor', 'Previously listed · Jane Example · Diretor']
    with create_app().test_client() as client:
        assert 'Another Example' in client.get(f'/runs/{second}?view=governance').get_data(as_text=True)


def test_registry_failure_preserves_last_good_comparison_and_retries(database):
    from ra import governance_roster as registry
    company, first, _ = seed(database)
    key = str(company['id'])
    url = 'https://issuer.example/board'
    registry.record(first, key, company, url, lambda _: ('Board', {'old':'Old director'}))
    _, second, _ = seed(database)
    def unavailable(_):
        raise ValueError('Incomplete roster')
    with pytest.raises(ValueError):
        registry.record(second, key, company, url, unavailable)
    row, = registry.observations(second, key)
    assert row['notes'] == ['Incomplete roster'] and row['previous_at'] is None
    _, third, _ = seed(database)
    registry.record(third, key, company, url, lambda _: ('Board', {'new':'New director'}, 'https://issuer.example/filing'))
    row, = registry.observations(third, key)
    assert row['source_url'].endswith('/filing')
    assert row['notes'] == ['Newly listed · New director', 'Previously listed · Old director']
    registry.record(second, key, company, url, lambda _: ('Board', {'new':'New director'}))
    assert not registry.observations(second, key)[0]['coverage_note']


def test_risk_factor_editing_translation_and_retry(database, monkeypatch):
    from ra import keywords, risk_factors, translation
    monkeypatch.setattr(risk_factors, "_rows", risk_factors._rows)
    scheduled = []
    monkeypatch.setattr(keywords, "schedule", lambda: scheduled.append(True))
    monkeypatch.setattr(risk_factors, "languages", lambda: ["pt"])
    text = "Currency falls more than 10% in seven days"
    good = "A moeda cai mais de 10% em sete dias"
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda terms, code: {text:good.replace("10%", "5%")})
    def model(prompt, data, schema, **kwargs):
        if "english" in schema["properties"]:
            return {"english": True}
        if "translations" in data:
            assert not kwargs
            return {text: data["translations"][text] == good}
        from ra.headline_review import GEMMA
        assert kwargs == {"model": GEMMA}
        return {text:good}
    monkeypatch.setattr(keywords, "model_json", model)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = dict(csrf_token="token", english=text, scope="country", enabled="yes")
    assert client.post("/risk-factors", data=form).location.endswith("/risk-factors")
    assert scheduled and client.get("/risk-factors").status_code == 200
    row, = risk_factors.records()
    keywords.translate_batch([row], "pt", factors=True)
    row, = risk_factors.records()
    assert row["translations"] == {"pt": good} and not keywords.records()
    assert "1/1 languages" in client.get("/risk-factors").text
    edit = dict(form, id=row["id"], previous=text, english="Payment restrictions", enabled="")
    client.post("/risk-factors", data=edit)
    row, = risk_factors.records()
    assert not row["enabled"] and row["translations"] == {}
    client.post("/risk-factors", data={**edit, "english":"Stale edit"})
    assert risk_factors.records()[0]["english"] == "Payment restrictions"
    risk_factors.seed(database)
    risk_factors.seed(database)
    assert len(risk_factors.records()) == len(risk_factors.SEEDS) + 1


def test_risk_descriptions_reject_overlength_without_clipping(database, monkeypatch):
    from html.parser import HTMLParser
    from ra import keywords, risk_factors
    inputs = []
    class Fields(HTMLParser):
        def handle_starttag(self, tag, attrs):
            field = dict(attrs)
            if tag == "input" and field.get("name") == "english":
                inputs.append(field)
    monkeypatch.setattr(risk_factors, "languages", lambda: [])
    monkeypatch.setattr(keywords, "schedule", lambda: pytest.fail("Invalid text must not queue translation"))
    row = database.execute("INSERT INTO risk_factors(english,scope) VALUES ('Sector downturn','sector') RETURNING *").fetchone()
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    Fields().feed(client.get("/risk-factors").text)
    assert len(inputs) == 2
    assert all(field.get("pattern") == ".{3,180}" and "maxlength" not in field for field in inputs)
    text = "Negative trends in related sectors " * 6
    form = dict(csrf_token="token", english=text, scope="sector", enabled="yes")
    for edit in ({}, dict(id=row["id"], previous=row["english"])):
        response = client.post("/risk-factors", data=form | edit, follow_redirects=True)
        assert "up to 180 characters" in response.text
        assert risk_factors.records() == [row]


def test_edit_one_risk_translation_from_list(database, monkeypatch):
    from ra import keywords, risk_factors, translation
    english = "Market drops more than 5%"
    original = "Queda do mercado superior a 5%"
    corrected = "O mercado recua mais de 5%"
    row = database.execute("INSERT INTO risk_factors(english,scope,translations,search_cues) "
                           "VALUES (%s,'country',%s,%s) RETURNING *", (english,
                           db.Jsonb({"pt": original, "el": "old Greek"}),
                           db.Jsonb({"pt": ["old Portuguese cue"], "el": ["Greek cue"]}))).fetchone()
    monkeypatch.setattr(risk_factors, "_rows", risk_factors._rows)
    monkeypatch.setattr(risk_factors, "languages", lambda: ["pt", "el", "es"])
    monkeypatch.setattr(keywords, "schedule", lambda: None)
    monkeypatch.setattr(keywords, "model_json", lambda prompt, data, schema: {english: data["translations"][english] == corrected})
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = dict(action="translation", id=row["id"], previous=english, language="pt", original=original, translation=corrected)
    assert "Edit Spanish translation" in client.get("/risk-factors").text
    assert client.post("/risk-factors", data=form).status_code == 400
    form["csrf_token"] = "token"
    def submit(**changes):
        return client.post("/risk-factors", data={**form, **changes}, follow_redirects=True).text
    assert "Keep the percentages" in submit(translation="queda de 6%")
    assert "Choose an active language" in submit(language="xx")
    rejected = submit(translation="incorrect 5%")
    assert "could not confirm" in rejected and "incorrect 5%</textarea>" in rejected
    assert risk_factors.records()[0]["translations"]["pt"] == original
    assert "Translation saved" in submit()
    assert "factor or translation changed" in submit()
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda terms, code: {english: original})
    monkeypatch.setattr(keywords, "model_json", lambda *args: {english: True})
    keywords.translate_batch([row], "pt", factors=True)
    saved, = risk_factors.records()
    assert saved["translations"] == {"pt": corrected, "el": "old Greek"} and saved["search_cues"] == {"el": ["Greek cue"]}
    assert risk_factors.search_phrase(risk_factors.active("country")[0], "pt") == corrected


def test_save_all_risk_factors(database, monkeypatch):
    from ra import keywords, risk_factors
    first, second = list(risk_factors.SEEDS)[:2]
    ids = [database.execute("INSERT INTO risk_factors(english,scope,translations,search_cues) "
                            "VALUES (%s,'company',%s,%s) RETURNING id",
                            (name, db.Jsonb({"pt": "original", "es": "otra"}),
                             db.Jsonb({"pt": ["old cue"], "es": ["otra"]}))).fetchone()["id"]
           for name in (first, second)]
    monkeypatch.setattr(risk_factors, "_rows", risk_factors._rows)
    monkeypatch.setattr(risk_factors, "languages", lambda: ["pt", "es"])
    monkeypatch.setattr(keywords, "validate_english", lambda *args: None)
    reviews, scheduled = [], []
    def review(terms, code, factors, context):
        reviews.append((terms, code))
        return {english: text != "rejected" for english, text in terms.items()}
    monkeypatch.setattr(keywords, "review_translations", review)
    monkeypatch.setattr(keywords, "schedule", lambda: scheduled.append(True))
    client = create_app().test_client()
    assert 'id="save-all"' in client.get("/risk-factors").text
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = {"csrf_token": "token", "id": list(map(str, ids))}
    for row_id, english in zip(ids, (first, second)):
        form.update({f"{row_id}.{field}": value for field, value in {
            "previous": english, "english": english, "scope": "country", "previous_scope": "company",
            "enabled": "false", "previous_enabled": "true", "language": "pt",
            "pt.original": "original", "pt.translation": "updated",
        }.items()})
    renamed = "Major change in shareholding"
    form[f"{ids[1]}.english"] = renamed
    def submit(**overrides):
        return client.post("/risk-factors/save-all", data=form | overrides)
    assert submit(csrf_token="bad").status_code == 400
    assert "Portuguese" in submit(**{f"{ids[1]}.pt.translation": "rejected"}).json["error"]
    assert "changed" in submit(**{f"{ids[1]}.pt.original": "stale"}).json["error"]
    assert all(row["enabled"] and row["scope"] == "company" for row in risk_factors.records())
    assert not scheduled
    assert submit().json == {"saved": 2}
    a, b = sorted(risk_factors.records(), key=lambda row: row["id"])
    assert a["translations"] == {"pt": "updated", "es": "otra"} and a["search_cues"] == {"es": ["otra"]}
    assert b["english"] == renamed and b["translations"] == {"pt": "updated"} and b["search_cues"] == {}
    assert reviews[-1] == ({renamed: "updated"}, "pt")
    assert all(not row["enabled"] and row["scope"] == "country" for row in (a, b))
    assert len(scheduled) == 1


def test_live_search_queue_and_worker_step(database, monkeypatch):
    active = db.create_run("selected", [{"name": "Heringer"}])
    waiting = db.create_run("selected", [{"name": "Heringer"}])
    database.execute("UPDATE search_runs SET status='running' WHERE id=%s", (active,))
    client = create_app().test_client()
    def progress(run_id):
        return client.get(f"/api/runs/{run_id}/status").json["discovery"]
    assert f"Waiting behind run #{active}" in progress(waiting)["message"]
    monkeypatch.setattr(worker, "_activity", None)
    worker._report(active, "Heringer · Searching company news · query 2 of 8", 1, 8)
    step = progress(active)
    assert (step["done"], step["total"]) == (1, 8) and step["age"] >= 0
    assert step["message"] in client.get(f"/runs/{active}").text
    worker._report(active, "Checking leadership and ownership records")
    assert progress(active)["done"] is None
    database.execute("UPDATE search_runs SET status='completed' WHERE id=%s", (active,))
    assert progress(active) is None and "Waiting for the search worker" in progress(waiting)["message"]


def test_firecrawl_monthly_credit_boundary(database):
    today = database.execute("SELECT (now() AT TIME ZONE 'UTC')::date AS day").fetchone()['day']
    start = today.replace(day=1)
    with database.cursor() as cur:
        cur.executemany("INSERT INTO news_api_usage VALUES (%s,%s,%s)", [
            ('firecrawl', start - timedelta(days=1), 1000), ('firecrawl', start, 999), ('jina', today, 20)])
    assert db.reserve_firecrawl_credit() == today
    assert db.reserve_firecrawl_credit() is None
    assert db.reserve_news_api_request('jina', None)
    db.release_firecrawl_credit(today)
    assert db.reserve_firecrawl_credit() == today
    assert db.reserve_firecrawl_credit() is None


@pytest.mark.parametrize("historical", [False, True])
def test_worker_enforces_fixed_week_before_translation_and_verification(database, monkeypatch, caplog, historical):
    _, run_id, _ = seed(database, status="queued")
    end = db.get_run(run_id)["created_at"]
    start = end - timedelta(days=7)
    if historical:
        end -= timedelta(weeks=4)
        start = end - timedelta(days=7)
        database.execute("UPDATE search_runs SET window_start=%s,window_end=%s WHERE id=%s", (start,end,run_id))
    translated = []
    monkeypatch.setattr(worker, "plan_subjects", lambda subjects, run_id: subjects)
    monkeypatch.setattr(worker, "translate_text", lambda title, *args: (translated.append(title) or title, "English"))
    monkeypatch.setattr(worker, "detect_language", lambda *args: "en")
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    dates = [start, end, start - timedelta(microseconds=1), end + timedelta(microseconds=1), None,
             start.astimezone(timezone.utc).replace(tzinfo=None),
             (start - timedelta(seconds=1)).isoformat(), "invalid"]
    articles = [dict(title=f"Company update {i}", url=f"https://example.test/window/{kind}/{i}",
                     published_at=date, language="en", publisher="Test", query="test", signal_terms=[],
                     source_kind=kind)
                for kind in ("news", "official", "context") for i, date in enumerate(dates)]
    # Exposure results join after company discovery and must obey the same window.
    monkeypatch.setattr(worker, "add_exposure_leads", lambda subject, leads, *args: leads + articles[16:])
    with caplog.at_level("INFO", logger="ra.worker"):
        worker.process_run(run_id, searcher=lambda subject: (articles[:16], []),
                           topic_searcher=lambda *args, **kwargs: [], roster_check=lambda *args: None)
    results = db.get_results(run_id)
    assert len(results) == len(translated) == 9
    assert all(start <= row["published_at"] <= end for row in results)
    assert db.get_run(run_id)["result_count"] == 9
    assert database.execute("SELECT count(*) AS n FROM verification_jobs").fetchone()["n"] == 9
    assert '"excluded": 15' in caplog.text
    if historical:
        note = database.execute("SELECT coverage_note FROM governance_rosters WHERE run_id=%s", (run_id,)).fetchone()
        assert "not treated as historical records" in note["coverage_note"]
        assert score_store.week_detail(db.get_run(run_id)["subjects"][0]["id"], week_start(end))[0]["metrics"]["search_count"] == 1


def test_historical_run_route_validates_and_preserves_window_on_retry(database, monkeypatch):
    company, _, _ = seed(database)
    monkeypatch.setattr(worker, "enqueue", lambda run_id: None)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    end = datetime.now(timezone.utc) - timedelta(weeks=2)
    start = end - timedelta(days=7) + timedelta(microseconds=1)
    form = dict(csrf_token="token",window_start=start.isoformat(),window_end=end.isoformat())
    for invalid in ({"window_end":""}, {"window_start":end.isoformat()},
                    {"window_start":start.replace(tzinfo=None).isoformat()},
                    {"window_start":(start-timedelta(days=1)).isoformat()}):
        assert client.post("/search/all",data=form|invalid).status_code == 400
    response = client.post("/search/all",data=form)
    assert response.status_code == 302
    run_id = int(response.location.rsplit('/',1)[1])
    run = db.get_run(run_id)
    assert (run["window_start"],run["window_end"]) == (start,end)
    assert "Publication window:" in client.get(response.location).text
    database.execute("UPDATE search_runs SET status='partial' WHERE id=%s",(run_id,))
    retry = client.post(f"/setup/runs/{run_id}/retry",data={"csrf_token":"token","stage":"search"})
    assert retry.status_code == 302
    repeated = db.get_run(int(retry.location.rsplit('/',1)[1]))
    assert (repeated["window_start"],repeated["window_end"]) == (start,end)


def test_report_translation_audit_releases_connection_before_inference(database, monkeypatch):
    from ra import headline_review
    _, run_id, article = seed(database)
    opened = []
    @contextmanager
    def connection():
        opened.append(True)
        try:
            yield database
        finally:
            opened.pop()
    monkeypatch.setattr(db, 'connect', connection)
    def audit(*args):
        assert not opened, 'Model inference must not hold a database transaction'
        return {'status': 'faithful'}
    monkeypatch.setattr(headline_review, 'audit', audit)
    locks = []
    original_lock = score_store._lock
    def lock(conn):
        locks.append(True)
        original_lock(conn)
    monkeypatch.setattr(score_store, '_lock', lock)
    db.set_report_selection(run_id, [article['id']], {article['id']: ('Correct English title', 'Publisher')})
    saved = score_store.get_article(article['id'])
    assert locks == [True] and saved['report_selected']
    assert saved['title_en'] == 'Correct English title' and saved['headline_review']['status'] == 'faithful'


def test_report_edit_does_not_overwrite_translation_changed_during_audit(database, monkeypatch):
    from ra import headline_review
    _, run_id, article = seed(database)
    def audit(*args):
        database.execute("UPDATE search_results SET title_en='Concurrent correction' WHERE id=%s", (article['id'],))
        return {'status': 'faithful'}
    monkeypatch.setattr(headline_review, 'audit', audit)
    with pytest.raises(ValueError, match='translation changed during review'):
        db.set_report_selection(run_id, [article['id']], {article['id']: ('Stale correction', '')})
    saved = score_store.get_article(article['id'])
    assert saved['title_en'] == 'Concurrent correction' and not saved['report_selected']


def test_five_grade_migration_preserves_audits_decisions_and_recomputes_means(database):
    from ra.scoring import VERSION
    company, _, positive = seed(database, title="Acme Manufacturing completes recovery")
    _, _, limited = seed(database, title="Acme Manufacturing wins a contract", url="https://example.test/contract")
    _, _, excluded = seed(database, url="https://example.test/excluded")
    _, _, pending = seed(database, status="running", url="https://example.test/pending")
    # Emulate an existing seven-grade database, including its original constraints.
    for table, column in (("search_results", "score"), ("search_results", "score_auto"),
                          ("company_weekly_scores", "score"), ("company_weekly_scores", "mean_score")):
        database.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_{column}_five_grades")
        database.execute(f"ALTER TABLE {table} ADD CHECK ({column} BETWEEN -3 AND 3)")
    evidence = [{"score": 3, "rule": "completed recovery", "sentence": "Original source text"}]
    verification = {"status": "supported", "score": 3, "evidence": evidence, "reason": "Original source reason"}
    for item, score, included in ((positive, 3, True), (limited, 1, True), (excluded, -3, False)):
        database.execute("UPDATE search_results SET score=%s,score_auto=%s,score_status='reviewed',"
                         "score_included=%s,score_reason='Original analyst reason',score_version='source-credit-v5',"
                         "score_updated_at=%s WHERE id=%s",
                         (score, score, included, item["created_at"], item["id"]))
    database.execute("UPDATE search_results SET score_evidence=%s,verification=%s WHERE id=%s",
                     (db.Jsonb(evidence), db.Jsonb(verification), positive["id"]))
    database.execute("UPDATE search_results SET score=-3,score_auto=-3 WHERE id=%s", (pending["id"],))
    score_store._weekly(database)
    start = week_start(positive["published_at"])
    old_row, _, old_history = score_store.week_detail(company["id"], start)
    assert float(old_row["mean_score"]) == 2.0  # (3 + 1) / 2
    old_history_ids = {r["id"]: r["snapshot"] for r in old_history}
    score_store.refresh_scores()
    migrated = score_store.get_article(positive["id"])
    assert migrated["score"] == migrated["score_auto"] == 2
    assert migrated["score_evidence"] == [dict(evidence[0], score=2)]
    assert migrated["verification"] == dict(verification, score=2, evidence=[dict(evidence[0], score=2)])
    for item in (positive, limited, excluded):
        row = score_store.get_article(item["id"])
        assert row["score_reason"] == "Original analyst reason"
        assert row["score_status"] == "reviewed" and row["score_version"] == VERSION
        assert row["score_updated_at"] == item["created_at"]
    assert not score_store.get_article(excluded["id"])["score_included"]
    assert score_store.get_article(excluded["id"])["score"] == -2
    assert score_store.get_article(pending["id"])["score"] == -2
    assert score_store.get_article(limited["id"])["score"] == 1
    row, _, revisions = score_store.week_detail(company["id"], start)
    assert float(row["mean_score"]) == 1.5  # Rebuild contributions, never clamp the old mean.
    assert {r["id"]: r["snapshot"] for r in revisions if r["id"] in old_history_ids} == old_history_ids
    audit = score_store.article_history(positive["id"])[0]
    assert audit["previous"]["score"] == 3 and audit["assessment"]["score"] == 2
    source_audit = database.execute("SELECT * FROM article_verification_history WHERE result_id=%s", (positive["id"],)).fetchone()
    assert source_audit["previous"] == verification
    assert score_store.refresh_scores() == {"articles_updated": 0, "weeks_updated": 0}
    assert len(score_store.article_history(positive["id"])) == 1
    assert len(score_store.week_detail(company["id"], start)[2]) == len(revisions)
    for table, column in (("search_results", "score"), ("search_results", "score_auto"),
                          ("company_weekly_scores", "score"), ("company_weekly_scores", "mean_score")):
        for retired in (-3, 3):
            with pytest.raises(psycopg.errors.CheckViolation):
                with database.transaction():
                    database.execute(f"UPDATE {table} SET {column}=%s", (retired,))
    # The previous values required for rollback remain intact in both audit streams.
    with database.transaction(force_rollback=True):
        database.execute("ALTER TABLE search_results DROP CONSTRAINT search_results_score_five_grades")
        database.execute("ALTER TABLE search_results DROP CONSTRAINT search_results_score_auto_five_grades")
        score_store._write(database, positive["id"], audit["previous"])
        database.execute("UPDATE search_results SET verification=%s WHERE id=%s",
                         (db.Jsonb(source_audit["previous"]), positive["id"]))
        restored = score_store.get_article(positive["id"])
        assert restored["score"] == 3 and restored["verification"] == verification


def test_five_grade_migration_rolls_back_atomically(database, monkeypatch):
    _, _, article = seed(database)
    score_store.refresh_scores()
    score_store.review_score(article["id"], -1, True, "Reviewed original source")
    database.execute("UPDATE search_results SET score_version='source-credit-v5' WHERE id=%s", (article["id"],))
    before = score_store.get_article(article["id"])
    history = score_store.article_history(article["id"])
    def fail(conn):
        raise RuntimeError("Injected constraint failure")
    monkeypatch.setattr(score_store, "_constrain_five_grades", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        with database.transaction():
            score_store.refresh_scores()
    assert score_store.get_article(article["id"]) == before
    assert score_store.article_history(article["id"]) == history
