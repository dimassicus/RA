from datetime import datetime, timedelta

from ra import db, keywords, monthly_backfill as backfill, search_settings, worker
from ra.scoring import WEEK_TIMEZONE
from test_score_store import database

SUBJECT = dict(name="Acme Industries", short_name="Acme Industries", country_registration="Indonesia",
               top_management="Jane Smith (CEO)", key_owners="Alex Owner (main shareholder)")
START = datetime(2026, 1, 15, 12, tzinfo=WEEK_TIMEZONE)
END = datetime(2026, 3, 8, 13, tzinfo=WEEK_TIMEZONE)


def test_month_bounds_use_run_calendar_and_clip_partial_months():
    gaps = dict(backfill.missing_months(START, END, {"2026-02"}))
    assert gaps == {"2026-01": (START, datetime(2026, 2, 1, tzinfo=WEEK_TIMEZONE) - timedelta(microseconds=1)),
                    "2026-03": (datetime(2026, 3, 1, tzinfo=WEEK_TIMEZONE), END)}


def test_archive_queries_use_both_people_and_company_anchored_approved_titles(monkeypatch):
    monkeypatch.setattr(keywords, "_rows", [
        dict(english="major shareholder", direction="neutral", enabled=True, translations={},
             phrases=[dict(english="main shareholder", translations={"id": "pemegang saham utama"})]),
        dict(english="chief executive officer", direction="neutral", enabled=True, translations={}),
        dict(english="smuggling", direction="adverse", enabled=True, translations={"id": "penyelundupan"}),
    ])
    for provider in ("Brave", "thenewsapi"):
        queries = backfill.archive_queries(SUBJECT, provider)
        assert any('"Jane Smith"' in q and '"Alex Owner"' in q for q in queries)
        assert all(any('"Acme Industries"' in q and f'"{term}"' in q for q in queries)
                   for term in ("CEO", "main shareholder", "pemegang saham utama", "penyelundupan"))
        assert max(map(len, queries)) <= 200


def test_brave_requires_in_window_publisher_date_and_reuses_lookups(monkeypatch):
    hit = dict(url="https://news.test/right", title="Acme Industries faces a lawsuit", description="",
               page_age="2026-01-18", language="en")
    hits = [hit, dict(hit, url="https://news.test/wrong-year"), dict(hit, url="https://news.test/unknown"),
            dict(hit, url="https://news.test/unrelated", title="Other company files for bankruptcy")]
    monkeypatch.setattr(backfill.recovery_providers, "request", lambda *args: {"web": {"results": hits}})
    calls = []
    def date(url):
        calls.append(url)
        return {hits[0]["url"]: START, hits[1]["url"]: START.replace(year=2025)}.get(url)
    monkeypatch.setattr(backfill, "publisher_date", date)
    cache, undated = {}, set()
    for _ in range(2):
        rows = backfill.brave_search("Acme", (START, END), SUBJECT, cache, set(), undated)
        assert [r["url"] for r in rows] == [hit["url"]]
    assert len(calls) == 3 and undated == {hits[2]["url"]}


def test_worker_fills_only_months_without_retained_company_leads(database, monkeypatch):
    search_settings.save(search_settings.DEFAULTS | dict(include_country=False, include_industry=False, adverse_only=True))
    rid = db.create_run("one_off", [SUBJECT], window_start=START, window_end=END)
    def article(title, url, stamp):
        return dict(title=title, url="https://test.example/" + url, published_at=stamp, language="en",
                    publisher="Test News", query="Acme", signal_terms=[])
    jan = article("Acme Industries files for bankruptcy", "jan", START)
    feb = article("Unrelated Ltd files for bankruptcy", "feb", START.replace(month=2))
    march = article("Alex Owner convicted of fraud", "march", END)
    monkeypatch.setattr(backfill.recovery_providers, "configured", lambda _: True)
    monkeypatch.setattr(backfill, "archive_queries", lambda *a: ['"Alex Owner"'])
    calls = []
    def fetch(query, bounds, *args):
        calls.append(bounds[0].month)
        return [march, march] if bounds[0].month == 3 else []
    monkeypatch.setattr(backfill, "brave_search", fetch)
    monkeypatch.setattr(backfill.news_apis, "configured_providers", lambda: [])
    worker.process_run(rid, searcher=lambda _: ([jan, feb], []), topic_searcher=lambda *a, **kw: ([], ""))
    run = db.get_run(rid)
    assert calls == [2, 3] and run["result_count"] == 2 and run["candidates_in_window"] == 3
    assert run["monthly_backfill"]["2026-02"]["added"] == 0
    assert run["monthly_backfill"]["2026-03"]["added"] == 1
    assert run["status"] == "completed"


def test_paid_provider_budget_failure_still_searches_later_months_with_other_provider(monkeypatch):
    gaps = dict(backfill.missing_months(START, END, {}))
    monkeypatch.setattr(backfill.recovery_providers, "configured", lambda _: True)
    monkeypatch.setattr(backfill, "archive_queries", lambda *args: ["first", "second"])
    def exhausted(*args):
        raise ValueError("Brave daily request limit reached")
    monkeypatch.setattr(backfill, "brave_search", exhausted)
    calls = []
    def fetch(provider, subject, *, queries, window, first_page_only):
        calls.append((queries[0], window[0].month))
        assert first_page_only
        return []
    monkeypatch.setattr(backfill.news_apis, "search", fetch)
    rows, notes, errors = backfill.recover(dict(SUBJECT, _news_providers=["thenewsapi"]), gaps, set(), lambda _: None)
    assert not rows and len(errors) == 1 and "limit reached" in errors[0]
    assert calls == [(q, m) for q in ("first", "second") for m in (1, 2, 3)]
    assert all(note["requests"] == 2 for note in notes.values())
