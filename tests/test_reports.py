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
    row = [cell.value for cell in book["Selected leads"][2]]
    assert row[0] == "2026-10-30"
    assert row[5:7] == ["Fato Relevante", "Material Fact"]
    assert row[3:5] == ["Heringer RI", "Heringer RI"]
