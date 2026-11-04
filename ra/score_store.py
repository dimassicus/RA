"""Persist article assessments and auditable weekly company snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import db
from .scoring import VERSION, aggregate_week, input_hash, score_article, week_start, weekly_groups

FIELDS = ("score", "score_auto", "score_reason", "score_evidence", "score_status",
          "score_included", "score_version", "score_input_hash")


def _lock(conn):
    conn.execute("SELECT pg_advisory_xact_lock(8765, 71)")


def _audit(conn, article, assessment, reason):
    conn.execute(
        "INSERT INTO article_score_history (result_id,company_id,url,previous,assessment,reason) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (article["id"], article["company_id"], article["url"],
         Jsonb({field: article[field] for field in FIELDS}), Jsonb(assessment), reason),
    )


def _write(conn, result_id, assessment):
    conn.execute("UPDATE search_results SET " + ", ".join(field + "=%s" for field in FIELDS) +
                 ", score_updated_at=now() WHERE id=%s",
                 tuple(Jsonb(assessment[field]) if field == "score_evidence" else assessment[field]
                       for field in FIELDS) + (result_id,))


def _assess(conn, run_id=None):
    rows = conn.execute(
        "SELECT r.*, s.subjects FROM search_results r JOIN search_runs s ON s.id=r.run_id "
        "WHERE s.status NOT IN ('running','queued') " +
        ("AND r.run_id=%s " if run_id is not None else "") + "ORDER BY r.id FOR UPDATE OF r",
        (run_id,) if run_id is not None else (),
    ).fetchall()
    updated = 0
    for article in rows:
        # Explicit analyst decisions survive backfill, translations and rule upgrades.
        if article["score_status"] == "reviewed":
            continue
        subject = next((s for s in article["subjects"] if s.get("id") == article["company_id"] and article["company_id"]), None)
        subject = subject or next((s for s in article["subjects"] if s["name"] == article["subject_name"]), {"name": article["subject_name"]})
        if article["score_version"] == VERSION and article["score_input_hash"] == input_hash(article, subject):
            continue
        assessment = score_article(article, subject)
        if article["score"] is not None:
            _audit(conn, article, assessment, "Automatic reassessment after evidence or rule change")
        _write(conn, article["id"], assessment)
        updated += 1
    return updated


def _weekly(conn):
    articles = conn.execute(
        "SELECT r.* FROM search_results r JOIN search_runs s ON s.id=r.run_id "
        "WHERE r.company_id IS NOT NULL AND s.status NOT IN ('running','queued')"
    ).fetchall()
    from .events import assign_groups
    assigned = assign_groups(articles)
    for article in articles:
        group = assigned[article["id"]]
        if article["event_group"] != group:
            conn.execute("UPDATE search_results SET event_group=%s WHERE id=%s", (group, article["id"]))
            conn.execute("INSERT INTO article_event_history (result_id,previous_group,new_group,reason) VALUES (%s,%s,%s,%s)",
                         (article["id"], article["event_group"], group, "Automatic URL / source-evidence grouping"))
            article["event_group"] = group
    groups = weekly_groups(articles)
    existing = {(row["company_id"], row["week_start"]): row["metrics"] for row in
                conn.execute("SELECT * FROM company_weekly_scores").fetchall()}
    keys = set(groups) | set(existing)
    coverage = {}
    runs = conn.execute("SELECT id, subjects, created_at, status FROM search_runs").fetchall()
    errors = conn.execute("SELECT run_id,subject_name,count(*) AS count FROM search_errors GROUP BY run_id,subject_name").fetchall()
    error_counts = {(r["run_id"], r["subject_name"]): r["count"] for r in errors}
    current = week_start(datetime.now(timezone.utc))
    for run in runs:
        for subject in run["subjects"]:
            if not subject.get("id"):
                continue
            key = (subject["id"], week_start(run["created_at"]))
            keys.add(key)
            entry = coverage.setdefault(key, {"search_count": 0, "source_errors": 0, "unfinished_runs": 0})
            entry["search_count"] += 1
            entry["unfinished_runs"] += run["status"] in ("queued", "running")
            entry["source_errors"] += error_counts.get((run["id"], subject["name"]), 0) + error_counts.get((run["id"], "Run"), 0)
    # Save explicit no-data weeks, never manufacture a neutral zero for silence.
    for company in conn.execute("SELECT id,created_at FROM companies WHERE active").fetchall():
        first = week_start(company["created_at"])
        while first <= current:
            keys.add((company["id"], first))
            first += timedelta(days=7)
    changed = 0
    for key in sorted(keys):
        metrics = aggregate_week(groups.get(key, []))
        metrics.update(coverage.get(key, {"search_count": 0, "source_errors": 0, "unfinished_runs": 0}))
        if existing.get(key) == metrics:
            continue
        conn.execute(
            "INSERT INTO company_weekly_scores (company_id,week_start,score,mean_score,metrics) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (company_id,week_start) DO UPDATE SET "
            "score=EXCLUDED.score,mean_score=EXCLUDED.mean_score,metrics=EXCLUDED.metrics,updated_at=now()",
            (*key, metrics["score"], metrics["mean_score"], Jsonb(metrics)),
        )
        conn.execute("INSERT INTO weekly_score_history (company_id,week_start,snapshot) VALUES (%s,%s,%s)",
                     (*key, Jsonb(metrics)))
        changed += 1
    return changed


def refresh_scores(run_id=None):
    """One transaction prevents charts observing half-rebuilt scores."""
    with db.connect() as conn:
        _lock(conn)
        return {"articles_updated": _assess(conn, run_id), "weeks_updated": _weekly(conn)}


def get_article(result_id):
    with db.connect() as conn:
        return conn.execute("SELECT r.*,s.status AS run_status FROM search_results r "
                            "JOIN search_runs s ON s.id=r.run_id WHERE r.id=%s", (result_id,)).fetchone()


def article_history(result_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM article_score_history WHERE result_id=%s ORDER BY id DESC", (result_id,)).fetchall()


def review_score(result_id, score: int, included: bool, reason: str):
    if type(score) is not int or not -3 <= score <= 3:
        raise ValueError("Score must be a whole number from -3 to +3")
    reason = reason.strip()
    if not 5 <= len(reason) <= 2000:
        raise ValueError("Explain the score in 5–2,000 characters")
    with db.connect() as conn:
        _lock(conn)
        article = conn.execute("SELECT r.*,s.status AS run_status FROM search_results r JOIN search_runs s ON s.id=r.run_id "
                               "WHERE r.id=%s FOR UPDATE OF r", (result_id,)).fetchone()
        if not article or article["run_status"] in ("running", "queued"):
            raise ValueError("The article must belong to a finished search")
        if included and (not article["published_at"] or article["published_at"] > datetime.now(timezone.utc)):
            raise ValueError("A valid publication date is required for weekly inclusion")
        assessment = {field: article[field] for field in FIELDS}
        assessment.update(score=score, score_included=included, score_status="reviewed", score_reason=reason)
        _audit(conn, article, assessment, reason)
        _write(conn, result_id, assessment)
        _weekly(conn)


def weekly_history(company_id=None):
    with db.connect() as conn:
        return conn.execute(
            "SELECT w.*,c.name,c.short_name,c.active FROM company_weekly_scores w JOIN companies c ON c.id=w.company_id " +
            ("WHERE c.id=%s " if company_id else "WHERE c.active ") + "ORDER BY c.name,w.week_start",
            (company_id,) if company_id else (),
        ).fetchall()


def week_detail(company_id, start):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM company_weekly_scores WHERE company_id=%s AND week_start=%s", (company_id, start)).fetchone()
        if not row:
            return None, [], []
        ids = row["metrics"]["article_ids"] + row["metrics"]["excluded_ids"] + row["metrics"].get("duplicate_ids", [])
        articles = conn.execute("SELECT * FROM search_results WHERE id=ANY(%s) ORDER BY score,id", (ids,)).fetchall()
        history = conn.execute("SELECT * FROM weekly_score_history WHERE company_id=%s AND week_start=%s ORDER BY id DESC",
                               (company_id, start)).fetchall()
        return row, articles, history


if __name__ == "__main__":
    db.init_schema()
    print(refresh_scores())
