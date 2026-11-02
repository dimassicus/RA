"""Background preliminary research; pending drafts resume after local server restart."""

from __future__ import annotations

import queue
import threading

from . import db, preliminary


_queue: queue.Queue[int] = queue.Queue()
_started = False
_lock = threading.Lock()


def _run():
    while True:
        intake_id = _queue.get()
        try:
            if not db.claim_intake(intake_id):
                continue
            draft = db.get_intake(intake_id)
            data, findings, error = preliminary.research_company(draft["draft_data"])
            db.finish_intake(intake_id, data, findings, error)
        except Exception as exc:
            try:
                draft = db.get_intake(intake_id)
                if draft:
                    db.finish_intake(intake_id, draft["draft_data"], draft["findings"],
                                     f"Research failed: {type(exc).__name__}: {exc}")
            except Exception:
                pass
        finally:
            _queue.task_done()


def start():
    global _started
    with _lock:
        if _started:
            return
        db.reset_interrupted_intake()
        for index in range(2):
            threading.Thread(target=_run, name=f"ra-intake-{index + 1}", daemon=True).start()
        _started = True
    for draft in db.list_intake(("queued",)):
        _queue.put(draft["id"])


def enqueue(intake_id: int):
    start()
    _queue.put(intake_id)
