"""Resolve a reported decision against quoted document text before drafting its status."""
import re
from datetime import date
from babel.dates import get_month_names
from babel import UnknownLocaleError
from .aliases import normalized

from . import keywords

OPERATIVE = re.compile(r'partially grant|remain.{0,30}(?:seiz|frozen|restrict)|ordered.{0,30}return|'
                       r'granted|dismissed|upheld|overturned|acquitted|sentenced|the court (?:held|ruled)|'
                       r'dikabulkan|mengabulkan|dikembalikan|tetap disita|rejeitad|deferid|parcialmente', re.I)
PENDING = re.compile(r'\b(?:await(?:s|ing)? (?:a |the )?(?:court.{0,10})?(?:decision|ruling|judgment)|'
                     r'(?:decision|ruling|petition|objection|outcome|case) (?:is |was |remains? )?(?:still )?pending|'
                     r'pending (?:the |a )?(?:court|decision|ruling)|(?:yet to|has yet to) (?:rule|decide))\b', re.I)
_ANSWER = keywords.object_schema({
    'outcome': {'type':'string'}, 'remaining': {'type':'string'}, 'residual_adverse': {'type':'boolean'},
    'outcome_line': {'type':'integer'}, 'remaining_line': {'type':'integer'},
    'decision_day': {'type':'string'}, 'date_line': {'type':'integer'}})


def mentions_day(text, value, language='en'):
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    if value in text:
        return True
    for numeric in re.finditer(r'(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)', text):
        a,b,y = map(int,numeric.groups())
        if (y,b,a)==(day.year,day.month,day.day) and (a>12 or a==b or language not in ('','en')):
            return True
    for locale in ('en', language):
        try:
            months = [get_month_names(width, locale=locale or 'en')[day.month] for width in ('wide','abbreviated')]
        except (ValueError, LookupError, UnknownLocaleError):
            continue
        words = normalized(text)
        for month in months:
            m = re.escape(normalized(month))
            if re.search(rf'\b(?:{m} 0?{day.day}|0?{day.day} {m}) {day.year}\b', words):
                return True
    return False


def passage(text, budget):
    """Keep operative language in the middle of a filing when it exceeds the prompt budget."""
    if len(text) <= budget:
        return text
    match = OPERATIVE.search(text)
    if not match:
        return text[:budget//2-3]+' […] '+text[-budget//2+3:]
    start = max(0, min(len(text)-budget, match.start()-budget//4))
    return text[start:start+budget]


def disposition(members, company, model):
    notices = [r for r in members if r['decision_notice']]
    if not notices:
        return None
    latest = max(r['published'] for r in notices)
    notice = next(r for r in reversed(notices) if r['published']==latest)
    # Do not use an older motion or a later cover page as proof of what the court decided.
    sources = []
    for row in sorted(members,key=lambda r:r['published'],reverse=True):
        if row['published'] < latest:
            continue
        for s in [*row['sources'], *row.get('corroboration', [])]:
            if s['kind']=='reader recovery, analyst review required' or any(t['url']==s['url'] for t in sources):
                continue
            sources.append(dict(url=s['url'],text=passage(s['text'],3000),language=s.get('language','en')))
    result = {'as_of':latest,'notice_url':notice['url'],'status':'unconfirmed'}
    if not sources:
        return result
    lines = {}
    for doc in sources[:5]:
        for sentence in re.split(r'\n+|(?<=[.!?。！？])\s+', doc['text']):
            sentence = sentence.strip()
            if 12 <= len(sentence) <= 1800:
                lines[len(lines)+1] = {'url':doc['url'],'text':sentence,'language':doc['language']}
    if not lines:
        return result
    answer = model("Identify the operative ruling for this issuer and proceeding. A report published after the notice "
                   "can describe the same ruling. Treat the publication date separately from the hearing date. "
                   "Return its outcome in English, including the quantities and units, and the ID of a supplied line "
                   "that supports it. Also give the date on which the court ruled as YYYY-MM-DD with the ID of its line, or empty and 0. "
                   "For partial relief also state what remains restricted and its supporting line ID. "
                   "Use two lines from the same source. Set residual_adverse true only for a continuing adverse "
                   "restriction on the named company. Use 0 and empty text when a fact cannot be established. "
                   "Do not confuse earlier requests or third-party cases with this ruling.",
                   {'issuer':company,'notice':notice['title'],'notice_publication':latest,'source_lines':lines}, _ANSWER)
    outcome = lines.get(answer.get('outcome_line'))
    remaining = lines.get(answer.get('remaining_line'))
    if not outcome or not answer.get('outcome'):
        return result
    partial = re.search(r'partial|in part|sebagian|parcial', answer['outcome']+' '+outcome['text'], re.I)
    if remaining or partial or answer.get('remaining') or answer.get('residual_adverse'):
        if not remaining or not answer.get('remaining') or remaining['url'] != outcome['url']:
            return result
    facts = dict(outcome=answer['outcome'], outcome_quote=outcome['text'], remaining=answer.get('remaining',''),
                 remaining_quote=remaining['text'] if remaining else '', source_url=outcome['url'],
                 residual_adverse=answer.get('residual_adverse') is True)
    dated = lines.get(answer.get('date_line'))
    if (dated and dated['url'] == outcome['url'] and
            mentions_day(dated['text'],answer.get('decision_day'),dated['language']) and answer['decision_day']<=latest):
        facts.update(decision_day=answer['decision_day'],date_quote=dated['text'])
    check = model("Do the quoted passages support both English statements for this issuer and this ruling? "
                  "Check dates, quantities, what was relieved versus what remains, and whether a continuing "
                  "adverse restriction is actually reported. Reject unsupported or contradictory paraphrases.",
                  {'issuer':company,'notice':notice['title'],'facts':facts},
                  keywords.object_schema({'supported':{'type':'boolean'}}))
    return dict(result,status='documented',**facts) if check.get('supported') is True else result
