from datetime import datetime, timedelta, timezone

import pytest

from ra.scoring import aggregate_week, score_article, week_start, weekly_groups

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
SUBJECT = {"name": "Acme Manufacturing", "country_registration": "Brazil"}


def article(title, **extra):
    return {"title": title, "language": "en", "source_kind": "news", "published_at": NOW,
            "created_at": NOW, "url": "https://example.test/1", "id": 1, **extra}


@pytest.mark.parametrize("title,expected", [
    ("Acme Manufacturing files for bankruptcy", -1),
    ("Acme Manufacturing announces plant closure", -1),
    ("Acme Manufacturing CEO resigns", -1),
    ("Acme Manufacturing updates its logo", 0),
    ("Acme Manufacturing reduces losses", 1),
    ("Acme Manufacturing returns to profit", 1),
    ("Acme Manufacturing emerges from bankruptcy", 1),
    ("Acme Manufacturing losses narrowed", 1),
    ("Acme Manufacturing fully repays defaulted debt", 1),
    ("Acme Manufacturing: lucro cresce", 1),
    ("Acme Manufacturing: reduz prejuízo", 1),
    ("Acme Manufacturing: recuperação judicial", -1),
    ("Acme Manufacturing encerra recuperação judicial", 1),
    ("Acme Manufacturing sues supplier", 0),
    ("Acme Manufacturing announces a software upgrade", 0),
    ("Acme Manufacturing rating downgraded", -1),
])
def test_three_grades_and_directional_context(title, expected):
    result = score_article(article(title), SUBJECT, NOW)
    assert result["score"] == expected
    assert result["score_included"] is False  # Headlines alone no longer enter weekly history.
    assert result["score_status"] == "needs_review"


@pytest.mark.parametrize("changes", [
    {"report_selected": False}, {"event_draft": {"excluded": True, "sentiment": -1}},
    {"verification": {"status": "unmatched"}}, {"published_at": None},
    {"published_at": NOW + timedelta(days=1)}, {"source_kind": "context"},
    {"exposure_paths": ["sector"]}, {"event_draft": {"event": 1, "sentiment": 1}},
])
def test_selection_accepts_only_eligible_company_event_scores(changes):
    lead = article("Acme Manufacturing financial results", report_selected=True,
                   event_draft={"event": 1, "sentiment": -1}, verification={"status": "needs_review"})
    accepted = score_article(lead, SUBJECT, NOW)
    assert accepted["score"] == -1 and accepted["score_included"]
    assert not score_article(dict(lead, **changes), SUBJECT, NOW)["score_included"]


@pytest.mark.parametrize("title", [
    "Acme Manufacturing denies fraud and bankruptcy reports",
    "Acme Manufacturing may default",
    "Acme Manufacturing avoids bankruptcy",
    "Acme Manufacturing reports no losses",
    "Other company files for bankruptcy",
])
def test_uncertain_or_unrelated_zero_is_excluded(title):
    result = score_article(article(title), SUBJECT, NOW)
    assert result["score"] == 0
    assert not result["score_included"]
    assert result["score_status"] == "needs_review"


def test_missing_date_untranslated_language_and_clone_warning_are_excluded():
    for changes in ({"published_at": None}, {"published_at": NOW + timedelta(days=1)},
                    {"language": "az", "title": "Acme Manufacturing xəbər"},
                    {"discovery_service": "FCA Warning RSS", "source_kind": "official"}):
        result = score_article({**article("Acme Manufacturing files for bankruptcy"), **changes}, SUBJECT, NOW)
        assert not result["score_included"]


def test_company_specific_official_source_and_translation():
    result = score_article(article("Fato Relevante - Hibernação", language="pt", source_kind="official"), SUBJECT, NOW)
    assert result["score"] == -1
    result = score_article(article("Acme Manufacturing 财务消息", language="zh", title_en="Acme Manufacturing returns to profit"), SUBJECT, NOW)
    assert result["score"] == 1


def test_mixed_directions_do_not_add_repeated_terms():
    result = score_article(article("Acme Manufacturing rating upgraded but announces plant closure"), SUBJECT, NOW)
    assert result["score"] == -1
    assert "mixed directions" in result["score_reason"]


def test_publication_weeks_use_monday_in_moscow():
    assert str(week_start(datetime(2026, 11, 1, 20, 59, tzinfo=timezone.utc))) == "2026-10-26"
    assert str(week_start(datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc))) == "2026-11-02"


def assessed(value, **extra):
    fields = dict(score=value, report_selected=True, score_included=True, score_status="provisional", event_draft={"event":1})
    return article("Test", **(fields | extra))


def test_no_data_differs_from_neutral_and_uncertainty():
    assert aggregate_week([])["score"] is None
    neutral = aggregate_week([assessed(0)])
    assert neutral["mean_score"] == 0
    assert neutral["neutral_count"] == 1
    excluded = aggregate_week([article("Unknown", score=0, report_selected=True, score_included=False, score_status="needs_review")])
    assert excluded["score"] is None
    assert excluded["excluded_count"] == 1
    assert aggregate_week([assessed(-1, score_status="reviewed", event_draft={})])["event_count"] == 0


def test_weekly_rounding_extrema_and_offsetting_coverage():
    data = [assessed(-1), assessed(1, id=2, url="https://example.test/2")]
    result = aggregate_week(data)
    assert result["score"] == result["mean_score"] == -1
    assert (result["worst_score"], result["best_score"]) == (-1, -1)
    assert result["negative_count"] == 1 and result["positive_count"] == 0
    assert aggregate_week([assessed(-1), assessed(0, id=2, url="b")])["score"] == -1
    assert aggregate_week([assessed(1), assessed(0, id=2, url="b")])["score"] == 0


def test_repeated_url_counts_once_and_review_takes_precedence():
    data = [assessed(-1), assessed(-1, id=2), assessed(0, score_status="reviewed", id=3)]
    result = aggregate_week(data)
    assert result["article_count"] == result["event_count"] == 1
    assert result["mean_score"] == 0
    assert result["article_ids"] == [3]
    assert result["contributions"][0]["score"] == 0


def test_weekly_event_combines_selected_evidence_across_saved_runs():
    def lead(run_id, lead_id, score, **extra):
        return assessed(score, run_id=run_id, id=lead_id, company_id=1, event_group="event:1",
                        url=f"https://example.test/{lead_id}", **extra)

    same_document = {"document_hash": "identical-filing"}
    older = [lead(10, 1, -1, verification=same_document), lead(10, 2, -1), lead(10, 3, -1)]
    newer = [lead(11, 4, 0, verification=same_document, score_status="reviewed"), lead(11, 5, 0),
             lead(11, 6, -1, report_selected=False)]
    assert [aggregate_week(run)["score"] for run in (older, newer)] == [-1, 0]

    weeks = weekly_groups(older + newer)
    event, = aggregate_week(weeks[1, week_start(NOW)])["contributions"]
    assert event["score"] == -1
    assert event["article_ids"] == [2, 3, 4, 5]


def test_latest_analyst_exclusion_overrides_older_inclusion():
    older = {**assessed(-1), "score_status": "reviewed", "score_updated_at": NOW}
    newer = {**older, "id": 2, "score_included": False, "score_updated_at": NOW + timedelta(minutes=1)}
    result = aggregate_week([newer, older])
    assert result["score"] is None
    assert result["excluded_ids"] == [2]


def test_score_hash_changes_when_future_publication_becomes_eligible():
    row = article("Acme Manufacturing files for bankruptcy", verification={
        "status": "supported", "score": -3, "evidence": [], "reason": "Source confirms bankruptcy"})
    before = score_article(row, SUBJECT, NOW - timedelta(seconds=1))
    after = score_article(row, SUBJECT, NOW)
    later = score_article(row, SUBJECT, NOW + timedelta(days=1))
    assert not before["score_included"] and after["score_included"]
    assert before["score_input_hash"] != after["score_input_hash"]
    assert after["score_input_hash"] == later["score_input_hash"]


@pytest.mark.parametrize("old,new", [(-3, -1), (-2, -1), (2, 1), (3, 1)])
def test_cached_legacy_source_grades_map_without_mutating_evidence(old, new):
    from copy import deepcopy
    from ra.scoring import LABELS, RUBRIC, VERSION
    check = {"status": "supported", "score": old, "reason": "Confirmed source event",
             "evidence": [{"score": old, "sentence": "Original source text", "rule": "event"}]}
    original = deepcopy(check)
    result = score_article(article("Acme Manufacturing update", verification=check), SUBJECT, NOW)
    assert set(LABELS) == set(RUBRIC) == set(range(-1, 2))
    assert result["score"] == result["score_auto"] == new
    assert result["score_evidence"] == [dict(check["evidence"][0], score=new)]
    assert result["score_included"] and result["score_version"] == VERSION
    assert check == original
