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
    worker.process_run(run_id, searcher=searcher, topic_searcher=lambda *args, **kwargs: [])
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


def test_keyword_edit_translate_and_disable_in_review(database, monkeypatch):
    from ra import keywords, translation
    monkeypatch.setattr(keywords, "_rules", keywords._rules)
    monkeypatch.setattr(keywords, "schedule", lambda: None)
    monkeypatch.setattr(translation, "installed_keyword_translations", lambda terms, code: {})
    def offline(*args):
        raise keywords.ProviderError("offline test")
    monkeypatch.setattr(keywords.online_translation, "translate", offline)
    term = "covenant breach"
    def model(prompt, data, schema):
        if "english" in schema["properties"]:
            return {"english": True}
        if schema["properties"][term]["type"] == "boolean":
            return {term: data["translations"][term] == "violação de cláusula"}
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
    company = dict(company, tax_id='22266175000188')
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
    def model(prompt, data, schema):
        if "english" in schema["properties"]:
            return {"english": True}
        if "translations" in data:
            return {text: data["translations"][text] == good}
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
