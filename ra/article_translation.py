"""Complete source-text translation; only independently reviewed text may reach attribution rules."""
import hashlib
import re

from scripts.translation_review.simple import review_content

from . import headline_review, translation

VERSION = "article-translation-v1"


def passages(body):
    """Keep every character, with bounded passages for Argos and Qwen's context budget."""
    parts = []
    while body:
        end = len(body)
        if end > 900:
            window = body[:900]
            breaks = list(re.finditer(r'(?<=[.!?。！？])\s+|\n+', window))
            if not breaks:
                breaks = list(re.finditer(r'\s+', window))
            if not breaks:
                raise ValueError('Source contains an overlong unbroken passage; analyst review required')
            end = breaks[-1].end()
        parts.append(body[:end])
        body = body[end:]
    return parts


def fingerprint(body):
    return hashlib.sha256(body.encode()).hexdigest()


def faithful(part):
    review = part.get('review', {})
    return (bool(part.get('english', '').strip()) and review.get('status') == 'faithful'
            and review.get('model') == headline_review.QWEN and review.get('reasoning') == 'off'
            and not review.get('issues'))


def group_replacements(parts, replacements, language):
    """Review complete contiguous replacements together, retaining that scope on retries."""
    pending = {id(part) for part in replacements}
    grouped, review = [], []
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if id(part) not in pending:
            grouped.append(part)
            continue
        members = [part]
        source, english = part['source'], part['english'].strip()
        while index < len(parts) and id(parts[index]) in pending:
            next_part = parts[index]
            candidate_source = source + next_part['source']
            candidate_english = english + ' ' + next_part['english'].strip()
            if next_part['provider'] != part['provider'] or len(candidate_source) > 3000:
                break
            try:
                review_content(candidate_source, language, 'en', {'A': candidate_english})
            except ValueError:
                break
            members.append(next_part)
            source, english = candidate_source, candidate_english
            index += 1
        if len(members) > 1:
            part = {'source': source, 'english': english, 'provider': part['provider'], 'review': {},
                    'rejected_translations': [entry for member in members for entry in [
                        *member.get('rejected_translations', []),
                        {'source': member['source'], **member['rejected_translation']}]]}
        grouped.append(part)
        review.append(part)
    return grouped, review


def prepare(document):
    """Reuse reviewed passages on retry; translate first, then review to limit model switching."""
    language = document.get('language', '').lower().split('-')[0]
    if document.get('status') != 'available' or document.get('review_only') or language in ('en', 'pt'):
        return document
    body = document.get('body', '')
    old = document.get('translation', {})
    result = {'version': VERSION, 'source_hash': fingerprint(body), 'language': language,
              'status': 'needs_review', 'parts': []}
    document = {**document, 'translation': result}
    if not language or not body.strip():
        result['reason'] = 'Full-article language or text is missing; analyst review required'
        return document
    old_parts = old.get('parts', [])
    reusable = (old.get('version') == VERSION and old.get('source_hash') == result['source_hash']
                and old.get('language') == language and isinstance(old_parts, list)
                and all(isinstance(p, dict) and isinstance(p.get('source'), str) and p['source'] for p in old_parts)
                and ''.join(p['source'] for p in old_parts) == body)
    try:
        parts = result['parts'] = ([dict(p) for p in old_parts] if reusable
                                    else [{'source': source} for source in passages(body)])
    except ValueError as exc:
        result['reason'] = str(exc)
        return document
    if not all(faithful(p) for p in parts):
        readiness = headline_review.readiness()
        if not readiness['ready']:
            result.update(status='unavailable', reason=readiness['message'])
            return document
    for part in parts:
        if faithful(part):
            continue
        if not part.get('english') or part.get('review', {}).get('status') == 'material_error':
            english, provider = translation.argos_translate(part['source'], language, complete=True)
            if not english:
                english, provider = headline_review.fallback(part['source'], language, '', provider, article=True)
            part.update(english=english, provider=provider, review={})
    for part in parts:
        if faithful(part) or not part.get('english'):
            continue
        part['review'] = headline_review.audit(part['source'], language, part['english'],
                                               retry_reason=part.get('review', {}).get('reason', ''))
        if part['review'].get('status') == 'unavailable':
            break
    # A rejected Argos output must not be retried forever with the same translation engine.
    replacements = []
    for part in parts:
        if (part.get('provider', '').startswith('Argos')
                and part.get('review', {}).get('status') in ('material_error', 'uncertain')):
            english, provider = headline_review.fallback(part['source'], language, '',
                                                         'Argos translation rejected by Qwen', article=True)
            if english:
                part['rejected_translation'] = {k: part[k] for k in ('english', 'provider', 'review')}
                part.update(english=english, provider=provider, review={})
                replacements.append(part)
    parts, replacements = group_replacements(parts, replacements, language)
    result['parts'] = parts
    for part in replacements:
        part['review'] = headline_review.audit(part['source'], language, part['english'])
    checked = sum(faithful(p) for p in parts)
    result.update(status='faithful' if checked == len(parts) else 'needs_review',
                  reason=f'Full-article translation: {checked}/{len(parts)} passages approved by Qwen (reasoning off)')
    return document


def checked_text(document):
    """Reject partial, stale or unreviewed translations even if their top-level status says faithful."""
    data = document.get('translation', {})
    parts = data.get('parts', [])
    if (data.get('status') != 'faithful' or data.get('version') != VERSION
            or data.get('source_hash') != fingerprint(document.get('body', ''))
            or data.get('language') != document.get('language', '').lower().split('-')[0]
            or not parts or ''.join(p.get('source', '') for p in parts) != document.get('body')
            or not all(faithful(p) for p in parts)):
        raise ValueError(data.get('reason') or 'Complete, Qwen-approved full-article translation required')
    return ' '.join(p['english'].strip() for p in parts)


def summary(document):
    data = document.get('translation', {})
    return {**{k: v for k, v in data.items() if k != 'parts'},
            'total': len(data.get('parts', [])), 'reviewed': sum(faithful(p) for p in data.get('parts', [])),
            'providers': sorted({p.get('provider', '') for p in data.get('parts', []) if p.get('provider')})}


def original_passages(document, sentence):
    """Return source passages corresponding to an English evidence sentence, including boundary overlaps."""
    parts = document['translation']['parts']
    body = checked_text(document)
    start = body.find(sentence)
    if start < 0:
        raise ValueError('Translated evidence was not found in the reviewed body')
    end, cursor, sources = start + len(sentence), 0, []
    for part in parts:
        next_cursor = cursor + len(part['english'].strip())
        if cursor < end and next_cursor > start:
            sources.append(part['source'])
        cursor = next_cursor + 1
    return sources
