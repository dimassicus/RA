from datetime import datetime, timezone

import pytest

from ra import company_developments as events, idx_filings, risk_factors, search_settings
from ra.relevance import classify
from ra.search import governance_searches

COMPANY = dict(name="PT Lautan Luas Tbk", short_name="Lautan Luas", aliases="LTLS", country_registration="Indonesia")
WINDOW = (datetime(2026, 5, 1, tzinfo=timezone.utc), datetime(2026, 11, 10, tzinfo=timezone.utc))


@pytest.fixture
def factors(monkeypatch):
    rows = [dict(english=k, enabled=True, scope="company", translations={}) for k in events.TERMS]
    monkeypatch.setattr(risk_factors, "_rows", rows)
    return rows


@pytest.mark.parametrize("title,key", [
    ("Court freezes seven assets belonging to Lautan Luas", events.ASSETS),
    ("LTLS Menggugat, 5 dari 7 Aset Lautan Luas Resmi Dikembalikan", events.ASSETS),
    ("PT Lautan Luas Tbk — Penetapan Permohonan Keberatan", events.ASSETS),
    ("Lautan Luas (LTLS) Siapkan Belanja Rp 500 Miliar", events.CAPEX),
    ("BRMS Pacu Produksi Emas, LTLS Bangun Pabrik Baru, BIPP Tambah Modal Rp 100 Miliar", events.CAPEX),
    ("Strategi Lautan Luas Perluas Pasar FiberCreme, dari Dapur Keluarga hingga Industri", events.STRATEGY),
    ("Lautan Luas plans to invest in a new plant", events.CAPEX),
    ("Lautan Luas expands into a new market", events.STRATEGY),
])
def test_developments_survive_adverse_filter_without_assigning_direction(factors, title, key):
    article = dict(title=title, language="id", verification={}, score_status="needs_review", score_included=False)
    assert search_settings.adverse_lead(article, COMPANY)
    display = classify(article, COMPANY)
    assert display["credit_developments"] == [events.PARTIAL_RELIEF if "5 dari 7" in title else events.LABELS[key]]
    assert display["relevance"] == "review" and not display["score_included"]


def test_development_requires_attribution_and_an_enabled_factor(factors):
    assert not events.review_topics(dict(title="LTLS reports earnings, BIPP plans to invest in a new plant"), COMPANY)
    assert not events.review_topics(dict(title="Lautan Luas wins award", summary="Another issuer plans to invest."), COMPANY)
    for f in factors:
        f["enabled"] = False
    assert not events.review_topics(dict(title="Lautan Luas plans to invest"), COMPANY)
    assert not any("siapkan belanja" in q for q, e in governance_searches(COMPANY))


def test_filing_index_validates_issuer_pdf_and_publication_window(factors):
    def row(date, url, title):
        return f'<li><span>{date}</span><span>Lainnya</span><a href="{url}">{title}</a></li>'
    prefix = "https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/"
    page = ('<h1><span>LTLS</span> · LAUTAN LUAS Tbk, PT</h1><section id="pengumuman">'
            + row("3 Jul 2026", prefix + "202607/july.pdf", "Penetapan Permohonan Keberatan")
            + row("20 Apr 2026", prefix + "202604/april.pdf", "Aset Perseroan")
            + row("3 Jul 2026", "https://other.example/press.pdf", "Wrong source") + "</section>")
    rows, gaps = idx_filings.collect(COMPANY, lambda u: page.encode(), WINDOW)
    assert not gaps and [r["url"] for r in rows] == [prefix + "202607/july.pdf"]
    assert search_settings.adverse_lead(rows[0], COMPANY)
    rows, gaps = idx_filings.collect(COMPANY, lambda u: page.replace("LAUTAN LUAS", "BUANA LAUTAN").encode(), WINDOW)
    assert not rows and "did not match" in gaps[0]


def test_partial_release_and_residual_adverse_exposure_are_both_visible(factors):
    article = dict(title="Five of seven Lautan Luas assets returned", score_status="reviewed", score=1,
                   score_included=True, verification={}, risk_factor_review={"matches": [
                       {"factor": events.ASSETS, "quote": "Two investment properties remain under seizure."}]})
    result = classify(article, COMPANY)
    assert set(result["credit_developments"]) == {events.PARTIAL_RELIEF, events.RESIDUAL_FREEZE}
    assert result["score"] == 1
    assert search_settings.adverse_lead(article, COMPANY)


@pytest.mark.parametrize("title,expected", [
    ("Lautan Luas: dua aset tetap disita", [events.RESIDUAL_FREEZE]),
    ("Lautan Luas secured release of five out of seven assets, but two remain frozen", [events.PARTIAL_RELIEF, events.RESIDUAL_FREEZE]),
    ("Lautan Luas litigation continues", [events.OPEN_CASE]),
    ("All seven of seven Lautan Luas assets returned", []),
    ("Lautan Luas properties may remain frozen", []),
    ("No Lautan Luas assets remain frozen", []),
    ("Lautan Luas wins release, BIPP properties remain frozen", []),
])
def test_only_reported_continuing_exposure_gets_adverse_label(factors, title, expected):
    topics = events.review_topics(dict(title=title), COMPANY)
    assert [v for v in topics if v != events.LABELS[events.ASSETS]] == expected
