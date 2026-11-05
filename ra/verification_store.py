"""Cached source evidence, a durable user-initiated queue and grouping corrections."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from . import db, score_store
from .article_content import READER_VERSION, canonical_url, retrieve
from .verification import verify

_thread = None
_wake = threading.Event()
_start_lock = threading.Lock()


def subject_for(article, run):
    return next((s for s in run['subjects'] if s.get('id') == article['company_id'] and article['company_id']), None) or next(
        (s for s in run['subjects'] if s['name'] == article['subject_name']), {'name': article['subject_name']})


def verify_article(result_id, fetcher=retrieve, source_url=None):
    article = score_store.get_article(result_id)
    if not article or article['run_status'] in ('running', 'queued'):
        raise ValueError('Wait for the search to finish')
    run = db.get_run(article['run_id'])
    url = canonical_url(source_url or article['url'])
    with db.connect() as conn:
        cached = conn.execute('SELECT * FROM article_documents WHERE url=%s', (url,)).fetchone()
    lifetime = timedelta(days=7) if cached and cached['document']['status'] == 'available' else timedelta(hours=24)
    if (cached and cached['document'].get('reader_version') == READER_VERSION
            and datetime.now(timezone.utc) - cached['fetched_at'] < lifetime):
        document = cached['document']
    else:
        document = {**fetcher(url), "reader_version": READER_VERSION}
        with db.connect() as conn:
            conn.execute('INSERT INTO article_documents (url,document) VALUES (%s,%s) ON CONFLICT (url) '
                         'DO UPDATE SET document=EXCLUDED.document,fetched_at=now()', (url, Jsonb(document)))
    assessment = verify(document, subject_for(article, run), article)
    assessment['requested_url'] = url
    with db.connect() as conn:
        score_store._lock(conn)
        current = conn.execute('SELECT verification FROM search_results WHERE id=%s FOR UPDATE', (result_id,)).fetchone()
        if current['verification'] != assessment:
            conn.execute('INSERT INTO article_verification_history (result_id,previous,assessment) VALUES (%s,%s,%s)',
                         (result_id, Jsonb(current['verification']), Jsonb(assessment)))
            conn.execute('UPDATE search_results SET verification=%s WHERE id=%s', (Jsonb(assessment), result_id))
        score_store._assess(conn, article['run_id'])
        score_store._weekly(conn)
    return assessment


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
        conn.execute("INSERT INTO verification_jobs (result_id,source_url) VALUES (%s,%s) ON CONFLICT (result_id) "
                     "DO UPDATE SET status='queued',source_url=EXCLUDED.source_url,error='',updated_at=now() "
                     "WHERE verification_jobs.status NOT IN ('queued','running')", (result_id, source_url))
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
        if counts.get(key, 0) >= limit_per_company:
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
        return {r['status']: r['n'] for r in conn.execute(
            'SELECT j.status,count(*) AS n FROM verification_jobs j JOIN search_results r ON r.id=j.result_id '
            'WHERE r.run_id=%s GROUP BY j.status', (run_id,)).fetchall()}


def details(result_id):
    with db.connect() as conn:
        article = conn.execute('SELECT * FROM search_results WHERE id=%s', (result_id,)).fetchone()
        members = conn.execute('SELECT * FROM search_results WHERE event_group=%s AND event_group<>\'\' ORDER BY id',
                               (article['event_group'],)).fetchall()
        return {'verification_history': conn.execute('SELECT * FROM article_verification_history WHERE result_id=%s ORDER BY id DESC',
                                                     (result_id,)).fetchall(),
                'event_history': conn.execute('SELECT * FROM article_event_history WHERE result_id=%s ORDER BY id DESC',
                                              (result_id,)).fetchall(),
                'job': conn.execute('SELECT * FROM verification_jobs WHERE result_id=%s', (result_id,)).fetchone(),
                'members': members}


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


def _loop():
    while True:
        try:
            with db.connect() as conn:
                job = conn.execute("SELECT * FROM verification_jobs WHERE status='queued' ORDER BY updated_at,result_id "
                                   "LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
                if job:
                    conn.execute("UPDATE verification_jobs SET status='running',updated_at=now() WHERE result_id=%s", (job['result_id'],))
            if not job:
                _wake.wait(5)
                _wake.clear()
                continue
            try:
                verify_article(job['result_id'], source_url=job['source_url'])
                status, error = 'completed', ''
            except Exception:
                status, error = 'failed', 'Verification could not finish; retry after checking the source'
            with db.connect() as conn:
                conn.execute('UPDATE verification_jobs SET status=%s,error=%s,updated_at=now() WHERE result_id=%s',
                             (status, error, job['result_id']))
        except Exception:
            _wake.wait(5)
            _wake.clear()
