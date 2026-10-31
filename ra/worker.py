"""Single local background worker for user-initiated search runs."""

from __future__ import annotations

import queue
import threading
import time

from . import db
from .search import search_company


_queue: queue.Queue[int] = queue.Queue()
_thread: threading.Thread | None = None


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
        run_id = _queue.get()
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


def process_run(run_id: int, searcher=search_company):
    run = db.get_run(run_id)
    if run is None or run["status"] not in ("queued", "running"):
        return
    with db.connect() as conn:
        conn.execute(
            "UPDATE search_runs SET status = 'running', started_at = COALESCE(started_at, now()), "
            "completed_companies = 0, result_count = 0, error_count = 0 WHERE id = %s", (run_id,)
        )
        conn.execute("DELETE FROM search_results WHERE run_id = %s", (run_id,))
        conn.execute("DELETE FROM search_errors WHERE run_id = %s", (run_id,))
    for subject in run["subjects"]:
        subject_key = str(subject.get("id") or f"one-off:{run_id}")
        name = subject["name"]
        try:
            articles, errors = searcher(subject)
        except Exception as exc:
            articles, errors = [], [f"{type(exc).__name__}: {exc}"]
        with db.connect() as conn:
            for article in articles:
                seen_before = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM search_results WHERE subject_key = %s AND url = %s "
                    "AND run_id <> %s)", (subject_key, article["url"], run_id)
                ).fetchone()["exists"]
                conn.execute(
                    "INSERT INTO search_results "
                    "(run_id, company_id, subject_key, subject_name, title, url, publisher, source_kind, "
                    "published_at, query, language, signal_terms, seen_before) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, subject_key, url) DO NOTHING",
                    (run_id, subject.get("id"), subject_key, name, article["title"],
                     article["url"], article["publisher"], article.get("source_kind", "news"), article["published_at"],
                     article["query"], article["language"], article["signal_terms"], seen_before),
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
    with db.connect() as conn:
        counts = conn.execute(
            "SELECT result_count, error_count FROM search_runs WHERE id = %s", (run_id,)
        ).fetchone()
        status = "completed" if counts["error_count"] == 0 else ("partial" if counts["result_count"] else "failed")
        conn.execute(
            "UPDATE search_runs SET status = %s, finished_at = now() WHERE id = %s",
            (status, run_id),
        )
