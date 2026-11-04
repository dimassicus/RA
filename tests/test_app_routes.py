from datetime import datetime, timezone

from ra import db, verification_store
from ra.app import create_app


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
