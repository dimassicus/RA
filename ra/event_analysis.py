"""Draft event groups once from a finished run's evidence using RA's local model."""
import logging
import re
import threading
from collections import defaultdict

from . import article_translation, db, headline_review, keywords, translation_names, company_developments, decision_review
from .events import event_assessment
from .scoring import WEEK_TIMEZONE
from .article_content import canonical_url

_log = logging.getLogger(__name__)
_wake = threading.Event()
_worker = None
_STRING = {"type": "string"}
_INT = {"type": "integer"}
_GROUPS = keywords.object_schema({"assignments": {"type": "array", "items": keywords.object_schema({
    "id": _INT, "event": _INT, "title": _STRING, "profit_decline_quote": _STRING, "sentiment": {"type": "integer", "enum": [-1, 0, 1]}})}})
_DRAFT = keywords.object_schema({"title": _STRING, "summary": _STRING, "event_day": _STRING})


def candidate(row):
    return (row.get('score') in (-1, 0) and (row.get('score_included') or
            (row.get('source_kind') != 'context' and not row.get('exposure_paths') and
             (row.get('verification') or {}).get('status') != 'unmatched' and row.get('score_status') != 'reviewed')))


def triage_candidate(row):
    # A favourable headline can still introduce mixed results in the underlying report.
    return candidate(row) or (row.get('score') == 1 and row.get('score_status') != 'reviewed'
                              and row.get('source_kind') != 'context' and not row.get('exposure_paths')
                              and (row.get('verification') or {}).get('status') != 'unmatched')


def excerpt(text, limit):
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return text[:half] + " […] " + text[-half:]


def source_texts(document, verification, subject=None):
    document = verification.get("review_document") or document
    hashes = {verification.get("document_hash")}
    hashes.update(s.get("hash") for s in verification.get("sources", []))
    sources = []
    for doc in [document, *document.get("linked_documents", [])]:
        if doc.get("status") != "available" or not doc.get("sha256") or doc["sha256"] not in hashes:
            continue
        body = doc["body"]
        kind = "original publisher text"
        if doc.get("method") == "Selected original PDF decision pages":
            kind = "selected original filing pages; full report not assessed"
        elif doc.get("review_only"):
            kind = "reader recovery, analyst review required"
        elif doc.get("language") not in ("en", "pt"):
            try:
                body = article_translation.checked_text(doc, subject)
                kind = "reviewed translation"
            except ValueError:
                kind = "original language, translation unverified"
        language = "en" if kind == "reviewed translation" else doc.get("language", "")
        if doc.get("excerpt") and not doc.get("review_only"):
            kind += " (selected excerpt)"
        sources.append({"url": doc["final_url"], "kind": kind, "text": body,
                        "language": language})
    return sources


def source_sample(sources, budget):
    # Give long filings the space left by short wrapper pages.
    excerpts = []
    for index, source in enumerate(sorted(sources, key=lambda s: len(s["text"]))):
        text = excerpt(source["text"], budget // (len(sources) - index))
        budget -= len(text)
        excerpts.append(dict(source, text=text))
    return excerpts


def evidence(row, subject=None):
    check = row.get("verification") or {}
    return {"id": row["id"], "published": str(row.get("published_at") or "")[:10],
            "decision_notice": company_developments.decision_notice(row), "petition": company_developments.petition_notice(row), "url": row.get("url", ""),
            "publisher": row.get("publisher", ""), "title": row["title"][:500],
            "english": (row.get("title_en", "")[:500] if (row.get("headline_review") or {}).get("status") == "faithful" and
                        translation_names.preserved(row["title"], row.get("title_en", ""), subject) else ""),
            "summary": row.get("discovery_summary", "")[:2500], "status": check.get("status", "pending"),
            "quotes": [e["sentence"][:650] for e in check.get("evidence", [])[:3] if e.get("sentence")],
            "sources": row.get("event_sources", []), "corroboration": row.get("decision_sources", []),
            "score": row["score"], "reviewed": row.get("score_status") == "reviewed"}


_EARNINGS = re.compile(r"\b(?:profits?|earnings|net income|laba|lucro|utilidad|bénéfice|net kâr|прибыл\w*)\b|净利润|純利益", re.I)


def reported_profit_declines(leads, assignments, company, model):
    """Select original evidence independently of headline sentiment and the grouping vote."""
    reports = {}
    for lead in leads:
        if lead['reviewed']:
            continue
        fields = [lead['title'], lead['english'], lead['summary'], *lead['quotes'], *(s['text'] for s in lead['sources'])]
        lines = list(dict.fromkeys(line.strip() for field in fields
                                   for line in re.split(r'(?<=[.!?。！？])\s+|\n+', field) if line.strip()))
        flagged = assignments[lead['id']].get('profit_decline_quote')
        if flagged or any(_EARNINGS.search(line) for line in lines):
            reports[lead['id']] = lines
    if not reports:
        return {}
    response = model("For each record independently, did the named issuer report lower realised profit or earnings "
        "for the reporting period covered by this article? Include an explicitly stated fall or comparable amounts "
        "demonstrating one, even if revenue rose. Exclude forecasts, peers, a slower increase, isolated amounts and "
        "incidental historical periods. Select the numbered passage that actually shows the downward comparison. "
        "An original-language headline is valid evidence. Omit records with no such passage.",
        {"issuer": company, "earnings_reports": {rid: dict(enumerate(lines, 1)) for rid, lines in reports.items()}},
        keywords.object_schema({"findings": {"type": "array", "items": keywords.object_schema({"id": _INT, "passage": _INT})}}))
    quotes = {}
    for finding in response['findings']:
        rid, index = finding['id'], finding['passage']
        if rid in reports and type(index) is int and 1 <= index <= len(reports[rid]):
            quotes[rid] = reports[rid][index - 1]
    return quotes


def _day(value):
    from datetime import date
    return date.fromisoformat(value) if value else date.min


def draft_company(company, rows, previous=(), model=None):
    """Yield each finished group so another model failure cannot discard its brief."""
    model = model or keywords.model_json
    subject = company if isinstance(company, dict) else {"name": company}
    company = subject["name"]
    labels = translation_names.names(subject)
    matcher = translation_names.pattern(labels)
    records = [evidence(r, subject) for r in sorted(rows, key=lambda a: (str(a.get('published_at') or ''), a['id'])) if triage_candidate(r)]
    for record in records:
        if record['decision_notice']:
            record['decision'] = decision_review.disposition([record], company, model)
    catalog = {e['event']: e for e in previous}
    groups, result = defaultdict(list), {}
    # Include the full current source-review budget without overflowing the local model context.
    batch_size = 5
    for offset in range(0, len(records), batch_size):
        batch = records[offset:offset + batch_size]
        # Nearest dated anchors keep long historical searches within the model context.
        anchors = sorted(catalog.values(), key=lambda e: abs((_day(e['date']) - _day(batch[0]['published'])).days))[:60]
        allowed = {r['id'] for r in batch}
        leads = [dict(r, sources=source_sample(r["sources"], article_translation.SOURCE_REVIEW_LIMIT), corroboration=[]) for r in batch]
        response = model("Group coverage by concrete company incident, across languages and publishers. "
            "Use the supplied company name verbatim or its original ticker. Never translate a proper name. "
            "Return every input id once. Use an existing event ID for the same incident, otherwise use the first "
            "article ID of the incident in this batch. Score each standalone story -1 negative, 0 neutral, +1 positive. Distinguish different sites, cases, periods and subsequent "
            "decisions. Group reports of a ruling and its implementation separately from an earlier petition. "
            "Do not discard a material partial release while restrictions remain. Use event 0 for unrelated, purely favourable or background stories without an event. "
            "Assess each issuer's realised profit separately from its revenue, operating cash flow and outlook. "
            "If the issuer reported falling profit or earnings for the period covered, copy a contiguous verbatim passage "
            "including the profit measure and its decrease into profit_decline_quote, otherwise use an empty string. "
            "Read the native headline, summary and source passages. Exclude forecasts, peers, incidental older comparisons "
            "and changes in profit growth rates alone. Keep reported profit declines as adverse events even when revenue "
            "rose. Group by issuer and reporting period; annual and quarterly earnings releases are distinct events. "
            "Never infer corporate wrongdoing from an owner's case. Give each event a specific English title.",
            {"company": company, "company_names": labels, "existing": anchors, "leads": leads},
            _GROUPS)["assignments"]
        if (len(response) != len(batch) or {a['id'] for a in response} != allowed or
                any(type(a["sentiment"]) is not int or a["sentiment"] not in (-1, 0, 1) or type(a['event']) is not int or a['event'] not in allowed | {e['event'] for e in anchors} | {0}
                    or not isinstance(a['title'], str) for a in response)):
            raise ValueError("Incomplete event grouping")
        by_id = {a['id']: a for a in response}
        profit_quotes = reported_profit_declines(leads, by_id, company, model)
        for r in batch:
            assignment = by_id[r['id']]
            key = assignment['event']
            if key in allowed and by_id[key]['event'] != key:
                raise ValueError("Conflicting event membership")
            anchor = catalog.get(key) or next((v for v in batch if v['id'] == key), {})
            if (r['decision_notice'] and (anchor.get('petition') or company_developments.petition_notice(anchor)) and
                    not company_developments.decision_notice(anchor) and
                    (anchor.get('date') or anchor.get('published') or '') < r['published']):
                key = r['id']
            protected = r["status"] == "supported" or r["reviewed"]
            direction = r['score'] if protected else assignment['sentiment']
            quote = profit_quotes.get(r['id'], '').strip()
            if quote:
                r['profit_decline_quote'] = quote
                direction = -1
                key = key or r['id']
            decision = r.get('decision') or {}
            if not protected and decision.get('status') == 'documented' and decision.get('residual_adverse'):
                direction = -1
                key = key or r['id']
            if not protected and decision.get('status') == 'unconfirmed' and not key and direction == 0:
                key = r['id']
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
        notices = sorted((r for r in members if r['decision_notice']),key=lambda r:r['published'],reverse=True)
        ruling = notices[0].get('decision') if notices else None
        if ruling and ruling['status']=='unconfirmed' and len(members)>1:
            ruling = decision_review.disposition(members, company, model)
        if ruling and ruling['status'] == 'unconfirmed':
            title = company + ' · ' + next(r['title'] for r in members if r['url'] == ruling['notice_url'])
            summary = (f"Coverage dated {ruling['as_of']} reports a decision involving {company}. "
                       "RA could not establish its terms from readable source text. The notice alone does not establish "
                       "the relief granted, any continuing restrictions, or the financial effect on the company.")
            yield {r['id']:dict(event=key, sentiment=r['score'], title=title[:250], summary=summary,
                                decision=ruling) for r in members}
            continue
        sample = sorted(members, key=lambda r: (not r['decision_notice'], not r.get('profit_decline_quote'), r['status'] != 'supported', r['score'], r['id']))
        sample = sample[:5]
        facts = [dict(r, sources=source_sample(r['sources'], 22000 // len(sample)), corroboration=[]) for r in sample]
        instruction = ("Write an English analyst brief of 150–250 words about this one company event. "
            "Describe what happened, distinguish event dates from publication dates, analyse severity and limits. "
            "For event_day give YYYY-MM-DD for the action being reported only when its exact date appears in source text. "
            "For a results announcement use its release date. Omit reporting-period ends, background dates and future deadlines. "
            "Use an empty string if the action date is uncertain. "
            "Include one supplied company name or ticker verbatim in both title and summary. Never translate company names. "
            "Use only the supplied evidence. Distinguish allegations, source statements and supported impacts. "
            "Do not invent facts, assume that silence means safety, or treat repeated coverage as greater damage. "
            "Do not claim that a filing omits details on the basis of excerpts. Identify the company’s role. "
            "Separate when the company learned of the event from when it disclosed it. Attribute contested facts. "
            "Describe uncertainty where needed. Return title and summary.")
        payload = {"company": company, "company_names": labels, "event": catalog[key], "coverage": len(members),
                   "publication_dates": sorted({r['published'] for r in members}), "evidence": facts}
        earnings = any(r.get('profit_decline_quote') for r in members)
        if earnings:
            instruction += (" State the reported profit decline in the title and summary. Give its reporting period, "
                            "magnitude and comparison when supplied. Keep revenue changes separate. Attribute figures "
                            "from a search excerpt to the publisher and disclose when its full text was unavailable. "
                            "A publisher date alone does not establish when the issuer released its results. "
                            "Call it the article date unless the source separately identifies a release date. "
                            "Write for a credit analyst. Omit commentary on drafting instructions, word choice, "
                            "issuer naming, unrelated allegations and when management learned its own figures. "
                            "Distinguish cost drivers from balance-sheet movements; do not infer causation.")
        if ruling:
            payload["decision"] = ruling
            instruction += (" Describe the latest dated disposition and any residual exposure, attributing it to the document. "
                            "State the decision_day and notice publication date separately. If decision_day is unknown, never equate the two dates. "
                            "Never present the original application as still awaiting that same ruling.")
        draft = model(instruction, payload, _DRAFT)
        def valid(draft):
            title, summary = draft.get("title"), draft.get("summary")
            return (isinstance(title, str) and isinstance(summary, str) and
                    (not earnings or all(re.search(r'profit|earnings|income|loss', field, re.I) and
                                         re.search(r'declin|fall|fell|drop|decreas|slid|slip|down|lower|shrank|shrink|plung|dip|contract|widen|reduc', field, re.I)
                                         for field in (title, summary))) and
                    150 <= len(summary.split()) <= 250 and matcher and
                    matcher.search(title[:250]) and matcher.search(summary) and
                    (not ruling or (not decision_review.PENDING.search(summary) and
                     (not ruling.get('decision_day') or decision_review.mentions_day(summary, ruling['decision_day'])))))
        if not valid(draft):
            draft = model(instruction + " Rewrite to 180–210 words. Copy the supplied issuer name or ticker into both fields.", payload, _DRAFT)
        aligned = True
        if ruling and valid(draft):
            verdict = model("Check the English brief against the dated ruling and its quoted evidence. "
                            "Accept only if the brief accurately conveys the outcome and every reported residual restriction, "
                            "the right parties, assets, quantities and dates. Reject any suggestion that the notice and hearing date coincide "
                            "without explicit evidence, or that the original application still awaits a ruling.",
                            {"brief":draft, "decision":ruling}, keywords.object_schema({"consistent":{"type":"boolean"}}))
            aligned = verdict.get("consistent") is True
        if ruling and (not aligned or not valid(draft)):
            # Give the analyst the dated source record even if the model repeats a stale legal status.
            draft = {"title": company + " · reported decision", "summary":
                     ("Decision dated " + ruling['decision_day'] + ". " if ruling.get('decision_day') else "") +
                     "The notice was published on " + ruling["as_of"] + ". " +
                     ruling["outcome"] + " " + ruling["remaining"]}
        elif not valid(draft):
            raise ValueError("Incomplete event narrative or missing company identity")
        day = draft.get('event_day')
        if (not day or ruling or day > max(payload['publication_dates']) or
                not any(decision_review.mentions_day(s['text'], day, s.get('language', 'en'))
                for fact in facts for s in fact['sources'])):
            day = None
        yield {r['id']: {"event": key, "sentiment": r["score"], "title": draft['title'][:250],
                         **({"profit_decline_quote": r["profit_decline_quote"]} if r.get("profit_decline_quote") else {}),
                         **({"event_day": day} if day else {}),
                         "summary": draft['summary'].strip(), **({"decision":ruling} if ruling else {})} for r in members}


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
        sources = sorted(members, key=lambda r: (r.get('published_at') is None, r.get('published_at'), r['id']))
        first_publication = sources[0].get('published_at')
        event_day = (draft.get('decision') or {}).get('decision_day') or draft.get('event_day')
        day = first_publication.astimezone(WEEK_TIMEZONE).date() if first_publication else None
        if event_day:
            day = _day(event_day)
        result.append(dict(country=countries.get(company, countries.get(name, 'Unspecified')), company=name, key=key,
                           title=draft.get('title') or members[0].get('title_en') or members[0]['title'],
                           summary=draft.get('summary'), decision=draft.get('decision'), score=score['score'], reason=score['reason'],
                           day=day, date_label='Event' if event_day else 'First coverage',
                           priority=score['priority'], sources=sources))
    return sorted(result, key=lambda e: (e['day'] is None, e['day'], e['country'].casefold(), e['company'].casefold(),
                                         tuple(-int(i) for i in e['priority']), e['title'], e['key']))


def process(run_id):
    from . import score_store
    run = db.get_run(run_id)
    rows = db.get_results(run_id)
    companies = defaultdict(list)
    for row in rows:
        companies[row['subject_key']].append(row)
    for members in companies.values():
        pending = [r for r in members if not r.get('event_draft') and triage_candidate(r)]
        if not pending:
            continue
        with db.connect() as conn:
            documents = {r['url']: r['document'] for r in conn.execute(
                "SELECT url,document FROM article_documents WHERE url=ANY(%s)",
                ([r['verification'].get('requested_url') for r in members if r.get('verification')],))}
            subject = db.subject_for(members[0], run["subjects"])
            for row in members:
                check = row.get('verification') or {}
                row['event_sources'] = source_texts(documents.get(check.get('requested_url'), {}), check, subject)
            by_url = {canonical_url(r['url']): r for r in members}
            for row in pending:
                row['decision_sources'] = [src for url in row.get('decision_followup_urls', [])
                                           if (followup := by_url.get(url)) and
                                           (followup.get('verification') or {}).get('status') != 'unmatched'
                                           for src in followup['event_sources']]
            members = pending
            prior = conn.execute("""SELECT DISTINCT ON (event_draft->>'event')
                (event_draft->>'event')::bigint AS event, event_draft->>'title' AS title,
                published_at::date::text AS date FROM search_results WHERE (company_id=%s OR (company_id IS NULL AND run_id=%s))
                AND event_draft ? 'event' AND published_at BETWEEN %s::timestamptz - interval '30 days'
                AND %s::timestamptz + interval '30 days'""",
                (members[0]['company_id'], run_id, min((r['published_at'] for r in members if r['published_at']), default=None),
                 max((r['published_at'] for r in members if r['published_at']), default=None))).fetchall()
        try:
            for drafts in draft_company(subject, members, prior):
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
            (id>%s OR (score_status='reviewed' AND score_included AND score<=0))) THEN 'pending' ELSE 'complete' END
            WHERE id=%s""", (run_id, max((r['id'] for r in rows), default=0), run_id))


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
