from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook

from ra.reports import make_xlsx


def test_selected_export_preserves_bilingual_text_and_uses_real_utc():
    moscow = timezone(timedelta(hours=3))
    run = {
        "id": 8, "created_at": datetime(2026, 10, 31, 17, 29, tzinfo=moscow),
        "scope": "selected", "status": "completed", "completed_companies": 1,
    }
    lead = {
        "published_at": datetime(2026, 10, 31, 0, 30, tzinfo=moscow),
        "subject_name": "Fertilizantes Heringer", "source_kind": "official",
        "publisher": "Heringer RI", "publisher_en": "Heringer RI",
        "title": "Fato Relevante", "title_en": "Material Fact",
        "signal_terms": [], "seen_before": False, "language": "pt-BR",
        "url": "https://example.com/notice", "query": "official",
    }
    book = load_workbook(make_xlsx(run, [lead], [], selected=True), read_only=True)
    assert book["Summary"]["B4"].value == "2026-10-31 14:29 UTC"
    assert "Article sentiment (-1 to +1)" in [cell.value for cell in book["Selected leads"][1]]
    row = [cell.value for cell in book["Selected leads"][2]]
    assert row[0] == "2026-10-30"
    assert row[5:7] == ["Fato Relevante", "Material Fact"]
    assert row[3:5] == ["Heringer RI", "Heringer RI"]


def test_company_report_windows_ranking_and_sparse_history():
    from ra.company_report import build_report, periods
    from ra.company_exports import company_pdf, company_xlsx
    from pypdf import PdfReader
    now = datetime(2024, 3, 31, 13, tzinfo=timezone.utc)
    run = {"id": 1, "created_at": now, "status": "completed"}
    company = {"name": "Company Alpha", "country_registration": "Brazil", "primary_industries": "Fertilizers"}
    base = {"title": "Company Alpha suspends production", "published_at": now, "publisher": "Example",
            "language": "en", "score": -1, "score_included": True, "event_group": "one", "verification": {"status": "supported"}}
    articles = [{**base, "id": i, "url": f"https://example.test/{i}", **extra} for i, extra in enumerate([
        {}, {}, {"published_at": now-timedelta(days=7)}, {"published_at": now+timedelta(seconds=1)},
        {"published_at": None}, {"title": "Brazil raises tariffs on fertilizer", "score_included": False, "score": None,
                                 "verification": {}, "event_group": "two", "exposure_paths": ["Sector · fertilizer", "Country · brazil"]}])]
    metrics = dict(search_count=1, source_errors=0, negative_count=0, positive_count=1, neutral_count=0, event_count=1, score=2)
    report = build_report(run, company, articles, {"Current": metrics, "1 week ago": dict(metrics, score=0, neutral_count=1, positive_count=0)},
                          [], {"1 week ago": (now-timedelta(days=7), [])})
    assert len(report["top"]) == 2 and len(report["sources"]) == 3
    assert report["top"][0]["state"] == "supported"
    assert "company 0 to 2" in report["comparisons"][0][1] and "moved +2" in report["comparisons"][0][1]
    assert "No archived" in report["comparisons"][1][1]
    assert dict(periods(now))["1 month ago"].day == 29
    assert report["ratings"][0][1] == "2024-04-01"
    book = load_workbook(company_xlsx(report))
    assert book["Sources"].max_row == 4 and book["Sources"]["J2"].hyperlink.target.startswith("https://")
    text = " ".join(p.extract_text() for p in PdfReader(company_pdf(report)).pages)
    assert "Company Alpha" in text and "No snapshot" in text and "12 months ago" in text
    assert "Cutoff" in text and "preceding seven days" in text


def test_filing_dates_respect_issuer_cutoff_revisions_and_export_links():
    from ra.company_report import build_report
    from ra.company_exports import company_pdf, company_xlsx
    from ra.watch_dates import upcoming
    from pypdf import PdfReader
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    company = {"country_registration": "Brazil", "tax_id": "22266175000188", "name": "Fertilizantes Heringer"}
    event, = upcoming(company, now)
    assert str(event["day"]) == "2026-11-12"
    assert not upcoming({**company, "tax_id": "123"}, now)
    assert not upcoming(company, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert not upcoming(company, datetime(2026, 11, 13, tzinfo=timezone.utc))
    for kind in ("Debt maturity", "Covenant test"):
        changed = dict(event, kind=kind, event_date="2026-12-01", filed_at="2026-11-04T12:00:00Z")
        cancelled = dict(changed, filed_at="2026-11-05T12:00:00Z", event_date=None)
        assert upcoming(company, now, [changed, cancelled, event])[0]["event_date"] == "2026-12-01"
        assert not upcoming(company, now+timedelta(days=1), [cancelled, event, changed])
    report = build_report({"id": 1, "status": "completed", "created_at": now}, company, [], {}, [])
    ws = load_workbook(company_xlsx(report)).active
    assert any(c.hyperlink and c.hyperlink.target == event["source_url"] and "12 Nov 2026" in c.value
               for c in ws["A"])
    reader = PdfReader(company_pdf(report))
    assert "12 Nov 2026" in reader.pages[0].extract_text()
    assert event["source_url"] in [ann.get_object().get("/A", {}).get("/URI")
                                  for page in reader.pages for ann in page.get("/Annots", [])]


def test_legacy_report_scale_is_labelled_and_never_compared_to_five_grades():
    from ra.company_report import build_report, rating_rows, driver
    from ra.scoring import VERSION
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    metrics = dict(search_count=1, source_errors=0, negative_count=0, positive_count=1,
                   neutral_count=0, event_count=1, score=3, version="source-credit-v5",
                   contributions=[dict(score=3, reason="Completed recovery", url="https://example.test")])
    run = dict(id=1, created_at=now, status="completed")
    report = build_report(run, {"name": "Acme"}, [],
                          {"Current": dict(metrics, score=2, version=VERSION), "1 week ago": metrics}, [])
    assert "different grading scales" in report["comparisons"][0][1]
    assert "Sentiment moved" not in report["comparisons"][0][1]
    assert list(rating_rows(report))[1][2] == 3
    assert "original scale" in list(rating_rows(report))[1][3]
    old = build_report(run, {"name": "Acme"}, [], {"Current": metrics}, [])
    assert "original scale" in driver(old)[0]
    assert metrics["score"] == 3
