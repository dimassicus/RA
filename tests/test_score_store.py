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
    assert next(r for r in rows if r["week_start"] == week_start(article["published_at"]))["score"] == -3
    assert score_store.weekly_history(empty["id"])[0]["score"] is None
    assert score_store.refresh_scores() == {"articles_updated": 0, "weeks_updated": 0}


def test_review_and_repeat_discovery_preserve_decision_and_weekly_audit(database):
    company, run_id, article = seed(database)
    seed(database)  # Same URL discovered in another run.
    score_store.refresh_scores()
    start = week_start(article["published_at"])
    before, _, history = score_store.week_detail(company["id"], start)
    assert before["metrics"]["article_count"] == 1
    score_store.review_score(article["id"], 2, True, "Source confirms completed recovery, headline was misleading")
    after, _, revisions = score_store.week_detail(company["id"], start)
    assert after["score"] == 2
    assert len(revisions) == len(history) + 1
    assert revisions[-1]["snapshot"]["score"] == -3
    assert revisions[-1]["snapshot"]["contributions"][0]["score"] == -3
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
    assert any(r["score"] == 2 for r in score_store.weekly_history(company["id"]))


def test_scoring_pages_api_and_csrf_protected_correction(database):
    company, run_id, article = seed(database)
    score_store.refresh_scores()
    with create_app().test_client() as client:
        assert client.get('/scores').status_code == 200
        assert client.get('/scores?weeks=bad').status_code == 400
        response = client.get(f'/runs/{run_id}?view=scored')
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
