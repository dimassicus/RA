"""Durable display translations, independent of source verification and scoring."""
import hashlib
import json
import logging
import threading
import time
from functools import lru_cache

from . import db, russian, translation_names
from .aliases import normalized
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
            if field in ('quote', 'outcome_quote', 'remaining_quote', 'profit_decline_quote', 'material_event_quote') and value:
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


def passage_key(source, names):
    return key(json.dumps([source, names], ensure_ascii=False), "ru-display-v1")


def saved_passage(source, names):
    digest = passage_key(source, names)
    with db.connect() as conn:
        row = conn.execute("SELECT russian FROM russian_display_passages WHERE key=%s", (digest,)).fetchone()
    if row:
        return row["russian"]
    result = russian._passage(source, names)
    with db.connect() as conn:
        conn.execute("INSERT INTO russian_display_passages VALUES (%s,%s) ON CONFLICT DO NOTHING", (digest, result))
    return result


def warm_short_passages(rows):
    # Only send single short passages to Gemma. Validate each keyed response independently.
    candidates = {}
    for row in rows:
        source = row["english"]
        if row["russian"] or not source or len(source) > 160 or "\n" in source or source.strip() != source:
            continue
        masked, names = translation_names.mask(source, row['subject'])
        if not masked.isascii() or "..." in masked or "ZXQ" in masked or not any(c.isalpha() for c in masked):
            continue
        digest = passage_key(masked, tuple(names.items()))
        candidates[digest] = masked
    if len(candidates) < 2:
        return
    with db.connect() as conn:
        for hit in conn.execute("SELECT key FROM russian_display_passages WHERE key=ANY(%s)", (list(candidates),)):
            candidates.pop(hit['key'])
    if len(candidates) < 2:
        return
    items = {f"text_{i}": value for i, value in enumerate(candidates.values())}
    try:
        answer = russian.keywords.model_json(
            "Translate each independent English passage into Russian under its own key. "
            "Preserve all names, numbers, identifiers, currencies, units and negations. Never move text between keys.",
            items, russian.keywords.object_schema({label: {"type": "string"} for label in items}),
            model=russian.local_models.GEMMA)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError):
        return
    with db.connect() as conn:
        for (digest, source), label in zip(candidates.items(), items):
            try:
                localized = russian._checked(source, answer[label], {})
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            conn.execute("INSERT INTO russian_display_passages VALUES (%s,%s) ON CONFLICT DO NOTHING",
                         (digest, localized))


def process_one(*, batch_size=1):
    # Lease before inference. A killed worker releases its job when the lease expires.
    with db.connect() as conn:
        rows = conn.execute("""UPDATE result_translations SET retry_at=now()+interval '15 minutes'
            WHERE key IN (SELECT key FROM result_translations WHERE run_id IN (
                       SELECT s.id FROM search_runs s JOIN display_translation_runs eligible USING (id) WHERE NOT s.paused)
                       AND (english='' OR russian='') AND russian_rejections=0 AND retry_at<=now()
                       ORDER BY retry_at,key LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING *""", (batch_size,)).fetchall()
    if not rows:
        return False
    started = time.monotonic()
    if batch_size > 1:
        warm_short_passages(rows)
    for row in rows:
        if batch_size > 1 and db.run_paused(row["run_id"]):
            with db.connect() as conn:
                conn.execute("UPDATE result_translations SET retry_at=now() WHERE key=%s AND retry_at=%s",
                             (row["key"], row["retry_at"]))
            continue
        process_row(row)
    log.info("display_batch runs=%s rows=%s seconds=%.3f", sorted({r["run_id"] for r in rows}), len(rows),
             time.monotonic() - started)
    return True


def process_row(row):
    from .article_translation import passages
    from .translation import translate_text
    english, localized = row['english'], row['russian']
    rejections, error = row['russian_rejections'], row['russian_error']
    try:
        source_name = normalized(row['source']) if not english else ''
        if source_name and any(normalized(name) == source_name for name in translation_names.names(row['subject'])):
            # A whole issuer name stays in its source spelling in both display languages.
            english = row['source']
            localized = localized or english
        if not english:
            chunks = [translate_text(part, row['language'] or 'auto', row['subject'], raise_rejection=True)[0]
                      for part in passages(row['source'], limit=480, subject=row['subject'])]
            if all(chunks):
                english = ' '.join(chunks)
        if not localized and english:
            localized = russian.checked_translation(english, row['subject'], translate_passage=saved_passage)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # Gemma repair and the independent fallback already ran. The same inputs repeat the rejection.
        rejections, error = rejections + 1, str(exc)[:250]
        log.warning("Display translation rejected run=%s key=%s: %s", row['run_id'], row['key'], error)
    except (OSError, RuntimeError) as exc:
        log.warning("Display translator unavailable run=%s key=%s: %s", row['run_id'], row['key'], type(exc).__name__)
    if english and localized:
        rejections, error = 0, ''
    previous = (row['english'], row['russian'], row['russian_rejections'], row['russian_error'])
    if (english, localized, rejections, error) != previous:
        with db.connect() as conn:
            conn.execute("""UPDATE result_translations SET english=%s,russian=%s,russian_rejections=%s,russian_error=%s
                WHERE key=%s AND retry_at=%s AND english=%s AND russian=%s""",
                (english, localized, rejections, error, row['key'], row['retry_at'], row['english'], row['russian']))


def _loop():
    while True:
        try:
            if process_one(batch_size=8):
                continue
        except Exception:
            log.exception('Display translation failed; queued for retry')
        _wake.wait(10)
        _wake.clear()


def start():
    threading.Thread(target=_loop, daemon=True, name='ra-display-translations').start()
