"""Read-only company monitoring brief shared by the PDF and workbook exports."""
from calendar import monthrange
from datetime import datetime, time, timedelta

from . import db, governance_roster, watch_dates
from .exposure import scope_sector_paths
from .aliases import headline_identity, related_identity, normalized
from .relevance import governance_cues, headline
from .keywords import matches
from .scoring import VERSION, WEEK_TIMEZONE, week_start


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
    end = run.get("window_end") or run.get("finished_at") or run["created_at"]
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
                as_of = (datetime.now(WEEK_TIMEZONE) if run.get("window_end")
                         else cutoff if label == "Current" else point)
                row = conn.execute(
                    "SELECT snapshot FROM weekly_score_history WHERE company_id=%s AND week_start=%s "
                    "AND created_at <= %s ORDER BY created_at DESC,id DESC LIMIT 1",
                    (subject["id"], week_start(point), as_of)).fetchone()
                history[label] = row["snapshot"] if row else None
                if label not in ("1 week ago", "1 month ago", "12 months ago"):
                    continue
                end_column = "COALESCE(window_end,finished_at)" if run.get("window_end") else "finished_at"
                prior = conn.execute(
                    f"SELECT id,{end_column} AS report_end,subjects FROM search_runs WHERE subjects @> %s::jsonb "
                    f"AND status IN ('completed','partial') AND {end_column} BETWEEN %s AND %s "
                    f"ORDER BY {end_column} DESC,id DESC LIMIT 1",
                    (db.Jsonb([{"id": subject["id"]}]), point - timedelta(days=7), point)).fetchone()
                if prior:
                    rows = conn.execute("SELECT * FROM search_results WHERE run_id=%s AND subject_key=%s",
                                        (prior["id"], key)).fetchall()
                    prior_subject = next(s for s in prior["subjects"] if s.get("id") == subject["id"])
                    previous_runs[label] = (prior["report_end"], relevant_sources(prior_subject, rows, prior["report_end"]))
    errors = [e["message"] for e in db.get_errors(run["id"]) if not e.get("resolved") and e["subject_name"] in (subject["name"], "Run")]
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
        identity = headline_identity(normalized(text), subject) or related_identity(normalized(text), subject)
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
    end = (run.get("window_end") or run.get("finished_at") or run["created_at"]).astimezone(WEEK_TIMEZONE)
    start = run.get("window_start") or end - timedelta(days=7)
    sources = relevant_sources(subject, articles, end)
    top, seen = [], set()
    for i, source in enumerate(sources, 1):
        source["ref"] = f"S{i}"
        a = source["article"]
        event = a.get("event_group") or a["url"]
        if (a.get("score") or 0) <= 0 and event not in seen and len(top) < 5:
            top.append(source)
            seen.add(event)
    ratings = []
    for label, point in periods(end):
        metrics = history.get(label)
        ratings.append((label, (week_start(point) + timedelta(days=7)).isoformat(), metrics))
    # Keep prior branch counts for rendering either language without reloading history.
    branches = ("Company", "Sector", "Country / region")
    prior_coverage = {
        label: (date, [(branch, sum(branch in item["branches"] for item in previous),
                        sum(branch in item["branches"] for item in sources)) for branch in branches])
        for label, (date, previous) in (previous_runs or {}).items()
    }
    report = dict(run=run, subject=subject, start=start, end=end, sources=sources, top=top,
                  ratings=ratings, prior_coverage=prior_coverage, errors=errors, dates=watch_dates.upcoming(subject, end))
    report["comparisons"] = list(comparison_rows(report))
    return report


def comparison_rows(report, language="en"):
    russian = language == "ru"
    if not russian and "comparisons" in report:
        yield from report["comparisons"]
        return
    if russian:
        from .i18n import ru

    history = {label: metrics for label, _, metrics in report["ratings"]}
    current = history.get("Current")
    for label in ("1 week ago", "1 month ago", "12 months ago"):
        past = history.get(label)
        prior = report["prior_coverage"].get(label)
        if not prior and not past:
            yield label, ("В архиве нет поиска или оценки для сравнения." if russian
                          else "No archived comparison run or rating snapshot.")
            continue
        if not past or not current or not past.get("search_count") or not current.get("search_count"):
            summary = ("Для сравнения недостаточно данных предыдущих поисков." if russian
                       else "Insufficient recorded search coverage for comparison.")
        else:
            def mix(m):
                if russian:
                    return f"негативных — {m['negative_count']}, позитивных — {m['positive_count']}, нейтральных — {m['neutral_count']}"
                return f"{m['negative_count']} adverse, {m['positive_count']} favourable, {m['neutral_count']} neutral"
            summary = (f"Учтённые события. Ранее: {mix(past)}. Сейчас: {mix(current)}." if russian
                       else f"Scored events: {mix(past)} then; {mix(current)} now.")
            previous_driver = strongest_contribution(past)
            current_driver = strongest_contribution(current)
            if previous_driver and current_driver and previous_driver["reason"] != current_driver["reason"]:
                old, new = previous_driver["reason"], current_driver["reason"]
                summary += (f" Основной фактор ранее: {ru(old)}. Сейчас: {ru(new)}." if russian
                            else f" Earlier driver: {old}. Current driver: {new}.")
            if scale(past) != scale(current):
                summary += (" Изменение оценки не рассчитано: использовались разные шкалы." if russian
                            else " Score change omitted: snapshots use different grading scales.")
            elif past["score"] is not None and current["score"] is not None:
                change = current["score"] - past["score"]
                if change:
                    summary += f" Изменение тональности: {change:+d}." if russian else f" Sentiment moved {change:+d}."
                else:
                    summary += " Тональность не изменилась." if russian else " Sentiment held steady."
            else:
                summary += " Недостаточно данных для расчёта изменения оценки." if russian else " A score change cannot be calculated."
        if prior:
            prior_end, branches = prior
            date = prior_end.astimezone(WEEK_TIMEZONE)
            if russian:
                names = {"Company": "компания", "Sector": "отрасль", "Country / region": "страна / регион"}
                counts = ", ".join(f"{names[branch]} — с {old} до {new}" for branch, old, new in branches)
                summary = f"Материалы по теме в сравнении с поиском от {date:%d.%m.%Y}: {counts}. " + summary
            else:
                counts = ", ".join(f"{branch.lower()} {old} to {new}" for branch, old, new in branches)
                summary = f"Relevant headlines versus the search ending {date:%d %b %Y}, {counts}. " + summary
        else:
            summary = ("В архиве нет поиска для сравнения. " if russian else "No archived comparison run. ") + summary
        yield label, summary


def scale(metrics, language="en"):
    limit = {VERSION: 1, "source-credit-v6": 2}.get(metrics.get("version"), 3)
    return f"от −{limit} до +{limit}" if language == "ru" else f"−{limit} to +{limit}"


def original_scale(metrics, language):
    if metrics.get("version") == VERSION:
        return ""
    if language == "ru":
        return " · исходная шкала " + scale(metrics, language)
    return " · original scale " + scale(metrics)


def rating_rows(report, language="en"):
    for label, week, metrics in report["ratings"]:
        if not metrics:
            yield label, week, "—", "Нет сохранённой оценки" if language == "ru" else "No snapshot", "—"
            continue
        score = metrics["score"]
        coverage = (f"Поисков: {metrics['search_count']} · ошибок: {metrics['source_errors']}" if language == "ru"
                    else f"{metrics['search_count']} searches · {metrics['source_errors']} errors")
        yield label, week, score if score is not None else "—", coverage + original_scale(metrics, language), metrics["event_count"]


def evidence_rows(report):
    for branch in ("Company", "Sector", "Country / region"):
        items = [s for s in report["sources"] if branch in s["branches"]]
        yield branch, len(items), sum(s["state"] == "supported" for s in items), sum(s["state"] == "pending" for s in items)


def strongest_contribution(metrics):
    return max(metrics.get("contributions") or [], key=lambda e: (abs(e["score"]), -e["score"]), default=None)


def driver(report, language="en"):
    metrics = report["ratings"][0][2] or {}
    event = strongest_contribution(metrics)
    if not event:
        return ("Нет событий компании, учтённых в оценке за эту неделю." if language == "ru"
                else "No eligible company event contributed to a recorded score for this week."), ""
    scale_note = original_scale(metrics, language)
    if language == "ru":
        from .i18n import ru
        return f"Наибольший вклад ({event['score']:+d}{scale_note}): {ru(event['reason'])}", event["url"]
    return f"Strongest contribution ({event['score']:+d}{scale_note}): {event['reason']}", event["url"]
