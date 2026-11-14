"""Language preference, interface catalog, and display-only lookups."""
import json
import re
from pathlib import Path
from contextlib import ExitStack

import pycountry
from babel import Locale
from flask import g, has_request_context, request

from . import bilingual, db, keywords
from .status_ru import status_ru

ROOT = Path(__file__).parent
MESSAGES = {key: value for name in ('workspace', 'review')
            for key, value in json.loads((ROOT / f'{name}_ru.json').read_text()).items()}
EN, RU = Locale('en'), Locale('ru')
COUNTRIES = {name.casefold(): RU.territories.get(code, name) for code, name in EN.territories.items()}
REGISTRATION_COUNTRIES = {RU.territories[c.alpha_2].casefold(): c.name for c in pycountry.countries
                          if c.alpha_2 in RU.territories}
COUNTRIES.update({c.name.casefold(): RU.territories[c.alpha_2] for c in pycountry.countries if c.alpha_2 in RU.territories})
LANGUAGES = {name: RU.languages.get(code, name) for code, name in EN.languages.items()}


def language():
    return 'ru' if has_request_context() and request.cookies.get('ra_language') == 'ru' else 'en'


def _reader():
    if 'translation_stack' not in g:
        g.translation_stack = ExitStack()
        g.translation_db = g.translation_stack.enter_context(db.connect())
        g.translation_cache = {}
        g.translation_missing = {}
        g.translation_pending = set()
    return g.translation_db


def close_reader(error):
    stack = g.pop('translation_stack', None)
    if stack:
        with stack:
            if error is None:
                for digest in sorted(g.translation_missing):
                    bilingual.enqueue(g.translation_db, *g.translation_missing[digest], run_id=g.translation_run_id)


def prefetch(rows):
    """Read a run's display text in one query, before rendering its two lead sections."""
    if language() != 'ru' or not rows:
        return
    keys = set()
    for row in rows:
        source, code = bilingual.lead_text(row)
        keys.add(bilingual.key(source, code))
        if text := row.get('discovery_summary'):
            keys.add(bilingual.key(text, bilingual.summary_language(text, row.get('language', ''))))
        values = [row.get('score_reason'), row.get('relevance_reason'), *row.get('exposure_paths', [])]
        for field in ('event_draft', 'verification', 'risk_factor_review'):
            values.extend(bilingual.display_prose(row.get(field)))
        keys.update(bilingual.key(v, 'en') for v in values if v and v not in MESSAGES)
    conn = _reader()
    g.translation_cache.update(dict.fromkeys(keys))
    for row in conn.execute('SELECT key,english,russian,run_id,russian_rejections FROM result_translations WHERE key=ANY(%s)', (list(keys),)):
        g.translation_cache[row['key']] = row


def _lookup(text, source, target):
    if source in (target, 'zxx') or not text:
        return text
    digest = bilingual.key(text, source)
    def read(conn):
        return conn.execute('SELECT english,russian,run_id,russian_rejections FROM result_translations WHERE key=%s', (digest,)).fetchone()
    if has_request_context():
        conn = _reader()
        cache = g.translation_cache
        if digest not in cache:
            cache[digest] = read(conn)
        row = cache[digest]
    else:
        with db.connect() as conn:
            row = read(conn)
    result = (row or {}).get('russian' if target == 'ru' else 'english')
    if result:
        return result
    if row and row["russian_rejections"] > 0:
        if target != "ru":
            return "Translation needs review · original: " + text
        return "Перевод не прошёл проверку · оригинал: " + text
    if has_request_context():
        if 'translation_run_id' not in g:
            run_id = g.get('article_run_id') or (request.view_args or {}).get('run_id')
            g.translation_run_id = run_id if run_id and conn.execute(
                'SELECT id FROM display_translation_runs WHERE id=%s', (run_id,)).fetchone() else None
        if g.translation_run_id:
            if not row or row['run_id'] is None:
                g.translation_missing[digest] = (text, source)
            g.translation_pending.add(digest)
            return ('Ожидает перевода · оригинал: ' if target == 'ru' else 'Translation pending · original: ') + text
    return text


def ru(text):
    if not isinstance(text, str) or not text or not any(c.isalpha() for c in text):
        return text
    if value := (MESSAGES.get(text) or COUNTRIES.get(text.casefold()) or LANGUAGES.get(text)
                 or keywords.starter_translations().get(text, {}).get("ru") or status_ru(text, MESSAGES)):
        return value
    if re.fullmatch(r'[^A-Za-z]*[А-Яа-яЁё][^A-Za-z]*', text):
        return text
    if m := re.fullmatch(r'(\d+) negative and (\d+) neutral distinct sources\.( Material adverse evidence takes precedence\.)?', text):
        return (f'Независимые источники: негативные — {m[1]}, нейтральные — {m[2]}.'
                + (' Существенные негативные факты имеют приоритет.' if m[3] else ''))
    return _lookup(text, 'en', 'ru')


def message(text):
    return ru(text) if language() == 'ru' else text


def headline(row, target=None):
    target = target or language()
    if target == 'en' and row.get('title_en'):
        return row['title_en']
    source, code = bilingual.lead_text(row)
    return _lookup(source, code or bilingual.summary_language(source), target)


def passage(text, code=None):
    return _lookup(text, code or bilingual.summary_language(text), language()) if text else ''


def summary(row):
    text = row.get('discovery_summary') or ''
    target = language()
    return _lookup(text, bilingual.summary_language(text, row.get('language', '')), target)


def localdate(value, fmt):
    if language() == 'ru':
        from babel.dates import format_datetime
        return format_datetime(value, {'%d %b': 'dd MMM', '%d %b %Y': 'dd MMM yyyy',
                                      '%d %b %Y %H:%M %Z': 'dd MMM yyyy HH:mm z'}[fmt], locale='ru')
    return value.strftime(fmt)
