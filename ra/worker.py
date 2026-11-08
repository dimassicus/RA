"""Single local background worker for user-initiated search runs."""

from __future__ import annotations

from functools import partial
from datetime import datetime, timedelta, timezone
import json
import queue
import threading
import time
import logging

from . import db, governance_roster
from .search import search_company
from .governance_sources import notice_source
from .exposure import add_exposure_leads, search_topic, sector_markets, topic_editions
from .news_apis import plan_subjects
from .translation import translate_text
from .language import detect_language
from .score_store import refresh_scores


_queue: queue.Queue[int] = queue.Queue()
_thread: threading.Thread | None = None

# The local app and its single search worker share one immutable activity snapshot.
_activity = None


def _report(run_id, message, done=None, total=None):
    global _activity
    _activity = (run_id, message, done, total, time.monotonic())
    logging.getLogger(__name__).info("run_progress %s", json.dumps(
        dict(run_id=run_id, message=message, done=done, total=total)))


def search_progress(run):
    if run["status"] == "queued":
        with db.connect() as conn:
            ahead = conn.execute("SELECT id FROM search_runs WHERE id<>%s AND "
                                 "(status='running' OR (status='queued' AND id<%s)) "
                                 "ORDER BY (status='running') DESC,id", (run["id"], run["id"])).fetchall()
        message = "Waiting for the search worker."
        if ahead:
            message = f"Waiting behind run #{ahead[0]['id']} · {len(ahead)} search{'es' if len(ahead) != 1 else ''} ahead."
        return {"message": message, "done": 0, "total": 1}
    if run["status"] == "running":
        snapshot = _activity
        if snapshot and snapshot[0] == run["id"]:
            _, message, done, total, updated = snapshot
            age = int(time.monotonic() - updated)
            return {"message": message, "done": done, "total": total, "age": age}
        return {"message": "Starting search…", "done": None, "total": None}
    return None


def enqueue(run_id: int):
    _queue.put(run_id)


def start_worker():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with db.connect() as conn:
        conn.execute("UPDATE search_runs SET status = 'queued' WHERE status = 'running'")
        pending = conn.execute("SELECT id FROM search_runs WHERE status = 'queued' ORDER BY id").fetchall()
    _thread = threading.Thread(target=_work_loop, daemon=True, name="ra-search-worker")
    _thread.start()
    for run in pending:
        enqueue(run["id"])


def _work_loop():
    while True:
        try:
            run_id = _queue.get(timeout=60)
        except queue.Empty:
            from .headline_review import retry_pending
            try:
                retry_pending()
            except Exception:
                logging.exception("Saved translation retry failed; will retry next minute")
            continue
        try:
            process_run(run_id)
        except Exception as exc:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE search_runs SET status = 'failed', finished_at = now(), error_count = error_count + 1 "
                    "WHERE id = %s", (run_id,)
                )
                conn.execute(
                    "INSERT INTO search_errors (run_id, subject_name, message) VALUES (%s, %s, %s)",
                    (run_id, "Run", f"{type(exc).__name__}: {exc}"),
                )
        finally:
            _queue.task_done()


def process_run(run_id: int, searcher=search_company, topic_searcher=None, roster_check=governance_roster.check):
    run = db.get_run(run_id)
    if run is None or run["status"] not in ("queued", "running"):
        return
    with db.connect() as conn:
        conn.execute(
            "UPDATE search_runs SET status = 'running', started_at = COALESCE(started_at, now()), "
            "completed_companies = 0, result_count = 0, error_count = 0, retry_translations = TRUE WHERE id = %s", (run_id,)
        )
        conn.execute("DELETE FROM search_results WHERE run_id = %s", (run_id,))
        conn.execute("DELETE FROM search_errors WHERE run_id = %s", (run_id,))
    report = partial(_report, run_id)
    report("Preparing company searches")
    window_end = run.get("window_end") or run["created_at"]
    window_start = run.get("window_start") or window_end - timedelta(days=7)
    historical_window = (window_start, window_end) if run.get("window_start") else None
    topic_cache = {}
    editions = topic_editions(run["subjects"])
    include_factors = topic_searcher is None
    if topic_searcher is None:
        markets = sector_markets(run["subjects"])

        def topic_searcher(branch, topic, *, edition="", factor=None):
            detail = f" · {factor[0]}" if factor else ""
            report(f"Searching {branch} news: {topic} ({edition or 'English edition'}){detail}")
            return search_topic(branch, topic, edition=edition, markets=markets, factor=factor, window=historical_window)
    check = partial(db.run_topic_check, run_id, topic_searcher, editions, include_factors=include_factors)
    for subject in plan_subjects(run["subjects"], run_id):
        subject_key = str(subject.get("id") or f"one-off:{run_id}")
        name = subject["name"]

        def company_progress(message, done=None, total=None):
            report(f"{name} · {message}", done, total)

        company_progress("Checking company news and official sources")
        search_failure = ""
        try:
            articles, errors = (search_company(subject, progress=company_progress, window=historical_window) if searcher is search_company
                                else searcher(subject))
        except Exception as exc:
            search_failure = f"{type(exc).__name__}: {exc}"
            articles, errors = [], [search_failure]
        source = notice_source(subject) if searcher is search_company else None
        if source:
            publisher, index_url = source
            failures = [message for message in errors if message.startswith(publisher + ':')]
            count = sum(item.get('discovery_service') == publisher for item in articles)
            summary = f'{count} notices collected in the seven-day window.'
            if failures:
                summary += ' Incomplete check. ' + ' '.join(failures)
            elif search_failure:
                summary = 'Disclosure check incomplete. ' + search_failure
            governance_roster.save_note(run_id, subject_key, subject, index_url, publisher, summary)
        company_progress("Checking leadership and ownership records")
        try:
            if historical_window:
                governance_roster.save_note(run_id, subject_key, subject, "", "Historical search",
                                           "Current ownership and leadership snapshots were not treated as historical records. Dated disclosures are searched separately.")
            else:
                roster_check(run_id, subject_key, subject)
        except (ValueError, OSError) as exc:
            errors.append(f"Registry roster: {exc}")
        articles = add_exposure_leads(subject, articles, errors, topic_cache, check)
        # Feeds can ignore their date query; enforce one fixed window across all branches.
        retained = []
        for article in articles:
            published = article.get("published_at")
            if isinstance(published, str):
                try:
                    published = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    published = None
            if published and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published and window_start <= published <= window_end:
                retained.append({**article, "published_at": published})
        logging.getLogger(__name__).info("run_window %s", json.dumps(dict(
            run_id=run_id, subject=name, start=window_start.isoformat(), end=window_end.isoformat(),
            collected=len(articles), retained=len(retained), excluded=len(articles) - len(retained))))
        articles = retained
        with db.connect() as conn:
            for index, article in enumerate(articles):
                company_progress(f"Processing lead {index + 1} of {len(articles)}", index, len(articles))
                language = detect_language(article["title"], article["language"])
                title_en, provider = translate_text(
                    article["title"], language, name, article["publisher"]
                )
                publisher_en = article["publisher"]
                seen_before = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM search_results WHERE subject_key = %s AND url = %s "
                    "AND run_id <> %s)", (subject_key, article["url"], run_id)
                ).fetchone()["exists"]
                conn.execute(
                    "INSERT INTO search_results "
                    "(run_id, company_id, subject_key, subject_name, title, url, publisher, discovery_service, source_kind, "
                    "published_at, query, language, title_en, publisher_en, translation_provider, "
                    "signal_terms, seen_before, exposure_paths) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, subject_key, url) DO NOTHING",
                    (run_id, subject.get("id"), subject_key, name, article["title"],
                     article["url"], article["publisher"], article.get("discovery_service", ""),
                     article.get("source_kind", "news"), article["published_at"],
                     article["query"], language, title_en, publisher_en, provider,
                     article["signal_terms"], seen_before, article.get("exposure_paths", [])),
                )
            for message in errors:
                conn.execute(
                    "INSERT INTO search_errors (run_id, subject_name, message) VALUES (%s, %s, %s)",
                    (run_id, name, message[:1000]),
                )
            conn.execute(
                "UPDATE search_runs SET completed_companies = completed_companies + 1, "
                "result_count = result_count + %s, error_count = error_count + %s WHERE id = %s",
                (len(articles), len(errors), run_id),
            )
        time.sleep(0.25)
    from .headline_review import review_saved
    report("Checking headline translations with Qwen")
    review_saved(run_id, progress=report)
    with db.connect() as conn:
        counts = conn.execute(
            "SELECT result_count, error_count FROM search_runs WHERE id = %s", (run_id,)
        ).fetchone()
        status = "completed" if counts["error_count"] == 0 else ("partial" if counts["result_count"] else "failed")
        conn.execute(
            "UPDATE search_runs SET status = %s, finished_at = now() WHERE id = %s",
            (status, run_id),
        )
    refresh_scores(run_id)
    from .verification_store import enqueue_run
    enqueue_run(run_id, limit_per_company=None)
