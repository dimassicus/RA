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


def test_risk_view_hides_noise_and_preserves_other_view_selections(monkeypatch):
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    run = {"id": 5, "scope": "selected", "status": "completed", "total_companies": 1,
           "completed_companies": 1, "result_count": 2, "error_count": 0,
           "created_at": now, "subjects": [{"name": "Acme Manufacturing"}]}
    common = {"subject_name": "Acme Manufacturing", "source_kind": "news", "published_at": now,
              "url": "https://example.test/news", "publisher": "News", "publisher_en": "News",
              "language": "en", "title_en": "", "translation_provider": "", "seen_before": False, "verification": {"status": "supported"}, "score": -3,
              "score_included": True, "score_status": "provisional", "score_reason": "Source-supported bankruptcy", "event_group": "event:1"}
    results = [{**common, "id": 1, "title": "Acme Manufacturing faces bankruptcy", "report_selected": False},
               {**common, "id": 2, "title": "National team wins cup", "report_selected": True, "verification": {"status":"unmatched", "reason":"Company not in source"}, "score_included":False}]
    monkeypatch.setattr(db, "get_run", lambda run_id: run)
    monkeypatch.setattr(db, "get_results", lambda run_id: results)
    monkeypatch.setattr(db, "get_errors", lambda run_id: [])
    monkeypatch.setattr(verification_store, "job_summary", lambda run_id: {})
    with create_app().test_client() as client:
        body = client.get("/runs/5").get_data(as_text=True)
        assert "Acme Manufacturing faces bankruptcy" in body
        assert "National team wins cup" not in body
        assert 'type="hidden" name="selected_ids" value="2"' in body
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
