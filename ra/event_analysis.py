"""Draft event groups once from a finished run's evidence using RA's local model."""
import logging
import threading
from collections import defaultdict

from . import article_translation, db, headline_review, keywords
from .events import event_assessment

_log = logging.getLogger(__name__)
_wake = threading.Event()
_worker = None
_STRING = {"type": "string"}
_INT = {"type": "integer"}
_GROUPS = keywords.object_schema({"assignments": {"type": "array", "items": keywords.object_schema({
    "id": _INT, "event": _INT, "title": _STRING, "sentiment": {"type": "integer", "enum": [-1, 0, 1]}})}})
_DRAFT = keywords.object_schema({"title": _STRING, "summary": _STRING})


def candidate(row):
    return (row.get('score') in (-1, 0) and (row.get('score_included') or
            (row.get('source_kind') != 'context' and not row.get('exposure_paths') and
             (row.get('verification') or {}).get('status') != 'unmatched' and row.get('score_status') != 'reviewed')))


def excerpt(text, limit):
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return text[:half] + " […] " + text[-half:]


def source_texts(document, verification):
    hashes = {verification.get("document_hash")}
    hashes.update(s.get("hash") for s in verification.get("sources", []))
    sources = []
    for doc in [document, *document.get("linked_documents", [])]:
        if doc.get("status") != "available" or not doc.get("sha256") or doc["sha256"] not in hashes:
            continue
        body = doc["body"]
        kind = "original publisher text"
        if doc.get("review_only"):
            kind = "reader recovery, analyst review required"
        elif doc.get("language") not in ("en", "pt"):
            try:
                body = article_translation.checked_text(doc)
                kind = "reviewed translation"
            except ValueError:
                kind = "original language, translation unverified"
        sources.append({"url": doc["final_url"], "kind": kind, "text": body})
    return sources


def source_sample(sources, budget):
    # Give long filings the space left by short wrapper pages.
    excerpts = []
    for index, source in enumerate(sorted(sources, key=lambda s: len(s["text"]))):
        text = excerpt(source["text"], budget // (len(sources) - index))
        budget -= len(text)
        excerpts.append(dict(source, text=text))
    return excerpts


def evidence(row):
    check = row.get("verification") or {}
    return {"id": row["id"], "published": str(row.get("published_at") or "")[:10],
            "publisher": row.get("publisher", ""), "title": row["title"][:500],
            "english": (row.get("title_en", "")[:500] if (row.get("headline_review") or {}).get("status") == "faithful" else ""),
            "summary": row.get("discovery_summary", "")[:600], "status": check.get("status", "pending"),
            "quotes": [e["sentence"][:650] for e in check.get("evidence", [])[:3] if e.get("sentence")],
            "sources": row.get("event_sources", []),
            "score": row["score"], "reviewed": row.get("score_status") == "reviewed"}


def _day(value):
    from datetime import date
    return date.fromisoformat(value) if value else date.min


def draft_company(company, rows, previous=(), model=None):
    """Yield each finished group so another model failure cannot discard its brief."""
    model = model or keywords.model_json
    records = [evidence(r) for r in sorted(rows, key=lambda a: (str(a.get('published_at') or ''), a['id'])) if candidate(r)]
    catalog = {e['event']: e for e in previous}
    groups, result = defaultdict(list), {}
    for offset in range(0, len(records), 12):
        batch = records[offset:offset + 12]
        # Nearest dated anchors keep long historical searches within the model context.
        anchors = sorted(catalog.values(), key=lambda e: abs((_day(e['date']) - _day(batch[0]['published'])).days))[:60]
        allowed = {r['id'] for r in batch}
        response = model("Group coverage by concrete company incident, across languages and publishers. "
            "Return every input id once. Use an existing event ID for the same incident, otherwise use the first "
            "article ID of the incident in this batch. Score each standalone story -1 negative, 0 neutral, +1 positive. Distinguish different sites, cases, periods and subsequent "
            "decisions. Use event 0 for unrelated, purely favourable or background stories without an event. "
            "Never infer corporate wrongdoing from an owner's case. Give each event a specific English title.",
            {"company": company, "existing": anchors, "leads": [dict(r, sources=source_sample(r["sources"], 1800)) for r in batch]},
            _GROUPS)["assignments"]
        if (len(response) != len(batch) or {a['id'] for a in response} != allowed or
                any(type(a["sentiment"]) is not int or a["sentiment"] not in (-1, 0, 1) or type(a['event']) is not int or a['event'] not in allowed | {e['event'] for e in anchors} | {0}
                    or not isinstance(a['title'], str) for a in response)):
            raise ValueError("Incomplete event grouping")
        by_id = {a['id']: a for a in response}
        for r in batch:
            assignment = by_id[r['id']]
            key = assignment['event']
            if key in allowed and by_id[key]['event'] != key:
                raise ValueError("Conflicting event membership")
            protected = r["status"] == "supported" or r["reviewed"]
            direction = r['score'] if protected else assignment['sentiment']
            if protected and not key:
                key = r['id']
            r['score'] = direction
            if not key or direction > 0:
                result[r['id']] = {"excluded": True, "sentiment": direction}
                continue
            catalog.setdefault(key, {"event": key, "title": assignment['title'][:250], "date": r['published']})
            groups[key].append(r)
    if result:
        yield result
    for key, members in groups.items():
        # Show the model the time span and representative evidence without exceeding local context.
        sample = sorted(members, key=lambda r: (r['status'] != 'supported', r['score'], r['id']))
        sample = sample[:5]
        facts = [dict(r, sources=source_sample(r['sources'], 22000 // len(sample))) for r in sample]
        instruction = ("Write an English analyst brief of 150–250 words about this one company event. "
            "Describe what happened, distinguish event dates from publication dates, analyse severity and limits. "
            "Use only the supplied evidence. Distinguish allegations, source statements and supported impacts. "
            "Do not invent facts, assume that silence means safety, or treat repeated coverage as greater damage. "
            "Do not claim that a filing omits details on the basis of excerpts. Identify the company’s role. "
            "Separate when the company learned of the event from when it disclosed it. Attribute contested facts. "
            "Describe uncertainty where needed. Return title and summary.")
        payload = {"company": company, "event": catalog[key], "coverage": len(members),
                   "publication_dates": sorted({r['published'] for r in members}), "evidence": facts}
        draft = model(instruction, payload, _DRAFT)
        if not 150 <= len((draft.get('summary') or '').split()) <= 250:
            draft = model(instruction + " Rewrite the brief to 180–210 words.", payload, _DRAFT)
        if (not isinstance(draft['title'], str) or not draft['title'].strip() or
                not isinstance(draft['summary'], str) or not 150 <= len(draft['summary'].split()) <= 250):
            raise ValueError("Incomplete event narrative")
        yield {r['id']: {"event": key, "sentiment": r["score"], "title": draft['title'][:250],
                         "summary": draft['summary'].strip()} for r in members}


def run_events(run, rows):
    countries = {s.get('id') or s['name']: s.get('country_registration') or 'Unspecified' for s in run['subjects']}
    groups = defaultdict(list)
    for row in rows:
        draft = row.get('event_draft') or {}
        if candidate(row) and draft.get('event'):
            groups[(row.get('company_id') or row['subject_name'], row['subject_name'], row.get('event_group') or str(row['id']))].append(row)
    result = []
    for (company, name, key), members in groups.items():
        initial = event_assessment(members)
        score = event_assessment(members, saved=True) if run.get('selection_saved') else initial
        draft = next((r['event_draft'] for r in members if (r.get('event_draft') or {}).get('summary')), {})
        result.append(dict(country=countries.get(company, countries.get(name, 'Unspecified')), company=name, key=key,
                           title=draft.get('title') or members[0].get('title_en') or members[0]['title'],
                           summary=draft.get('summary'), score=score['score'], reason=score['reason'],
                           priority=score['priority'], sources=members))
    return sorted(result, key=lambda e: (e['country'].casefold(), e['company'].casefold(),
                                         tuple(-int(i) for i in e['priority']), e['title']))


def process(run_id):
    from . import score_store
    rows = db.get_results(run_id)
    companies = defaultdict(list)
    for row in rows:
        companies[row['subject_key']].append(row)
    for members in companies.values():
        members = [r for r in members if not r.get('event_draft') and candidate(r)]
        if not members:
            continue
        with db.connect() as conn:
            documents = {r['url']: r['document'] for r in conn.execute(
                "SELECT url,document FROM article_documents WHERE url=ANY(%s)",
                ([r['verification'].get('requested_url') for r in members if r.get('verification')],))}
            for row in members:
                check = row.get('verification') or {}
                row['event_sources'] = source_texts(documents.get(check.get('requested_url'), {}), check)
            prior = conn.execute("""SELECT DISTINCT ON (event_draft->>'event')
                (event_draft->>'event')::bigint AS event, event_draft->>'title' AS title,
                published_at::date::text AS date FROM search_results WHERE (company_id=%s OR (company_id IS NULL AND run_id=%s))
                AND event_draft ? 'event' AND published_at BETWEEN %s::timestamptz - interval '30 days'
                AND %s::timestamptz + interval '30 days'""",
                (members[0]['company_id'], run_id, min((r['published_at'] for r in members if r['published_at']), default=None),
                 max((r['published_at'] for r in members if r['published_at']), default=None))).fetchall()
        try:
            for drafts in draft_company(members[0]['subject_name'], members, prior):
                with db.connect() as conn:
                    score_store._lock(conn)
                    for rid, draft in drafts.items():
                        conn.execute("UPDATE search_results SET event_draft=%s WHERE id=%s AND event_draft='{}'",
                                     (db.Jsonb(draft), rid))
        finally:
            with db.connect() as conn:
                score_store._lock(conn)
                score_store._assess(conn, run_id)
                score_store._weekly(conn)
    with db.connect() as conn:
        conn.execute("""UPDATE search_runs SET event_analysis_status=CASE WHEN EXISTS (
            SELECT 1 FROM search_results WHERE run_id=%s AND event_draft='{}' AND
            score_status='reviewed' AND score_included AND score<=0) THEN 'pending' ELSE 'complete' END
            WHERE id=%s""", (run_id, run_id))


def _loop():
    while True:
        run_id = None
        try:
            with db.connect() as conn:
                row = conn.execute("""SELECT id FROM search_runs s WHERE event_analysis_status='pending'
                    AND status IN ('completed','partial') AND NOT EXISTS (SELECT 1 FROM search_results r
                    JOIN verification_jobs j ON j.result_id=r.id WHERE r.run_id=s.id AND j.status IN ('queued','running'))
                    ORDER BY id DESC LIMIT 1""").fetchone()
            if row and headline_review.readiness()['ready']:
                run_id = row['id']
                process(run_id)
                continue
        except Exception:
            _log.exception("Company event drafting failed for run %s", run_id)
            if run_id:
                with db.connect() as conn:
                    conn.execute("UPDATE search_runs SET event_analysis_status='failed' WHERE id=%s", (run_id,))
        _wake.wait(15)
        _wake.clear()


def start():
    global _worker
    if not _worker or not _worker.is_alive():
        with db.connect() as conn:
            conn.execute("UPDATE search_runs SET event_analysis_status='pending' WHERE "
                         "event_analysis_status='waiting_sources' AND status IN ('completed', 'partial')")
        _worker = threading.Thread(target=_loop, daemon=True, name='ra-company-events')
        _worker.start()
