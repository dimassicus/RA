"""Replay missed Google searches into the original run, with one Brave fallback per company."""
import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.client import HTTPException
from xml.etree import ElementTree

from . import db, exposure, monthly_backfill, news_apis, recovery_providers, rss_gate, rss_windows, search, search_settings
from .aliases import provider_name
from .article_content import canonical_url

log = logging.getLogger(__name__)
LEGACY_SUFFIX = ': HTTPError: HTTP Error 503: Service Unavailable'


def bounds(run):
    end = run['window_end'] or run['created_at']
    return run['window_start'] or end - timedelta(days=7), end


def subject_key(run_id, subject):
    return str(subject.get('id') or f'one-off:{run_id}')


def record(conn, run_id, key, query, edition, window, *, context=None, unknown=None):
    request = dict(query=query, edition=edition, window=[d.isoformat() for d in window])
    if edition is not None:
        request['url'], request['language'] = search.feed_url(query, edition, window=window)
    if context:
        request['context'] = context
    if unknown is not None:
        request['legacy_error_id'] = unknown
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    conn.execute("""INSERT INTO rss_recovery(run_id,subject_key,request,fingerprint,status)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (run_id, key, db.Jsonb(request), fingerprint, 'unknown_edition' if unknown else 'pending'))


def defer(run, subjects, query, edition, window, context=None):
    with db.connect() as conn:
        for subject in subjects:
            record(conn, run['id'], subject_key(run['id'], subject), query, edition,
                   window or bounds(run), context=context)


def import_legacy(run_id):
    """Older releases saved query text without edition. Replay only unambiguous failures."""
    run = db.get_run(run_id)
    if not run:
        raise ValueError("Unknown search run")
    if run['search_settings'].get('processing_stopped_at'):
        return 0
    with db.connect() as conn:
        errors = conn.execute("""SELECT id,subject_name,message FROM search_errors WHERE run_id=%s
            AND NOT rss_imported ORDER BY id FOR UPDATE""", (run_id,)).fetchall()
        imported = 0
        for subject in run['subjects']:
            failed = defaultdict(list)
            for row in errors:
                if row['subject_name'] != subject['name']:
                    continue
                message = row['message']
                if message.endswith(LEGACY_SUFFIX):
                    failed[message[:-len(LEGACY_SUFFIX)]].append(row['id'])
                    continue
                # The legacy company RSS adapter prefixes the error with the actual quoted query.
                timeout = re.fullmatch(r'(["(].+): (?:TimeoutError|URLError): .*timed out.*', message)
                if timeout:
                    failed[timeout[1]].append(row['id'])
            if not failed:
                continue
            plans = defaultdict(list)
            for q, edition, span in search.planned_feeds(subject, bounds(run), search_settings.for_run(run)["adverse_only"]):
                if q in failed:
                    plans[q].append((edition,span))
            key = subject_key(run_id, subject)
            for q, ids in failed.items():
                if not plans[q] and not q.startswith(('"','(')):
                    continue  # A named issuer or paid API can also return its own 503.
                if len(plans[q]) == len(ids):
                    for edition, span in plans[q]:
                        record(conn,run_id,key,q,edition,span)
                else:
                    for error_id in ids:
                        record(conn,run_id,key,q,None,bounds(run),unknown=error_id)
                imported += len(ids)
                conn.execute("UPDATE search_errors SET rss_imported=TRUE WHERE id=ANY(%s)", (ids,))
        if run['status'] not in ('queued', 'running') and coverage(conn, run_id):
            sync_run(conn, run_id, refresh_weeks=True)
    return imported


def coverage(conn, run_id):
    return conn.execute("""SELECT j.subject_key, count(*) AS total, min(j.created_at) AS first_seen,
        count(*) FILTER(WHERE j.status='recovered') AS recovered,
        count(*) FILTER(WHERE j.status='unknown_edition') AS uncertain,
        b.outcome AS brave FROM rss_recovery j LEFT JOIN rss_brave_attempts b USING(run_id,subject_key)
        WHERE j.run_id=%s GROUP BY j.subject_key,b.outcome ORDER BY j.subject_key""",(run_id,)).fetchall()


def sync_run(conn, run_id, *, refresh_weeks=False):
    from . import score_store
    score_store._lock(conn)
    previous = conn.execute("SELECT result_count,error_count,status FROM search_runs WHERE id=%s FOR UPDATE",
                            (run_id,)).fetchone()
    updated = conn.execute("""WITH totals AS (
        SELECT (SELECT count(*) FROM search_errors WHERE run_id=%s AND NOT rss_imported)
          + (SELECT count(DISTINCT subject_key) FROM rss_recovery WHERE run_id=%s AND status<>'recovered') AS errors,
          (SELECT count(*) FROM search_results WHERE run_id=%s) AS leads)
        UPDATE search_runs SET error_count=errors, result_count=leads,
          status=CASE WHEN status IN ('queued','running') THEN status WHEN errors>0 AND leads>0 THEN 'partial'
              WHEN errors>0 THEN 'failed' ELSE 'completed' END
        FROM totals WHERE id=%s RETURNING result_count,error_count,status""",
        (run_id, run_id, run_id, run_id)).fetchone()
    if refresh_weeks and updated != previous:
        score_store._weekly(conn)


def fetch_job(job, subject, run):
    request = job['request']
    query, edition = request['query'], request['edition']
    span = tuple(datetime.fromisoformat(v) for v in request['window'])
    url, language = request['url'], request['language']
    probe = search.feed_url('"' + provider_name(subject) + '"', edition, window=span)[0]
    raw = search.fetch_feed(url, probe=probe)
    if 'context' not in request:
        return search.parse_feed(raw, query, language, limit=100, window=span)
    ctx = request['context']
    branch, topic = ctx['branch'], ctx['topic']
    hits = exposure.search_topic(branch, topic, lambda _: raw, edition=edition, factor=ctx['factor'],
                                 window=span, markets=exposure.sector_markets(run['subjects']))
    return [(item, channel) for item, channel in hits if item['published_at'] and span[0] <= item['published_at'] <= span[1]]


def save_context_hits(conn, run_id, context, hits):
    row = conn.execute("SELECT hits FROM exposure_checks WHERE run_id=%s AND branch=%s AND topic=%s FOR UPDATE",
                       (run_id, context['branch'], context['topic'])).fetchone()
    if not row or not hits:
        return
    entries = {item['url']: (item, label) for item, label in row['hits']}
    for item, channel in hits:
        stored = dict(item, published_at=item['published_at'].isoformat())
        previous, labels = entries.get(item['url'], (stored, ''))
        entries[item['url']] = (previous, ', '.join(dict.fromkeys(filter(None, [labels, channel]))))
    conn.execute("UPDATE exposure_checks SET hits=%s WHERE run_id=%s AND branch=%s AND topic=%s",
                 (db.Jsonb(list(entries.values())), run_id, context['branch'], context['topic']))


def append_leads(conn, run, subject, rows):
    from . import score_store, worker
    if not rows or (run.get('search_settings') or {}).get('processing_stopped_at'):
        return 0
    key = subject_key(run['id'], subject)
    from .publisher_lookup import normalized, original_title
    # Serialize appenders for this run without holding the analyst scoring lock during translation.
    conn.execute("SELECT pg_advisory_xact_lock(8766,%s)", (run['id'],))
    saved = conn.execute("""SELECT id,url,title,publisher,published_at,exposure_paths,
        verification->>'source_url' AS source_url FROM search_results WHERE run_id=%s AND subject_key=%s""",
        (run['id'],key)).fetchall()
    urls, stories = {}, {}
    def story(row):
        return (normalized(original_title(row)), normalized(row['publisher']), row['published_at'])
    for previous in saved:
        for url in (previous['url'], previous['source_url']):
            if url:
                urls[canonical_url(url)] = previous
        if previous['publisher'] and previous['published_at']:
            stories[story(previous)] = previous
    start, end = bounds(run)
    unique = []
    path_updates = {}
    for row in rows:
        if not row['published_at'] or not start <= row['published_at'] <= end:
            continue
        url = canonical_url(row['url'])
        previous = urls.get(url) or (stories.get(story(row)) if row['publisher'] else None)
        if previous:
            paths = list(dict.fromkeys([*previous.get('exposure_paths', []), *row.get('exposure_paths', [])]))
            if paths != previous.get('exposure_paths', []):
                previous['exposure_paths'] = paths
                if previous.get('id'):
                    path_updates[previous['id']] = paths
            continue
        urls[url] = row
        if row['publisher']:
            stories[story(row)] = row
        unique.append(row)
    added = worker._save_leads(run['id'],subject,key,unique,search_settings.for_run(run),
                               lambda *args: None,set(),{},connection=conn,queue_checks=True)
    if added or path_updates:
        score_store._lock(conn)
        for result_id, paths in path_updates.items():
            conn.execute("UPDATE search_results SET exposure_paths=%s WHERE id=%s", (paths,result_id))
    if added:
        conn.execute("UPDATE search_runs SET candidates_in_window=coalesce(candidates_in_window,0)+%s, "
                     "event_analysis_status=CASE WHEN event_analysis_status='failed' THEN 'failed' ELSE 'pending' END, "
                     "retry_translations=TRUE WHERE id=%s",(added,run['id']))
    if added or path_updates:
        score_store._assess(conn,run['id'])
    return added


def brave_fallback(run, subject):
    key = subject_key(run['id'],subject)
    # Commit the reservation before network I/O so a restart cannot repeat a paid request.
    with db.connect() as conn:
        claim = conn.execute("INSERT INTO rss_brave_attempts(run_id,subject_key) VALUES (%s,%s) "
                             "ON CONFLICT DO NOTHING RETURNING run_id",(run['id'],key)).fetchone()
    if not claim:
        return
    try:
        queries = news_apis.provider_queries(subject, 'gnews')
        if not queries:
            raise ValueError("No usable company name for the fallback")
        language = search.LOCALES.get(search.normalized(subject.get('country_registration','')), ('en',))[0].split('-')[0]
        undated = set()
        found = monthly_backfill.brave_search(queries[0], bounds(run), subject, {}, set(), undated, language=language)
        stored = [dict(row, published_at=row['published_at'].isoformat()) for row in found]
        outcome = f"{len(stored)} dated matches"
        if undated:
            outcome += f", {len(undated)} matches lacked a confirmed publication date"
        with db.connect() as conn:
            conn.execute("UPDATE rss_brave_attempts SET staged=%s,outcome=%s WHERE run_id=%s AND subject_key=%s",
                         (db.Jsonb(stored), outcome, run['id'], key))
    except (ValueError, OSError, HTTPException, news_apis.ProviderError) as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        with db.connect() as conn:
            conn.execute("UPDATE rss_brave_attempts SET outcome=%s WHERE run_id=%s AND subject_key=%s",
                         (detail[:200],run['id'],key))


def collect_paid():
    if not recovery_providers.configured('Brave'):
        return False
    with db.connect() as conn:
        job = conn.execute("""SELECT j.run_id,j.subject_key FROM rss_recovery j JOIN search_runs s ON s.id=j.run_id
            WHERE s.status<>'queued' AND NOT s.paused AND NOT (s.search_settings ? 'processing_stopped_at')
            AND j.status<>'recovered' AND NOT EXISTS
            (SELECT FROM rss_brave_attempts b WHERE b.run_id=j.run_id AND b.subject_key=j.subject_key)
            ORDER BY j.id LIMIT 1""").fetchone()
    if not job or not db.remaining_news_api_requests({'brave': recovery_providers.daily_limit('Brave')})['brave']:
        return False
    run = db.get_run(job['run_id'])
    subject = next(s for s in run['subjects'] if subject_key(run['id'], s) == job['subject_key'])
    brave_fallback(run, subject)
    return True


def recover_one():
    from . import local_models
    # Take the model lease before opening a transaction that can translate recovered leads.
    with local_models.batch("RSS recovery"):
        return _recover_one()


def _recover_one():
    # Save paid discoveries while Google is paused, then merge once the foreground writer finishes.
    if collect_paid():
        return True
    with db.connect() as conn:
        paid = conn.execute("""SELECT b.* FROM rss_brave_attempts b JOIN search_runs s ON s.id=b.run_id
            WHERE s.status NOT IN ('running','queued') AND NOT s.paused AND NOT (s.search_settings ? 'processing_stopped_at') AND b.staged IS NOT NULL
            ORDER BY b.run_id,b.subject_key LIMIT 1 FOR UPDATE OF b SKIP LOCKED""").fetchone()
        if paid:
            run = db.get_run(paid['run_id'])
            subject = next(s for s in run['subjects'] if subject_key(run['id'], s) == paid['subject_key'])
            rows = [dict(r, published_at=datetime.fromisoformat(r['published_at'])) for r in paid['staged']]
            added = append_leads(conn, run, subject, rows)
            conn.execute("UPDATE rss_brave_attempts SET staged=NULL,outcome=%s WHERE run_id=%s AND subject_key=%s",
                         (f"{paid['outcome']}, {added} new leads",run['id'],paid['subject_key']))
            sync_run(conn, run['id'], refresh_weeks=True)
            return True
        if time.time() < rss_gate.google.until:
            return False
        job = conn.execute("""SELECT j.* FROM rss_recovery j JOIN search_runs s ON s.id=j.run_id
            WHERE s.status NOT IN ('running','queued') AND NOT s.paused AND NOT (s.search_settings ? 'processing_stopped_at') AND j.status='pending' AND j.next_attempt<=now()
            ORDER BY j.next_attempt,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED""").fetchone()
        if not job:
            return False
        run = db.get_run(job['run_id'])
        subject = next(s for s in run['subjects'] if subject_key(run['id'],s)==job['subject_key'])
        try:
            rows = fetch_job(job,subject,run)
        except (rss_gate.Deferred, OSError, ValueError, ElementTree.ParseError, HTTPException) as exc:
            delay = max(rss_gate.google.until-time.time(), min(3600, 30 * 2**min(job['attempts'],7)))
            conn.execute("""UPDATE rss_recovery SET attempts=attempts+1,detail=%s,next_attempt=now()+
                %s*interval '1 second' WHERE id=%s""",(type(exc).__name__,delay,job['id']))
            return True
        context = job['request'].get('context')
        if context:
            save_context_hits(conn, run['id'], context, rows)
            related = conn.execute("""SELECT id,subject_key FROM rss_recovery WHERE run_id=%s
                AND fingerprint=%s AND status='pending' ORDER BY id FOR UPDATE SKIP LOCKED""",
                (run['id'], job['fingerprint'])).fetchall()
            subjects = {subject_key(run['id'], s): s for s in run['subjects']}
            for target in related:
                company = subjects[target['subject_key']]
                hits = exposure.add_exposure_leads(company, [], [], {}, lambda b,t:
                    (rows if (b,t)==(context['branch'],context['topic']) else [], ''))
                append_leads(conn, run, company, hits)
            completed = [target['id'] for target in related]
        else:
            append_leads(conn, run, subject, rows)
            completed = [job['id']]
        conn.execute("UPDATE rss_recovery SET status='recovered',detail='',attempts=attempts+1 WHERE id=ANY(%s)",
                     (completed,))
        if run['scope'] == 'all' or run['search_settings'].get('watchlist_batch') or len(run['subjects']) > 1:
            req = job['request']
            key = rss_windows.topic_key(context['branch'], context['topic'], context.get('factor')) if context else job['subject_key']
            span = tuple(datetime.fromisoformat(v) for v in req['window'])
            rss_windows.save(conn, run['id'], [(key, req['query'], req['edition'], span)])
        sync_run(conn, run['id'], refresh_weeks=True)
        return True


def _loop():
    while True:
        try:
            if recover_one():
                time.sleep(1)
                continue
        except Exception:
            log.exception('RSS recovery paused after an error')
        time.sleep(15)


def start():
    rss_gate.google.restore()
    threading.Thread(target=_loop, daemon=True, name='ra-rss-recovery').start()


if __name__ == '__main__':
    import argparse
    from . import keywords, risk_factors
    parser = argparse.ArgumentParser(description="Import a legacy run’s recorded Google RSS 503s for same-run recovery")
    parser.add_argument('--import-run', type=int, required=True)
    args = parser.parse_args()
    keywords.reload_rules()
    risk_factors.reload()
    print(f"Imported {import_legacy(args.import_run)} failed searches.")
