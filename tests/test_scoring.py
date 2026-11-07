from datetime import datetime, timedelta, timezone

import pytest

from ra.scoring import aggregate_week, score_article, week_start

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
SUBJECT = {"name": "Acme Manufacturing", "country_registration": "Brazil"}


def article(title, **extra):
    return {"title": title, "language": "en", "source_kind": "news", "published_at": NOW,
            "created_at": NOW, "url": "https://example.test/1", "id": 1, **extra}


@pytest.mark.parametrize("title,expected", [
    ("Acme Manufacturing files for bankruptcy", -3),
    ("Acme Manufacturing announces plant closure", -2),
    ("Acme Manufacturing CEO resigns", -1),
    ("Acme Manufacturing updates its logo", 0),
    ("Acme Manufacturing reduces losses", 1),
    ("Acme Manufacturing returns to profit", 2),
    ("Acme Manufacturing emerges from bankruptcy", 3),
    ("Acme Manufacturing losses narrowed", 1),
    ("Acme Manufacturing fully repays defaulted debt", 3),
    ("Acme Manufacturing: lucro cresce", 1),
    ("Acme Manufacturing: reduz prejuízo", 1),
    ("Acme Manufacturing: recuperação judicial", -2),
    ("Acme Manufacturing encerra recuperação judicial", 3),
    ("Acme Manufacturing sues supplier", 0),
    ("Acme Manufacturing announces a software upgrade", 0),
    ("Acme Manufacturing rating downgraded", -2),
])
def test_seven_steps_and_directional_context(title, expected):
    result = score_article(article(title), SUBJECT, NOW)
    assert result["score"] == expected
    assert result["score_included"] is False  # Headlines alone no longer enter weekly history.
    assert result["score_status"] == "needs_review"


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
    assert result["score"] == -2
    result = score_article(article("Acme Manufacturing 财务消息", language="zh", title_en="Acme Manufacturing returns to profit"), SUBJECT, NOW)
    assert result["score"] == 2


def test_mixed_directions_do_not_add_repeated_terms():
    result = score_article(article("Acme Manufacturing rating upgraded but announces plant closure"), SUBJECT, NOW)
    assert result["score"] == -2
    assert "mixed directions" in result["score_reason"]


def test_publication_weeks_use_monday_in_moscow():
    assert str(week_start(datetime(2026, 11, 1, 20, 59, tzinfo=timezone.utc))) == "2026-10-26"
    assert str(week_start(datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc))) == "2026-11-02"


def assessed(value, **extra):
    return article("Test", score=value, score_included=True, score_status="provisional", **extra)


def test_no_data_differs_from_neutral_and_uncertainty():
    assert aggregate_week([])["score"] is None
    neutral = aggregate_week([assessed(0)])
    assert neutral["mean_score"] == 0
    assert neutral["neutral_count"] == 1
    excluded = aggregate_week([article("Unknown", score=0, score_included=False, score_status="needs_review")])
    assert excluded["score"] is None
    assert excluded["excluded_count"] == 1


def test_weekly_rounding_extrema_and_offsetting_coverage():
    data = [assessed(-3), assessed(3, id=2, url="https://example.test/2")]
    result = aggregate_week(data)
    assert result["score"] == 0
    assert result["mean_score"] == 0
    assert (result["worst_score"], result["best_score"]) == (-3, 3)
    assert result["negative_count"] == result["positive_count"] == 1
    assert aggregate_week([assessed(-1), assessed(0, id=2, url="b")])["score"] == -1
    assert aggregate_week([assessed(1), assessed(0, id=2, url="b")])["score"] == 1


def test_repeated_url_counts_once_and_review_takes_precedence():
    data = [assessed(-3), assessed(-2, id=2),
            article("Test", score=1, score_included=True, score_status="reviewed", id=3)]
    result = aggregate_week(data)
    assert result["article_count"] == 1
    assert result["mean_score"] == 1
    assert result["article_ids"] == [3]
    assert result["contributions"][0]["score"] == 1


def test_latest_analyst_exclusion_overrides_older_inclusion():
    older = {**assessed(-3), "score_status": "reviewed", "score_updated_at": NOW}
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
