"""Conservative company/event attribution against extracted source sentences.

'Supported' means the source explicitly makes the statement, not independent
fact checking. No pronoun resolution, group-to-subsidiary inference or LLM calls.
"""
import re

from .aliases import company_names, headline_identity, normalized
from .scoring import COMPILED, UNCERTAIN, NEGATION
from . import article_translation

VERSION = 'source-attribution-v3'
# Deliberately closed vocabulary between the company name and event phrase.
# 'Acme's supplier filed for bankruptcy' must never become Acme's bankruptcy.
BRIDGE = re.compile(r'(?:(?:s|has|have|had|is|was|were|has been|will|its|the|a|an|that it|that the company|'
                    r'files|filed|filing|for|reports|reported|announces|announced|'
                    r'confirms|confirmed|declares|declared|suffers|suffered|enters|entered|seeks|sought|'
                    r'company|ceo|chairman|president|chief executive|net|annual|quarterly|'
                    r'foi|esta|em|entrou|pede|pediu|anuncia|anunciou|informa|'
                    r'informou|registra|registrou|teve|apresenta|apresentou|'
                    r'sua|seu|o|a|um|uma|de|em|processo|pedido|decreta|decretou)\s*)*')
HISTORICAL = re.compile(r'\b(?:previously|historically|last year|years? ago|in 20\d\d|em 20\d\d|ano passado|anteriormente)\b')
CONTEXT = re.compile(r'\b(?:denies?|denied|rumou?r\w*|alleged|hypothetical|cancelled|canceled|withdrawn|dismissed|resolved|lifted|no longer|forecast\w*|expects?|expected|plans?|planned|risk of|could|might|may|would|will|nega|negou|preve|pretende|pode)\b')
OTHER_ENTITY = re.compile(r'\b(?:subsidiar\w*|supplier\w*|rival\w*|competitor\w*|parent|affiliate\w*|fornecedor\w*)\b')


def verify(document, subject, article):
    result = {'version': VERSION, 'status': 'needs_review', 'reason': '', 'evidence': [],
              'document_hash': document.get('sha256', ''), 'source_url': document.get('final_url', article['url']),
              'recovery': document.get('recovery',''), 'method': document.get('method', ''), 'language': document.get('language', '')}
    if document.get('status') != 'available':
        return {**result, 'status': 'unavailable', 'reason': document.get('reason', 'Source unavailable')}
    if document.get('review_only'):
        return {**result, 'reason': 'Recovered publisher text requires analyst confirmation of the event and company impact'}
    body = document['body']
    if article.get('discovery_service') == 'FCA Warning RSS':
        return {**result, 'reason': 'Possible clone warning; analyst must identify the affected legal entity'}
    # An English headline is not a translation of the full article.
    translated = document.get('language', '').lower().split('-')[0] not in ('en', 'pt')
    if translated:
        result['translation'] = article_translation.summary(document)
        try:
            body = article_translation.checked_text(document)
        except ValueError as exc:
            return {**result, 'reason': str(exc)}
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', body) if s.strip()]
    names = sorted(company_names(subject), key=len, reverse=True)
    identity_seen, ambiguous = False, False
    for index, sentence in enumerate(sentences):
        folded = normalized(sentence)
        if not headline_identity(folded, subject):
            continue
        identity_seen = True
        # Adjacent sentences can deny or date a claim without repeating the name.
        context = normalized(" ".join(sentences[max(0, index-1):index+2]))
        if (UNCERTAIN.search(context) or NEGATION.search(context) or CONTEXT.search(context)
                or HISTORICAL.search(context) or OTHER_ENTITY.search(folded)):
            if any(pattern.search(folded) for _, _, pattern in COMPILED):
                ambiguous = True
            continue
        candidates = [(m.start(), m.end(), score, label, m.group())
                      for score, label, pattern in COMPILED for m in pattern.finditer(folded)]
        accepted = []
        for start, end, score, label, phrase in sorted(candidates, key=lambda c: -(c[1]-c[0])):
            if any(start < b and end > a for a, b in accepted):
                continue
            accepted.append((start, end))
            # An executive departure alone does not establish a credit consequence.
            if (re.fullmatch(r'resign\w*|renuncia\w*|istifa\w*|tu chuc', phrase)
                    or (phrase.startswith('demiss') and not re.search(r'\b(?:funcionarios|empregados|trabalhadores)\b', folded))):
                continue
            if (re.fullmatch(r'default(?:s|ed)?', phrase) and not re.search(r'\b(?:debt|payment|bond|creditor|loan|interest)\w*\b', folded)):
                ambiguous = True
                continue
            if phrase in ('loss', 'losses') and not re.search(r'\b(?:net|financial|quarter\w*|million|billion|earnings|annual|revenue|profit)\b', folded):
                ambiguous = True
                continue
            linked = False
            for name in names:
                # Company must be the grammatical subject at the start of this sentence.
                match = re.match(r'^(?:the |a empresa |empresa |a )?' + re.escape(name) + r'(?!\w)', folded)
                if not match or match.end() > start:
                    continue
                bridge = folded[match.end():start].strip()
                if len(bridge) <= 90 and BRIDGE.fullmatch(bridge):
                    linked = True
                    break
            if not linked:
                ambiguous = True
                continue
            evidence = {'score': score, 'rule': label, 'phrase': phrase,
                        'field': 'reviewed article translation' if translated else 'source article', 'sentence': sentence[:1200]}
            if translated:
                evidence['original_passages'] = article_translation.original_passages(document, sentence)
            result['evidence'].append(evidence)
    if not identity_seen:
        return {**result, 'status': 'unmatched', 'reason': 'Extracted source text does not establish this company identity'}
    if ambiguous:
        return {**result, 'reason': 'Event attribution, denial, timing or conditional context needs analyst review'}
    if not result['evidence']:
        return {**result, 'reason': 'Company mentioned, but no explicit company-subject credit event was established'}
    dominant = min(result['evidence'], key=lambda e: (-abs(e['score']), e['score']))
    result.update(status='supported', score=dominant['score'],
                  reason='Source explicitly attributes an event to the named company: ' + dominant['rule'])
    if translated:
        result['reason'] += '; full-article translation independently approved by Qwen (reasoning off)'
    # Used for narrow near-copy grouping; never group on event category alone.
    result['signature'] = normalized(' '.join(e['sentence'] for e in result['evidence']))
    return result
