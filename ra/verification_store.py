"""Cached source evidence, a durable user-initiated queue and grouping corrections."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from . import local_models, article_translation, company_developments, db, risk_factors, score_store, search_settings
from .article_content import READER_VERSION, canonical_url, retrieve, validate_pdf_text
from .language import detect_language
from .verification import verify

logger = logging.getLogger(__name__)

_thread = None
_wake = threading.Event()
_start_lock = threading.Lock()


def verify_article(result_id, fetcher=retrieve, source_url=None):
    with local_models.batch(f"article {result_id}"):
        flow = _verification(result_id, fetcher, source_url)
        doc, subject = next(flow)
        if doc.get('linked_documents'):
            sources = [doc, *doc['linked_documents']]
            parent, *children = local_models.run_phases(
                (article_translation.prepare_steps(source, subject) for source in sources), f"article {result_id}")
            prepared = dict(parent, linked_documents=children)
        else:
            prepared = article_translation.prepare(doc, subject)
        return _finish(flow, prepared)


def _finish(flow, prepared):
    try:
        flow.send(prepared)
    except StopIteration as done:
        return done.value
    raise RuntimeError("Unexpected additional verification stage")


def _verification(result_id, fetcher, source_url):
    started = stage_started = time.perf_counter()
    stage, seconds = "retrieval_cache", {}
    cache_hit, completed = False, False

    def next_stage(name):
        nonlocal stage, stage_started
        now = time.perf_counter()
        seconds[stage] = round(now - stage_started, 6)
        stage, stage_started = name, now

    try:
        article = score_store.get_article(result_id)
        if not article or article['run_status'] in ('running', 'queued'):
            raise ValueError('Wait for the search to finish')
        run = db.get_run(article['run_id'])
        url = canonical_url(source_url or article['url'])
        with db.connect() as conn:
            cached = conn.execute('SELECT * FROM article_documents WHERE url=%s', (url,)).fetchone()
        lifetime = timedelta(days=7) if cached and cached['document']['status'] == 'available' else timedelta(hours=24)
        hint = {"title": article["title"], "publisher": article["publisher"],
                "published_at": article["published_at"].isoformat() if article["published_at"] else ""}
        if article.get("filing_attachments"):
            hint["filing_attachments"] = article["filing_attachments"]
        upgrade = (cached
                   and cached['document'].get('reader_version') in ('public-source-v4', 'public-source-v5', 'public-source-v6', 'public-source-v8')
                   and cached['document']['status'] == 'available'
                   and urlsplit(url).hostname not in ('kap.org.tr', 'www.kap.org.tr')
                   and (cached['document'].get('reader_version') in ('public-source-v6', 'public-source-v8')
                        or not url.startswith(('https://dpm.vn/cong-bo-thong-tin/',
                                               'https://dpm.vn/bao-cao-tai-chinh/'))))
        if (cached and cached['document'].get('source_hint') == hint
                and (cached['document'].get('reader_version') == READER_VERSION or upgrade)
                and datetime.now(timezone.utc) - cached['fetched_at'] < lifetime):
            cache_hit = True
            document = cached['document']
            old_language = document.get('language')
            revisit_language = (old_language in ('uz', 'kk', '') and document.get('body')
                                and detect_language(document['body'][:4000]) != old_language)
            if upgrade or revisit_language:
                # Retain checked text; v7 changes KAP, v6 issuer attachments, v5 validation/language.
                document = {**document, 'reader_version': READER_VERSION}
                try:
                    if document.get('method', '').startswith('PDF text'):
                        validate_pdf_text(document['body'])
                except ValueError as exc:
                    document.update(status='unavailable', reason=str(exc))
                    document.pop('translation', None)
                else:
                    document['language'] = detect_language(document['body'][:4000])
                    if document.get('translation', {}).get('language') != document['language']:
                        document.pop('translation', None)
                with db.connect() as conn:
                    conn.execute('UPDATE article_documents SET document=%s '
                                 'WHERE url=%s AND document=%s AND fetched_at=%s',
                                 (Jsonb(document), url, Jsonb(cached['document']), cached['fetched_at']))
        else:
            fetched = retrieve(url, hint) if fetcher is retrieve else fetcher(url)
            document = {**fetched, "reader_version": READER_VERSION, "source_hint": hint}
            if (cached and document.get('status') == 'available' and document.get('body')
                    and cached['document'].get('translation')
                    and all(document.get(key) == cached['document'].get(key)
                            for key in ('body', 'sha256', 'language'))):
                document['translation'] = cached['document']['translation']
            with db.connect() as conn:
                conn.execute('INSERT INTO article_documents (url,document) VALUES (%s,%s) ON CONFLICT (url) '
                             'DO UPDATE SET document=EXCLUDED.document,fetched_at=now()', (url, Jsonb(document)))
        next_stage("translation_review")
        subject = db.evidence_subject(article, run)
        original = document
        document = article_translation.bounded_source(original, subject, company_developments.decision_notice(article),
                                                      per_document=run['per_document_limit'])
        bounded = document is not original
        if bounded:
            prior = (article.get('verification') or {}).get('review_document') or {}
            previous = {d.get('final_url'): d for d in [prior, *prior.get('linked_documents', [])]}
            for doc in [document, *document['linked_documents']]:
                saved = previous.get(doc.get('final_url'), {})
                if saved.get('translation') and saved.get('body') == doc.get('body') and saved.get('language') == doc.get('language'):
                    doc['translation'] = saved['translation']
        prepared = yield document, subject
        if not bounded and prepared != document:
            with db.connect() as conn:
                conn.execute('UPDATE article_documents SET document=%s WHERE url=%s AND document=%s',
                             (Jsonb(prepared), url, Jsonb(document)))
        document = prepared
        next_stage("attribution")
        checked_docs = [(doc, verify(doc, subject, article)) for doc in
                        [document, *document.get('linked_documents', [])]]
        supported = [(doc, check) for doc, check in checked_docs if check['status'] == 'supported']
        document, assessment = (supported or checked_docs)[0]
        if len(checked_docs) > 1:
            assessment['sources'] = [{'url': check['source_url'], 'status': check['status'],
                                      'reason': check['reason'], 'hash': check['document_hash']} for _, check in checked_docs]
            if len({check['score'] for _, check in supported}) > 1:
                assessment.update(status='needs_review', reason='Publisher and linked evidence disagree on direction')
        if bounded:
            assessment['review_scope'] = prepared['review_scope']
            assessment['review_document'] = prepared
        next_stage("themes")
        settings = search_settings.for_run(run)
        themes = risk_factors.cached_assess(article, document, subject, [r for r in risk_factors.records()
                                           if r["enabled"] and search_settings.scope_enabled(r["scope"], settings)])
        next_stage("persistence")
        assessment['requested_url'] = url
        from .bilingual import enqueue_prose
        with db.connect() as conn:
            enqueue_prose(conn, assessment, subject)
            enqueue_prose(conn, themes, subject)
            for doc in [prepared, *prepared.get('linked_documents', [])]:
                enqueue_prose(conn, doc.get('translation', {}), subject)
                if doc.get('excerpt') or doc.get('review_only'):
                    from .bilingual import enqueue
                    enqueue(conn, doc.get('body'), doc.get('language') or '', subject)
        with db.connect() as conn:
            score_store._lock(conn)
            current = conn.execute('SELECT verification FROM search_results WHERE id=%s FOR UPDATE', (result_id,)).fetchone()
            conn.execute('UPDATE search_results SET risk_factor_review=%s WHERE id=%s', (Jsonb(themes), result_id))
            if current['verification'] != assessment:
                conn.execute('INSERT INTO article_verification_history (result_id,previous,assessment) VALUES (%s,%s,%s)',
                             (result_id, Jsonb(current['verification']), Jsonb(assessment)))
                conn.execute('UPDATE search_results SET verification=%s WHERE id=%s', (Jsonb(assessment), result_id))
            score_store._assess(conn, article['run_id'])
            score_store._weekly(conn)
        completed = True
        return assessment
    finally:
        ended = time.perf_counter()
        seconds[stage] = round(ended - stage_started, 6)
        logger.info("source_timing %s", json.dumps({
            "result_id": result_id, "cache_hit": cache_hit, "completed": completed,
            "failed_stage": None if completed else stage,
            "seconds": seconds, "total_seconds": round(ended - started, 6),
        }, sort_keys=True))


def enqueue(result_id, source_url=''):
    article = score_store.get_article(result_id)
    if not article or article['run_status'] in ('running', 'queued'):
        raise ValueError('Wait for the search to finish')
    if source_url:
        try:
            parsed = urlsplit(source_url)
            valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                     and parsed.port in (443, None) and len(source_url) <= 4096)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Use a public HTTPS publisher URL')
    with db.connect() as conn:
        queued = conn.execute("INSERT INTO verification_jobs (result_id,source_url) VALUES (%s,%s) ON CONFLICT (result_id) "
                              "DO UPDATE SET status='queued',source_url=EXCLUDED.source_url,error='',updated_at=now() "
                              "WHERE verification_jobs.status NOT IN ('queued','running')", (result_id, source_url))
        if queued.rowcount:
            conn.execute("DELETE FROM article_documents WHERE url=%s AND document->>'status'<>'available'",
                         (canonical_url(source_url or article['url']),))
    _wake.set()


def enqueue_run(run_id, limit_per_company=8):
    run = db.get_run(run_id)
    if not run or run['status'] in ('running', 'queued'):
        raise ValueError('Wait for the search to finish')
    rows = db.get_results(run_id)
    # Prefer accessible publisher sources, then strongest headline cues and recent dates.
    rows.sort(key=lambda a: (a.get('source_kind') == 'context', urlsplit(a['url']).hostname == 'news.google.com',
                            -abs(a.get('score') or 0), -(a['published_at'] or a['created_at']).timestamp(), a['id']))
    counts, total = {}, 0
    for article in rows:
        if (article.get('verification') or {}).get('status', 'pending') != 'pending':
            continue
        key = article['subject_key']
        if limit_per_company is not None and counts.get(key, 0) >= limit_per_company:
            continue
        with db.connect() as conn:
            added = conn.execute('INSERT INTO verification_jobs (result_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING result_id',
                                 (article['id'],)).fetchone()
        if added:
            counts[key] = counts.get(key, 0) + 1
            total += 1
    _wake.set()
    return total


def job_summary(run_id):
    with db.connect() as conn:
        counts = conn.execute("""
            SELECT count(*) AS total,
              count(DISTINCT (r.subject_key, r.event_draft->>'event')) FILTER (WHERE r.event_draft ? 'event') AS event_briefs,
              max(GREATEST(r.score_updated_at,
                  CASE WHEN j.status IN ('completed','failed') THEN j.updated_at END))::text AS revision,
              count(*) FILTER (WHERE j.status='queued') AS queued,
              count(*) FILTER (WHERE j.status='running') AS running,
              count(*) FILTER (WHERE j.status='failed') AS failed,
              count(*) FILTER (WHERE j.status='completed' OR
                (j.status IS NULL AND r.verification->>'status'<>'pending')) AS completed,
              count(*) FILTER (WHERE r.verification->>'status' IN ('unavailable','needs_review')
                                OR j.status='failed') AS attention,
              count(*) FILTER (WHERE (r.verification->>'status'='unavailable' OR j.status='failed'
                                OR r.verification->'translation'->>'status' IN ('needs_review','unavailable')
                                OR r.verification->>'reason'='Full-article language is not supported by attribution rules; analyst reading required')
                                AND COALESCE(j.status,'') NOT IN ('queued','running')) AS source_retryable,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx') AND r.title_en='') AS missing,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx') AND r.title_en<>'') AS translations,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx') AND r.title_en<>''
                                AND r.headline_review->>'status' IN ('faithful','material_error','uncertain')) AS reviewed,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx')
                                AND r.headline_review->>'status' IN ('unavailable','inconclusive')) AS review_failed,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx')
                                AND (r.title_en='' OR COALESCE(r.headline_review->>'status','pending')
                                     IN ('pending','unavailable','inconclusive'))
                                AND COALESCE((r.headline_review->>'retry_count')::integer,0)<3) AS review_pending,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx')
                                AND (r.title_en='' OR COALESCE(r.headline_review->>'status','pending')
                                     IN ('pending','unavailable','inconclusive'))
                                AND COALESCE((r.headline_review->>'retry_count')::integer,0)>=3) AS review_exhausted,
              count(*) FILTER (WHERE lower(split_part(r.language,'-',1)) NOT IN ('en','zxx')
                                AND r.headline_review->>'status' IN ('material_error','uncertain')) AS review_attention
            FROM search_results r LEFT JOIN verification_jobs j ON j.result_id=r.id
            WHERE r.run_id=%s""", (run_id,)).fetchone()
        counts.update(conn.execute("""SELECT count(*) AS rss_total,count(*) FILTER(WHERE status='recovered') AS rss_recovered,
            (SELECT count(*) FROM rss_brave_attempts WHERE run_id=%s AND outcome<>'requested') AS brave_finished
            FROM rss_recovery WHERE run_id=%s""", (run_id,run_id)).fetchone())
    counts['translation_total'] = counts['translations'] + counts['missing']
    for done, total, percent in ((counts['completed'] + counts['failed'], 'total', 'percent'),
                                 (counts['reviewed'], 'translation_total', 'translation_percent')):
        counts[percent] = 100 * done // counts[total] if counts[total] else 0
    return counts


def details(result_id):
    with db.connect() as conn:
        article = conn.execute('SELECT * FROM search_results WHERE id=%s', (result_id,)).fetchone()
        members = conn.execute('SELECT * FROM search_results WHERE event_group=%s AND event_group<>\'\' ORDER BY id',
                               (article['event_group'],)).fetchall()
        checked = article['verification'] or {}
        source = conn.execute(
            "SELECT document FROM article_documents WHERE url=%s",
            (checked.get('requested_url'),)
        ).fetchone() if checked.get('document_hash') else None
        document = checked.get('review_document') or (source['document'] if source else {})
        document = next((d for d in [document, *document.get('linked_documents', [])]
                         if d.get('sha256') == checked.get('document_hash')), {})
        return {'recovered_body': document.get('body', '') if document.get('review_only') else '',
                'body_translation': document.get('translation', {}),
                'verification_history': conn.execute('SELECT * FROM article_verification_history WHERE result_id=%s ORDER BY id DESC',
                                                     (result_id,)).fetchall(),
                'event_history': conn.execute('SELECT * FROM article_event_history WHERE result_id=%s ORDER BY id DESC',
                                              (result_id,)).fetchall(),
                'job': conn.execute('SELECT * FROM verification_jobs WHERE result_id=%s', (result_id,)).fetchone(),
                'members': members}


def retry_failed_run(run_id):
    with db.connect() as conn:
        run = conn.execute('SELECT status FROM search_runs WHERE id=%s FOR UPDATE', (run_id,)).fetchone()
        if not run or run['status'] in ('queued', 'running'):
            raise ValueError('Wait for the search to finish before retrying source checks.')
        jobs = conn.execute("""
            INSERT INTO verification_jobs (result_id)
            SELECT r.id FROM search_results r LEFT JOIN verification_jobs j ON j.result_id=r.id
            WHERE r.run_id=%s AND (j.status='failed' OR r.verification->>'status'='unavailable'
              OR r.verification->'translation'->>'status' IN ('needs_review','unavailable')
              OR r.verification->>'reason'='Full-article language is not supported by attribution rules; analyst reading required')
              AND COALESCE(j.status,'') NOT IN ('queued','running')
            ON CONFLICT (result_id) DO UPDATE SET status='queued',error='',updated_at=now()
            WHERE verification_jobs.status NOT IN ('queued','running')
            RETURNING result_id,source_url
        """, (run_id,)).fetchall()
        for job in jobs:
            article = conn.execute('SELECT url FROM search_results WHERE id=%s', (job['result_id'],)).fetchone()
            url = canonical_url(job['source_url'] or article['url'])
            # Failed cached fetches must reach the publisher again on an explicit retry.
            conn.execute("DELETE FROM article_documents WHERE url=%s AND document->>'status'<>'available'", (url,))
    _wake.set()
    return len(jobs)


def review_group(result_id, action, target_id, reason):
    reason = reason.strip()
    if action not in ('merge', 'separate', 'automatic') or not 5 <= len(reason) <= 2000:
        raise ValueError('Choose a grouping action and explain it in 5–2,000 characters')
    with db.connect() as conn:
        score_store._lock(conn)
        article = conn.execute('SELECT r.*,s.status AS run_status FROM search_results r JOIN search_runs s ON s.id=r.run_id '
                               'WHERE r.id=%s FOR UPDATE OF r', (result_id,)).fetchone()
        if not article or article['run_status'] in ('running', 'queued'):
            raise ValueError('Wait for the search to finish')
        rows = [article]
        group = '' if action == 'automatic' else f'manual:{result_id}'
        if action == 'merge':
            target = conn.execute('SELECT r.*,s.status AS run_status FROM search_results r JOIN search_runs s ON s.id=r.run_id '
                                  'WHERE r.id=%s FOR UPDATE OF r', (target_id,)).fetchone()
            if (not target or not article['company_id'] or target['company_id'] != article['company_id']
                    or target['id'] == result_id or target['run_status'] in ('running', 'queued')):
                raise ValueError('Target must be a different article for the same saved company in a finished search')
            group = target['event_group_manual'] or f'manual:{target_id}'
            rows += conn.execute('SELECT * FROM search_results WHERE company_id=%s AND '
                                 '(id=%s OR (event_group=%s AND event_group<>\'\')) FOR UPDATE',
                                 (article['company_id'], target_id, target['event_group'])).fetchall()
        for row in {r['id']: r for r in rows}.values():
            conn.execute('UPDATE search_results SET event_group_manual=%s WHERE id=%s', (group, row['id']))
            conn.execute('INSERT INTO article_event_history (result_id,previous_group,new_group,reason) VALUES (%s,%s,%s,%s)',
                         (row['id'], row['event_group'], group, reason))
        score_store._weekly(conn)


def start():
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive():
            return
        with db.connect() as conn:
            conn.execute("UPDATE verification_jobs SET status='queued' WHERE status='running'")
        _thread = threading.Thread(target=_loop, daemon=True, name='ra-source-verification')
        _thread.start()


def process_jobs(run_id):
    """Take a single queue snapshot. Late discoveries remain queued for the next pass."""
    from . import headline_review
    with local_models.batch(f"run {run_id}"):
        with db.connect() as conn:
            jobs = conn.execute("""UPDATE verification_jobs j SET status='running',updated_at=now()
                FROM search_results r WHERE j.result_id=r.id AND r.run_id=%s AND j.status='queued'
                RETURNING j.result_id,j.source_url,j.translation_checkpoint""", (run_id,)).fetchall()
        if not jobs:
            return
        flows, documents, failed = {}, {}, set()

        def prepare_source(key, index, doc, subject, saved):
            def checkpoint(value):
                with db.connect() as conn:
                    conn.execute("""UPDATE verification_jobs SET translation_checkpoint=translation_checkpoint || %s
                        WHERE result_id=%s AND status='running'""", (Jsonb({str(index): value}), key))
            try:
                if saved and saved.get('source_hash') == article_translation.fingerprint(doc.get('body') or ''):
                    doc = dict(doc, translation=saved)
                documents[key][index] = yield from article_translation.prepare_steps(doc, subject, checkpoint=checkpoint)
            except Exception:
                logger.exception("Source preparation failed for lead %s", key)
                failed.add(key)

        def headline_checks():
            yield local_models.QWEN
            headline_review.review_saved(run_id)

        def tasks():
            yield headline_checks()
            for job in jobs:
                key = job['result_id']
                try:
                    flow = _verification(key, retrieve, job['source_url'])
                    parent, subject = next(flow)
                    flows[key] = flow
                    sources = [parent, *parent.get('linked_documents', [])]
                    documents[key] = list(sources)
                    for index, source in enumerate(sources):
                        yield prepare_source(key, index, source, subject, job['translation_checkpoint'].get(str(index)))
                except Exception:
                    logger.exception("Source retrieval failed for lead %s", key)
                    failed.add(key)

        try:
            local_models.run_phases(tasks(), f"run {run_id}")
            for job in jobs:
                key = job['result_id']
                if key not in failed:
                    try:
                        parent, *children = documents[key]
                        _finish(flows[key], dict(parent, linked_documents=children) if children else parent)
                    except Exception:
                        logger.exception("Source verification failed for lead %s", key)
                        failed.add(key)
                status = 'failed' if key in failed else 'completed'
                error = 'Verification could not finish; retry after checking the source' if key in failed else ''
                with db.connect() as conn:
                    conn.execute("UPDATE verification_jobs SET status=%s,error=%s,translation_checkpoint='{}',updated_at=now() WHERE result_id=%s",
                                 (status,error,key))
        finally:
            for flow in flows.values():
                flow.close()
            with db.connect() as conn:
                conn.execute("UPDATE verification_jobs SET status='queued' WHERE result_id=ANY(%s) AND status='running'",
                             ([job['result_id'] for job in jobs],))


def next_run():
    with db.connect() as conn:
        row = conn.execute("""SELECT r.run_id FROM verification_jobs j JOIN search_results r ON r.id=j.result_id
            JOIN search_runs s ON s.id=r.run_id WHERE j.status='queued' AND s.status IN ('completed','partial')
            ORDER BY j.updated_at,j.result_id LIMIT 1""").fetchone()
    return row['run_id'] if row else None


def _loop():
    while True:
        try:
            run_id = next_run()
            if run_id is not None:
                from . import event_analysis
                with local_models.batch(f"run {run_id}"):
                    process_jobs(run_id)
                    event_analysis.finish_pending(run_id)
                continue
        except Exception:
            logger.exception("Could not process queued source verification")
        _wake.wait(5)
        _wake.clear()
