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
              "language": "en", "title_en": "", "translation_provider": "", "seen_before": False, "verification": {"status": "supported"}, "score": -2,
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
        assert ('data-character-limit="300"' in body) == (path == "/risk-factors")


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
