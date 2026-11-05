"""Read-only company monitoring brief shared by the PDF and workbook exports."""
from calendar import monthrange
from datetime import datetime, time, timedelta

from . import db, governance_roster, watch_dates
from .exposure import scope_sector_paths
from .aliases import headline_identity, normalized
from .relevance import governance_cues, headline
from .keywords import matches
from .scoring import WEEK_TIMEZONE, week_start


def periods(anchor):
    anchor = anchor.astimezone(WEEK_TIMEZONE)
    yield "Current", anchor
    yield "1 week ago", anchor - timedelta(weeks=1)
    for months in (1, 3, 6, 12):
        year, month = divmod(anchor.year * 12 + anchor.month - 1 - months, 12)
        month += 1
        yield f"{months} month{'s' if months > 1 else ''} ago", anchor.replace(
            year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))


def load_report(run, subject):
    end = run.get("finished_at") or run["created_at"]
    points = list(periods(end))
    history, previous_runs = {}, {}
    with db.connect() as conn:
        key = str(subject.get("id") or f"one-off:{run['id']}")
        articles = conn.execute("SELECT * FROM search_results WHERE run_id=%s AND subject_key=%s",
                                (run["id"], key)).fetchall()
        if subject.get("id"):
            # Allow post-run verification until the next company search or the end of its score week.
            next_run = conn.execute(
                "SELECT min(created_at) AS next_start FROM search_runs WHERE created_at > %s "
                "AND subjects @> %s::jsonb", (end, db.Jsonb([{"id": subject["id"]}]))).fetchone()["next_start"]
            next_week = datetime.combine(week_start(end) + timedelta(weeks=1), time.min, WEEK_TIMEZONE)
            cutoff = min(datetime.now(WEEK_TIMEZONE), next_run or next_week, next_week)
            for label, point in points:
                as_of = cutoff if label == "Current" else point
                row = conn.execute(
                    "SELECT snapshot FROM weekly_score_history WHERE company_id=%s AND week_start=%s "
                    "AND created_at <= %s ORDER BY created_at DESC,id DESC LIMIT 1",
                    (subject["id"], week_start(point), as_of)).fetchone()
                history[label] = row["snapshot"] if row else None
                if label not in ("1 week ago", "1 month ago", "12 months ago"):
                    continue
                prior = conn.execute(
                    "SELECT id,finished_at,subjects FROM search_runs WHERE subjects @> %s::jsonb "
                    "AND status IN ('completed','partial') AND finished_at BETWEEN %s AND %s "
                    "ORDER BY finished_at DESC,id DESC LIMIT 1",
                    (db.Jsonb([{"id": subject["id"]}]), point - timedelta(days=7), point)).fetchone()
                if prior:
                    rows = conn.execute("SELECT * FROM search_results WHERE run_id=%s AND subject_key=%s",
                                        (prior["id"], key)).fetchall()
                    prior_subject = next(s for s in prior["subjects"] if s.get("id") == subject["id"])
                    previous_runs[label] = (prior["finished_at"], relevant_sources(prior_subject, rows, prior["finished_at"]))
    errors = [e["message"] for e in db.get_errors(run["id"]) if e["subject_name"] in (subject["name"], "Run")]
    report = build_report(run, subject, articles, history, errors, previous_runs)
    report["rosters"] = governance_roster.observations(run["id"], key)
    return report


def relevant_sources(subject, articles, end):
    start = end - timedelta(days=7)
    sources = []
    for a in articles:
        published = a["published_at"]
        if not published or not start < published <= end:
            continue
        check = a.get("verification") or {}
        state = check.get("status", "pending")
        a = scope_sector_paths(a, subject)
        paths = a.get("exposure_paths") or []
        branches = [label for prefix, label in (("Sector ·", "Sector"), ("Country ·", "Country / region"))
                    if any(path.startswith(prefix) for path in paths)]
        text = " ".join(headline(a, field) for field in ("title", "title_en"))
        identity = headline_identity(normalized(text), subject)
        direct = (a.get("source_kind") == "official" or identity or state == "supported"
                  or a.get("score_included") or (a.get("report_selected") and a.get("source_kind") != "context"))
        if direct and (state != "unmatched" or a.get("score_included")):
            branches.insert(0, "Company")
        if not branches:
            continue
        rank = (bool(a.get("score_included")), abs(a.get("score") or 0) if a.get("score_included") else 0,
                bool(a.get("report_selected")), "Company" in branches, "Country / region" in branches, bool(matches(headline(a, "title"), a.get("language")) or matches(headline(a, "title_en"))), published, a["id"])
        sources.append(dict(article=a, branches=branches, rank=rank, state=state,
                            governance=governance_cues(a, subject) if "Company" in branches else []))
    sources.sort(key=lambda s: s["rank"], reverse=True)
    return sources


def build_report(run, subject, articles, history, errors, previous_runs=None):
    end = (run.get("finished_at") or run["created_at"]).astimezone(WEEK_TIMEZONE)
    start = end - timedelta(days=7)
    sources = relevant_sources(subject, articles, end)
    top, seen = [], set()
    for i, source in enumerate(sources, 1):
        source["ref"] = f"S{i}"
        a = source["article"]
        event = a.get("event_group") or a["url"]
        if event not in seen and len(top) < 5:
            top.append(source)
            seen.add(event)
    ratings = []
    for label, point in periods(end):
        metrics = history.get(label)
        ratings.append((label, week_start(point).isoformat(), metrics))
    current = history.get("Current")
    comparisons = []
    for label in ("1 week ago", "1 month ago", "12 months ago"):
        past = history.get(label)
        if not past or not current or not past.get("search_count") or not current.get("search_count"):
            summary = "Insufficient recorded search coverage for comparison."
        else:
            def mix(m):
                return f"{m['negative_count']} adverse, {m['positive_count']} favourable, {m['neutral_count']} neutral"
            summary = f"Scored events: {mix(past)} then; {mix(current)} now."
            previous_driver = strongest_contribution(past)
            current_driver = strongest_contribution(current)
            if previous_driver and current_driver and previous_driver["reason"] != current_driver["reason"]:
                summary += f" Earlier driver: {previous_driver['reason']}. Current driver: {current_driver['reason']}."
            if past["score"] is not None and current["score"] is not None:
                change = current["score"] - past["score"]
                summary += f" Sentiment moved {change:+d}." if change else " Sentiment held steady."
            else:
                summary += " A score change cannot be calculated."
        prior = (previous_runs or {}).get(label)
        if prior:
            prior_end, previous = prior
            counts = []
            for branch in ("Company", "Sector", "Country / region"):
                old = sum(branch in item["branches"] for item in previous)
                new = sum(branch in item["branches"] for item in sources)
                counts.append(f"{branch.lower()} {old} to {new}")
            summary = (f"Relevant headlines versus the search ending {prior_end.astimezone(WEEK_TIMEZONE):%d %b %Y}, "
                       + ", ".join(counts) + ". " + summary)
        elif not past:
            summary = "No archived comparison run or rating snapshot."
        else:
            summary = "No archived comparison run. " + summary
        comparisons.append((label, summary))
    return dict(run=run, subject=subject, start=start, end=end, sources=sources, top=top,
                ratings=ratings, comparisons=comparisons, errors=errors, dates=watch_dates.upcoming(subject, end))


def rating_rows(report):
    for label, week, metrics in report["ratings"]:
        if not metrics:
            yield label, week, "—", "No snapshot", "—"
            continue
        score = metrics["score"]
        coverage = f"{metrics['search_count']} searches · {metrics['source_errors']} errors"
        yield label, week, score if score is not None else "—", coverage, metrics["event_count"]


def evidence_rows(report):
    for branch in ("Company", "Sector", "Country / region"):
        items = [s for s in report["sources"] if branch in s["branches"]]
        yield branch, len(items), sum(s["state"] == "supported" for s in items), sum(s["state"] == "pending" for s in items)


def strongest_contribution(metrics):
    return max(metrics.get("contributions") or [], key=lambda e: (abs(e["score"]), -e["score"]), default=None)


def driver(report):
    event = strongest_contribution(report["ratings"][0][2] or {})
    if not event:
        return "No eligible company event contributed to a recorded score for this week.", ""
    return f"Strongest contribution ({event['score']:+d}): {event['reason']}", event["url"]
