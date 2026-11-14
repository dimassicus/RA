from datetime import datetime, timezone
import pytest

from ra import db, verification_store
from ra.app import create_app


@pytest.fixture(autouse=True)
def reviewer_offline(monkeypatch):
    from ra import headline_review
    monkeypatch.setattr(headline_review, "readiness", lambda: {"ready": False, "message": "Start LM Studio API"})


def test_saved_run_page_accepts_trailing_slash(monkeypatch):
    run = {
        "id": 5, "scope": "selected", "status": "completed",
        "total_companies": 1, "completed_companies": 1,
        "result_count": 0, "error_count": 0,
        "created_at": datetime(2026, 10, 31, tzinfo=timezone.utc),
        "subjects": [{"name": "Fertilizantes Heringer", "country_registration": "Brazil"}],
    }
    monkeypatch.setattr(db, "get_run", lambda run_id: run if run_id == 5 else None)
    monkeypatch.setattr(db, "get_results", lambda run_id: [])
    monkeypatch.setattr(db, "get_errors", lambda run_id: [])
    monkeypatch.setattr(verification_store, "job_summary", lambda run_id: {})
    with create_app().test_client() as client:
        assert client.get("/runs/5").status_code == 200
        assert client.get("/runs/5/").status_code == 200


@pytest.mark.parametrize("language,finished", [
    ("en", "Finished with partial coverage"),
    ("ru", "Поиск завершён с неполным охватом источников"),
])
def test_partial_run_header_and_live_activity_are_run_specific(monkeypatch, language, finished):
    from ra import i18n, local_models
    monkeypatch.setattr(i18n, "_lookup", lambda text, *_: text)
    run = dict(id=51, status="partial", scope="selected", subjects=[], created_at=datetime.now(timezone.utc),
               completed_companies=1, total_companies=1, result_count=0, error_count=1, event_analysis_status="complete")
    checks = dict(queued=0, running=0)
    monkeypatch.setattr(db, "get_run", lambda _: run)
    monkeypatch.setattr(db, "get_results", lambda _: [])
    monkeypatch.setattr(db, "get_errors", lambda _: [])
    monkeypatch.setattr(verification_store, "job_summary", lambda _: checks)
    client = create_app().test_client()
    client.set_cookie("ra_language", language)
    message = "Loading Gemma 26B A4B…"
    for owner in ("run 510", "individual request"):
        monkeypatch.setattr(local_models, "_activity", (owner, message))
        page = client.get("/runs/51").text
        assert f">{finished}</span>" in page and 'id="reviewer-status" role="status" hidden' in page
        data = client.get("/api/runs/51/status").json
        assert data["state_label"] == finished and data["reviewer"]["message"] == ""

    checks["running"] = 1
    monkeypatch.setattr(local_models, "_activity", ("run 51", message))
    active = client.get("/api/runs/51/status").json
    assert active["state_label"] != finished and "Gemma" in active["reviewer"]["message"]
    assert active["reviewer"]["message"] in client.get("/runs/51").text
    checks["running"] = 0
    run["event_analysis_status"] = "pending"
    assert client.get("/api/runs/51/status").json["state_label"] != finished


def test_ai_summary_orders_events_across_companies_and_their_sources(monkeypatch):
    def lead(identifier, company, day, group, decision=None, event_day=None):
        draft = dict(event=group, title=f"Brief {group}", summary="Summary")
        if decision:
            draft['decision'] = dict(status='documented', decision_day=decision)
        if event_day:
            draft['event_day'] = event_day
        return dict(id=identifier, subject_name=company, subject_key=company, title=f"Source {identifier}",
                    event_draft=draft, event_group=str(group), url=f"https://example.test/{identifier}",
                    published_at=datetime(2026, 7, day, tzinfo=timezone.utc) if day else None,
                    score=0, score_included=True, score_status='provisional', report_selected=False,
                    publisher='News', language='en', verification={}, source_kind='news')
    rows = [lead(4, 'Alpha', None, 4), lead(3, 'Zulu', 6, 2), lead(1, 'Alpha', 5, 1, '2026-07-01'),
            lead(2, 'Zulu', 2, 2), lead(5, 'Alpha', 9, 5, event_day='2026-06-30')]
    run = dict(id=5, scope='selected', status='completed', total_companies=2, completed_companies=2,
               result_count=len(rows), error_count=0, created_at=rows[2]['published_at'], event_analysis_status='complete',
               subjects=[dict(name='Alpha', country_registration='Brazil'), dict(name='Zulu', country_registration='Japan')])
    monkeypatch.setattr(db, 'get_run', lambda _: run)
    monkeypatch.setattr(db, 'get_results', lambda _: rows)
    monkeypatch.setattr(db, 'get_errors', lambda _: [])
    monkeypatch.setattr(verification_store, 'job_summary', lambda _: {})
    with create_app().test_client() as client:
        page = client.get('/runs/5')
        assert page.status_code == 200
        summary = page.get_data(as_text=True).split('AI summary for search results', 1)[1].split('id="analyst-review"', 1)[0]
        labels = ['Brief 5', 'Brief 1', 'Brief 2', 'Source 2', 'Source 3', 'Date unknown', 'Brief 4']
        assert [summary.index(label) for label in labels] == sorted(summary.index(label) for label in labels)
        review = page.get_data(as_text=True).split('id="summary-leads"', 1)[1].split('id="other-leads"', 1)[0]
        assert 'name="accept_summary" value="5,1,2,3,4"' in review
        groups = review.split('<div class="event-choice">')[1:]
        expected = [(5, [5]), (1, [1]), (2, [2, 3]), (4, [4])]
        assert len(groups) == len(expected)
        for index, (group, (event, ids)) in enumerate(zip(groups, expected), 1):
            assert f'aria-controls="event-leads-{index}"' in group
            assert f'<table id="event-leads-{index}">' in group
            assert f'<summary><strong>Brief {event}</strong>' in group
            assert group.count('name="selected_ids"') == len(ids)
            positions = [group.index(f'aria-label="Include lead {i} in report"') for i in ids]
            assert positions == sorted(positions)


def test_risk_view_hides_noise_and_preserves_other_view_selections(monkeypatch):
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    run = {"id": 5, "scope": "selected", "status": "completed", "total_companies": 1,
           "completed_companies": 1, "result_count": 2, "error_count": 0,
           "created_at": now, "subjects": [{"name": "Acme Manufacturing"}]}
    common = {"subject_name": "Acme Manufacturing", "source_kind": "news", "published_at": now,
              "url": "https://example.test/news", "publisher": "News", "publisher_en": "News",
              "language": "en", "title_en": "", "translation_provider": "", "seen_before": False, "verification": {"status": "supported"}, "score": -1,
              "score_included": True, "score_status": "provisional", "score_reason": "Source-supported bankruptcy", "event_group": "event:1"}
    results = [{**common, "id": 1, "title": "Acme Manufacturing faces bankruptcy", "report_selected": False},
               {**common, "id": 2, "title": "National team wins cup", "report_selected": True, "verification": {"status":"unmatched", "reason":"Company not in source"}, "score_included":False}]
    monkeypatch.setattr(db, "get_run", lambda run_id: run)
    monkeypatch.setattr(db, "get_results", lambda run_id: results)
    monkeypatch.setattr(db, "get_errors", lambda run_id: [])
    monkeypatch.setattr(verification_store, "job_summary", lambda run_id: {})
    with create_app().test_client() as client:
        body = client.get("/runs/5?view=risk").get_data(as_text=True)
        assert "Acme Manufacturing faces bankruptcy" in body
        assert "National team wins cup" not in body
        assert 'name="selection_baseline" value=""' in body
        assert "National team wins cup" in client.get("/runs/5?view=all").get_data(as_text=True)
        assert client.get("/runs/5?view=invalid").status_code == 400


def test_watchlist_country_and_sector_filters(monkeypatch):
    from ra import worker
    companies = [
        {"id": 1, "name": "One", "country_registration": "Brazil", "primary_industries": "Fertilizers"},
        {"id": 2, "name": "Two", "country_registration": "Vietnam", "primary_industries": "Fertilizer"},
        {"id": 3, "name": "Three", "country_registration": "Greece", "primary_countries": "Brasil", "primary_industries": "Aluminium and Copper"},
    ]
    recorded = []
    monkeypatch.setattr(db, "list_companies", lambda active: companies if active else [])
    monkeypatch.setattr(db, "create_run", lambda scope, subjects, **window: recorded.append((scope, [s["id"] for s in subjects])) or 4)
    monkeypatch.setattr(worker, "enqueue", lambda run_id: None)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    for filters, expected in [({}, [1, 2, 3]), ({"country": "brazil"}, [1, 3]), ({"sector": "fertilizer"}, [1, 2]),
                              ({"country": "brazil", "sector": "copper"}, [3])]:
        assert client.post("/search/all", data={"csrf_token": "token", **filters}).status_code == 302
        assert recorded[-1] == ("selected" if filters else "all", expected)
    with client.session_transaction() as session:
        assert ("warning", "Start LM Studio API") in session["_flashes"]
    client.post("/search/all", data={"csrf_token": "token", "country": "missing"})
    assert len(recorded) == 4


def test_non_ascii_form_token_is_rejected_without_server_error():
    with create_app().test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "token"
        assert client.post("/companies/import", data={"csrf_token": "токен"}).status_code == 400


def test_historical_cutoff_excludes_selected_date_and_default_stays_rolling(monkeypatch):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from ra import worker
    calls = []
    monkeypatch.setattr(db, "list_companies", lambda active: [{"id": 1, "name": "Company"}])
    monkeypatch.setattr(db, "create_run", lambda scope, subjects, **window: calls.append(window) or 10)
    monkeypatch.setattr(worker, "enqueue", lambda run_id: None)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = {"csrf_token": "token"}
    assert client.post("/search/all", data=form).status_code == 302
    assert calls[-1] == {"window_start": None, "window_end": None}
    for cutoff in ("2026-10-12", "2026-11-09", "2026-11-10"):
        assert client.post("/search/all", data=form | {"cutoff_date": cutoff}).status_code == 302
        end = datetime.fromisoformat(cutoff).replace(tzinfo=ZoneInfo("Europe/Moscow"))
        assert calls[-1] == {"window_start": end - timedelta(days=7), "window_end": end - timedelta(microseconds=1)}
    assert client.post("/search/all", data=form | {"cutoff_date": "bad"}).status_code == 400
    assert client.post("/search/all", data=form | {"cutoff_date": "2026-10-12", "window_start": "2026-10-05T00:00:00+03:00"}).status_code == 400


def test_inclusive_end_date_keeps_selected_day_and_caps_today_at_launch(monkeypatch):
    from zoneinfo import ZoneInfo
    from ra import app, worker
    now = datetime(2026, 11, 10, 15, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)
    monkeypatch.setattr(app, "datetime", Clock)
    calls = []
    monkeypatch.setattr(db, "list_companies", lambda active: [{"id": 1, "name": "Company"}])
    monkeypatch.setattr(db, "create_run", lambda scope, subjects, **window: calls.append(window) or 10)
    monkeypatch.setattr(worker, "enqueue", lambda run_id: None)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "token"
    form = {"csrf_token": "token", "end_date": "2026-11-10"}
    assert client.post("/search/all", data=form).status_code == 302
    assert calls[-1] == {"window_start": now.replace(day=4, hour=0, minute=0), "window_end": now}
    assert client.post("/search/all", data=form | {"end_date": "2026-11-04"}).status_code == 302
    assert calls[-1] == {"window_start": now.replace(month=10, day=29, hour=0, minute=0),
                         "window_end": now.replace(day=4, hour=23, minute=59, second=59, microsecond=999999)}
    for extra in ({"end_date": "2026-11-11"}, {"end_date": "bad"},
                  {"cutoff_date": "2026-11-10"}, {"window_end": now.isoformat()}):
        assert client.post("/search/all", data=form | extra).status_code == 400
    assert len(calls) == 2


@pytest.mark.parametrize("path", ["/keywords", "/risk-factors"])
def test_settings_render_languages_without_babel_locales(monkeypatch, path):
    from ra import keywords, risk_factors
    catalog = risk_factors if path == "/risk-factors" else keywords
    monkeypatch.setattr(catalog, "languages", lambda: ["av", "ru", "unknown"])
    monkeypatch.setattr(catalog, "records", lambda: [
        {"id": 1, "english": "Factory closes", "scope": "company", "direction": "adverse",
         "enabled": True, "translations": {"ru": "Закрытие завода"}},
    ])
    with create_app().test_client() as client:
        response = client.get(path)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Avaric" in body and "Russian" in body and "unknown" in body
        assert 'id="translation-progress"' in body
        assert "translation-progress.js" in body
        assert ('data-character-limit="200"' in body) == (path == "/risk-factors")


@pytest.mark.parametrize("factors", ["0", "1"])
def test_live_translation_status_counts_only_enabled_terms_and_target_languages(monkeypatch, factors):
    from ra import keywords, risk_factors, local_models
    catalog = risk_factors if factors == "1" else keywords
    monkeypatch.setattr(catalog, "languages", lambda: ["ru", "av"])
    monkeypatch.setattr(catalog, "records", lambda: [
        {"enabled": True, "translations": {"ru": "Текст", "es": "Texto"}},
        {"enabled": True, "translations": {}},
        {"enabled": False, "translations": {"ru": "Текст", "av": "Text"}},
    ])
    monkeypatch.setattr(keywords, "translation_status", lambda: {
        "running": True, "retry_requested": True, "message": "Checking translations",
        "elapsed_seconds": 12, "error": "",
    })
    monkeypatch.setattr(local_models, "activity", lambda: "Waiting for another local-model task")
    with create_app().test_client() as client:
        response = client.get("/api/translations/status?factors=" + factors)
        assert response.status_code == 200
        data = response.get_json()
        assert (data["saved"], data["total"], data["missing"]) == (1, 4, 3)
        assert data["running"] and data["retry_requested"]
        assert data["elapsed_seconds"] == 12 and "Waiting" in data["model_activity"]


@pytest.mark.parametrize('path', ['/search/selected', '/search/one-off'])
def test_single_company_custom_inclusive_range(monkeypatch, path):
    from zoneinfo import ZoneInfo
    from ra import app, worker
    now = datetime(2026, 11, 10, 15, 30, tzinfo=ZoneInfo('Europe/Moscow'))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)
    monkeypatch.setattr(app, 'datetime', Clock)
    monkeypatch.setattr(db, 'get_company', lambda _: dict(id=1, name='Acme', country_registration='Brazil', active=True))
    calls = []
    monkeypatch.setattr(db, 'create_run', lambda scope, subjects, **window: calls.append(window) or 99)
    monkeypatch.setattr(worker, 'enqueue', lambda _: None)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session['csrf_token'] = 'token'
    form = dict(csrf_token='token', company_id='1', name='Acme', country_registration='Brazil',
                start_date='2024-01-01', end_date='2026-11-10')
    if path == "/search/one-off":
        assert client.post(path, data=form).status_code == 410
        assert not calls
        return
    assert client.post(path, data=form).status_code == 302
    assert calls[-1] == dict(window_start=now.replace(year=2024, month=1, day=1, hour=0, minute=0), window_end=now)
    assert client.post(path, data=form | {'start_date': '2026-10-04', 'end_date': '2026-10-04'}).status_code == 302
    assert calls[-1]['window_start'] == now.replace(month=10, day=4, hour=0, minute=0)
    assert calls[-1]['window_end'] == now.replace(month=10, day=4, hour=23, minute=59, second=59, microsecond=999999)
    for invalid in ({'start_date': 'bad'}, {'start_date': '2026-11-11'}, {'end_date': ''},
                    {'end_date': '2026-11-11'}, {'cutoff_date': '2026-11-10'}, {'window_start': now.isoformat()}):
        assert client.post(path, data=form | invalid).status_code == 400
    assert len(calls) == 2


def test_weekly_scores_sort_by_visible_name_including_archived_history(monkeypatch):
    from ra import score_store
    names = [dict(id=1, name="AAA Holdings", short_name="Zulu", active=True),
             dict(id=2, name="Beta", active=True),
             dict(id=3, name="Public corporation", short_name="Álpha", active=False)]
    monkeypatch.setattr(db, "list_companies", lambda _: names)
    monkeypatch.setattr(score_store, "weekly_history", lambda: [
        dict(company_id=3, week_start=datetime(2025, 1, 6).date(), score=-1)])
    with create_app().test_client() as client:
        page = client.get("/scores")
        assert page.status_code == 200
        table = page.get_data(as_text=True).split('class="heatmap"', 1)[1]
        assert table.index(">Álpha<") < table.index(">Beta<") < table.index(">Zulu<")
