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


def enqueue(conn, source, language='en', subject=None, english=''):
    if not source or not source.strip():
        return
    language = base_language(language)
    names = translation_names.names(subject)
    protection = {'name': names[0], 'aliases': '\n'.join(names[1:])} if names else {}
    conn.execute("""INSERT INTO result_translations(key,source,language,subject,english,russian)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING""",
        (key(source, language), source, language, db.Jsonb(protection),
         source if language in ('en', 'zxx') else english, source if language in ('ru', 'zxx') else ''))
    _wake.set()


def lead_text(row):
    if base_language(row.get('language')) in ('ru', 'zxx') or not row.get('title_en'):
        return row['title'], base_language(row.get('language')) or summary_language(row['title'])
    return row['title_en'], 'en'


def enqueue_lead(conn, row, subject):
    source, code = lead_text(row)
    enqueue(conn, source, code, subject, row.get('title_en', ''))
    if summary := row.get('summary'):
        enqueue(conn, summary, summary_language(summary, row.get('language', '')), subject)
    for path in row.get('exposure_paths', []):
        enqueue(conn, path, 'en', subject)


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


def enqueue_prose(conn, data, subject):
    for value in sorted(set(display_prose(data))):
        enqueue(conn, value, 'en', subject)
    for quote in sorted(set(source_quotes(data))):
        enqueue(conn, quote, summary_language(quote), subject)


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


def process_one():
    from .article_translation import passages
    from .translation import translate_text
    # Lease before inference. A killed worker releases its job when the lease expires.
    with db.connect() as conn:
        row = conn.execute("""UPDATE result_translations SET retry_at=now()+interval '15 minutes'
            WHERE key=(SELECT key FROM result_translations WHERE (english='' OR russian='') AND retry_at<=now()
                       ORDER BY retry_at,key LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *""").fetchone()
    if not row:
        return False
    english, localized = row['english'], row['russian']
    if not english:
        chunks = [translate_text(part, row['language'], row['subject'])[0]
                  for part in passages(row['source'], limit=480, subject=row['subject'])]
        if all(chunks):
            english = ' '.join(chunks)
    if not localized and english:
        localized = russian.translate(english, row['subject'])
    if english != row['english'] or localized != row['russian']:
        with db.connect() as conn:
            conn.execute('UPDATE result_translations SET english=%s,russian=%s WHERE key=%s',
                         (english, localized, row['key']))
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
