"""Durable display translations, independent of source verification and scoring."""
import hashlib
import logging
import threading
from functools import lru_cache

from . import db, russian, translation_names
from .language import base_language, detect_language

_wake = threading.Event()
log = logging.getLogger(__name__)
summary_language = lru_cache(maxsize=2048)(detect_language)


def key(source, language):
    return hashlib.sha256((language + "\0" + source).encode()).hexdigest()


def enqueue(conn, source, language='en', subject=None, english='', *, run_id):
    if not source or not source.strip():
        return
    language = base_language(language)
    names = translation_names.names(subject)
    protection = {'name': names[0], 'aliases': '\n'.join(names[1:])} if names else {}
    conn.execute("""INSERT INTO result_translations(key,source,language,subject,english,russian,run_id)
        SELECT %s,%s,%s,%s,%s,%s,id FROM display_translation_runs WHERE id=%s
        ON CONFLICT (key) DO UPDATE SET run_id=excluded.run_id
        WHERE result_translations.run_id IS NULL""",
        (key(source, language), source, language, db.Jsonb(protection),
         source if language in ('en', 'zxx') else english, source if language in ('ru', 'zxx') else '', run_id))
    _wake.set()


def lead_text(row):
    if base_language(row.get('language')) in ('ru', 'zxx') or not row.get('title_en'):
        return row['title'], base_language(row.get('language')) or summary_language(row['title'])
    return row['title_en'], 'en'


def enqueue_lead(conn, row, subject, run_id):
    source, code = lead_text(row)
    enqueue(conn, source, code, subject, row.get('title_en', ''), run_id=run_id)
    if summary := row.get('summary') or row.get('discovery_summary'):
        enqueue(conn, summary, summary_language(summary, row.get('language', '')), subject, run_id=run_id)
    for path in row.get('exposure_paths', []):
        enqueue(conn, path, 'en', subject, run_id=run_id)


def display_prose(data):
    """Yield only display prose. Keep source quotations, IDs and canonical JSON intact."""
    if isinstance(data, list):
        for child in data:
            yield from display_prose(child)
    elif isinstance(data, dict):
        for field, value in data.items():
            if isinstance(value, str) and field in {
                'title', 'summary', 'reason', 'outcome', 'remaining', 'english', 'sentence',
                'explanation', 'connection', 'factor', 'coverage',
            }:
                yield value
            elif isinstance(value, (list, dict)):
                yield from display_prose(value)


def enqueue_prose(conn, data, subject, run_id):
    for value in sorted(set(display_prose(data))):
        enqueue(conn, value, 'en', subject, run_id=run_id)
    for quote in sorted(set(source_quotes(data))):
        enqueue(conn, quote, summary_language(quote), subject, run_id=run_id)


def source_quotes(data):
    if isinstance(data, list):
        for child in data:
            yield from source_quotes(child)
    elif isinstance(data, dict):
        for field, value in data.items():
            if field in ('quote', 'outcome_quote', 'remaining_quote', 'profit_decline_quote') and value:
                yield value
            elif isinstance(value, (list, dict)):
                yield from source_quotes(value)


def enqueue_document(conn, document, subject, run_id):
    for doc in [document, *document.get('linked_documents', [])]:
        enqueue_prose(conn, doc.get('translation', {}), subject, run_id)
        if doc.get('excerpt') or doc.get('review_only'):
            enqueue(conn, doc.get('body'), doc.get('language') or '', subject, run_id=run_id)


def restore_run_queue(conn):
    """Recover demand for eligible runs from their saved evidence when upgrading the shared cache."""
    for run in conn.execute("""SELECT s.* FROM search_runs s
            JOIN display_translation_runs eligible ON eligible.id=s.id ORDER BY s.id""").fetchall():
        for row in conn.execute("""SELECT r.*, d.document FROM search_results r LEFT JOIN article_documents d
                ON d.url=r.verification->>'requested_url' WHERE r.run_id=%s ORDER BY r.id""", (run['id'],)):
            subject = db.evidence_subject(row, run)
            enqueue_lead(conn, row, subject, run['id'])
            for field in ('event_draft', 'verification', 'risk_factor_review'):
                enqueue_prose(conn, row[field], subject, run['id'])
            for field in ('score_reason', 'relevance_reason'):
                enqueue(conn, row.get(field), subject=subject, run_id=run['id'])
            doc = row['verification'].get('review_document') or row['document'] or {}
            enqueue_document(conn, doc, subject, run['id'])


def process_one():
    from .article_translation import passages
    from .translation import translate_text
    # Lease before inference. A killed worker releases its job when the lease expires.
    with db.connect() as conn:
        row = conn.execute("""UPDATE result_translations SET retry_at=now()+interval '15 minutes'
            WHERE key=(SELECT key FROM result_translations WHERE run_id IN (SELECT id FROM display_translation_runs)
                       AND (english='' OR russian='') AND russian_rejections<3 AND retry_at<=now()
                       ORDER BY retry_at,key LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *""").fetchone()
    if not row:
        return False
    english, localized = row['english'], row['russian']
    if not english:
        chunks = [translate_text(part, row['language'], row['subject'])[0]
                  for part in passages(row['source'], limit=480, subject=row['subject'])]
        if all(chunks):
            english = ' '.join(chunks)
    rejections, error = row['russian_rejections'], row['russian_error']
    if not localized and english:
        try:
            localized = russian.checked_translation(english, row['subject'])
            rejections, error = 0, ''
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            rejections += 1
            error = str(exc)[:250]
            log.warning("Display translation rejected run=%s key=%s attempt=%s: %s",
                        row['run_id'], row['key'], rejections, error)
        except (OSError, RuntimeError) as exc:
            log.warning("Display translator unavailable run=%s key=%s: %s", row['run_id'], row['key'], type(exc).__name__)
    previous = (row['english'], row['russian'], row['russian_rejections'], row['russian_error'])
    if (english, localized, rejections, error) != previous:
        with db.connect() as conn:
            conn.execute("""UPDATE result_translations SET english=%s,russian=%s,russian_rejections=%s,russian_error=%s
                WHERE key=%s AND retry_at=%s AND english=%s AND russian=%s""",
                (english, localized, rejections, error, row['key'], row['retry_at'], row['english'], row['russian']))
    return True


def _loop():
    while True:
        try:
            if process_one():
                continue
        except Exception:
            log.exception('Display translation failed; queued for retry')
        _wake.wait(10)
        _wake.clear()


def start():
    threading.Thread(target=_loop, daemon=True, name='ra-display-translations').start()
