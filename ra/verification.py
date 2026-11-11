"""Conservative company/event attribution against extracted source sentences.

'Supported' means the source explicitly makes the statement, not independent
fact checking. No pronoun resolution, group-to-subsidiary inference or LLM calls.
"""
import re

from .aliases import LEGAL_SUFFIX, company_names, headline_identity, related_identity, normalized
from .adverse_terms import PERSON_TITLES
from .relevance import case_context
from .article_content import validate_pdf_text
from .scoring import COMPILED, UNCERTAIN, NEGATION, sentiment_assessment
from . import article_translation

VERSION = 'source-attribution-v5'
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


def declared_hibernation(sentence, names):
    """Recognize a formally defined issuer's own enumerated board decision."""
    declaration = re.match(r'^A\s+(.{3,180}?)\s*\([“"\']Companhia[”"\']\)', sentence, re.I | re.S)
    if not declaration or LEGAL_SUFFIX.sub('', normalized(declaration[1])) not in names:
        return None
    decision = re.search(r'\ba Diretoria da Companhia decidiu\s*\(I\)', sentence, re.I)
    if not decision or decision.start() < declaration.end():
        return None
    if re.search(r'[“”"‘’\']', sentence[declaration.end():decision.start()]):
        return None  # A quoted third-party statement does not inherit the issuer's definition.
    action = re.search(r';\s*(?:e\s+)?\((?:II|III|IV)\)\s*hibernar\s+as\s+(?:plantas|unidades)\b'
                       r'(?=\s+(?:localizadas|situadas)\s+em\b)',
                       sentence[decision.end():], re.I)
    if not action:
        return None
    prefix = sentence[decision.end():decision.end() + action.start()].translate(
        str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"}))
    if prefix.count('"') % 2 or prefix.count("'") % 2:
        return None
    return {'company_declaration': declaration[0],
            'decision': sentence[decision.start():decision.end() + action.end()]}


def verify(document, subject, article):
    return sentiment_assessment(_verify(document, subject, article))


def _verify(document, subject, article):
    result = {'version': VERSION, 'status': 'needs_review', 'reason': '', 'evidence': [],
              'document_hash': document.get('sha256', ''), 'source_url': document.get('final_url', article['url']),
              'recovery': document.get('recovery',''), 'method': document.get('method', ''), 'language': document.get('language', '')}
    if document.get('status') != 'available':
        return {**result, 'status': 'unavailable', 'reason': document.get('reason', 'Source unavailable')}
    if document.get('review_only'):
        return {**result, 'reason': 'Recovered publisher text requires analyst confirmation of the event and company impact'}
    body = document['body']
    if document.get('method') == 'PDF text (no OCR)':
        try:
            validate_pdf_text(body)
        except ValueError as exc:
            return {**result, 'status': 'unavailable', 'reason': str(exc)}
    if article.get('discovery_service') == 'FCA Warning RSS':
        return {**result, 'reason': 'Possible clone warning; analyst must identify the affected legal entity'}
    # An English headline is not a translation of the full article.
    translated = document.get('language', '').lower().split('-')[0] not in ('en', 'pt')
    if translated:
        result['translation'] = article_translation.summary(document)
        try:
            body = article_translation.checked_text(document, subject)
        except ValueError as exc:
            return {**result, 'reason': str(exc)}
    # PDF lines wrap sentences; S.A. is part of a company name, not a sentence ending.
    line_break = r'\n{2,}' if document.get('method') == 'PDF text (no OCR)' else r'\n+'
    sentences = [s.strip() for s in re.split(r'(?<!\bS\.A\.)(?<=[.!?])\s+|' + line_break,
                                            body, flags=re.I) if s.strip()]
    names = sorted(company_names(subject), key=len, reverse=True)
    identity_seen, related_seen, ambiguous = False, False, False
    reported_context = set()
    roles = [normalized(term) for role, variants in PERSON_TITLES.items() for term in (role, *variants)]
    for index, sentence in enumerate(sentences):
        folded = normalized(sentence)
        related = related_identity(folded, subject)
        related_seen |= related
        if not headline_identity(folded, subject):
            if related:
                reported_context.update(case_context(sentence))
            continue
        identity_seen = True
        # Adjacent sentences can deny or date a claim without repeating the name.
        context = normalized(" ".join(sentences[max(0, index-1):index+2]))
        stages = case_context(context)
        reported_context.update(stages)
        # A person's criminal case, or a company's role as victim/witness, is not
        # evidence of corporate wrongdoing or a measured credit consequence.
        if stages:
            ambiguous = True
            continue
        # Portuguese "no" means "in the"; its negation is "não", handled by UNCERTAIN.
        english_negation = translated or document.get('language', '').lower().split('-')[0] != 'pt'
        negated = any(match[0] != 'no' or english_negation for match in NEGATION.finditer(context))
        if (UNCERTAIN.search(context) or negated or CONTEXT.search(context)
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
            attribution = None
            for name in names:
                # Company must be the grammatical subject at the start of this sentence.
                match = re.match(r'^(?:the |a empresa |empresa |a )?' + re.escape(name) + r'(?!\w)', folded)
                if not match or match.end() > start:
                    continue
                bridge = folded[match.end():start].strip()
                if related or any(re.search(r"(?<!\w)" + re.escape(role) + r"(?!\w)", bridge) for role in roles):
                    continue
                if len(bridge) <= 90 and BRIDGE.fullmatch(bridge):
                    linked = True
                    break
            if (not linked and phrase == 'hibernar' and folded.count('hibernar') == 1
                    and not translated and document.get('language', '').lower().split('-')[0] == 'pt'
                    and document.get('method') == 'PDF text (no OCR)' and article.get('source_kind') == 'official'):
                attribution = declared_hibernation(sentence, names)
                linked = attribution is not None
            if not linked:
                ambiguous = True
                continue
            evidence = {'score': score, 'rule': label, 'phrase': phrase,
                        'field': 'reviewed article translation' if translated else 'source article', 'sentence': sentence[:1200]}
            if attribution:
                evidence['attribution'] = attribution
            if translated:
                evidence['original_passages'] = article_translation.original_passages(document, sentence)
            result['evidence'].append(evidence)
    if reported_context:
        result['case_context'] = sorted(reported_context)
    if not identity_seen and related_seen:
        return {**result, 'reason': 'Named owner or manager mentioned; confirm identity, company relationship and impact'}
    if not identity_seen and document.get('excerpt'):
        return {**result, 'reason': 'Selected excerpt does not establish the company identity; check the original source'}
    if not identity_seen:
        return {**result, 'status': 'unmatched', 'reason': 'Extracted source text does not establish this company identity'}
    if ambiguous:
        return {**result, 'reason': ('Reported context: ' + ', '.join(sorted(reported_context)) +
                                    '; confirm subject role, source credibility and company impact') if reported_context else
                'Event attribution, denial, timing or conditional context needs analyst review'}
    if not result['evidence']:
        return {**result, 'reason': 'Company mentioned, but no explicit company-subject credit event was established'}
    dominant = min(result['evidence'], key=lambda e: (-abs(e['score']), e['score']))
    result.update(status='supported', score=dominant['score'],
                  reason='Source explicitly attributes an event to the named company: ' + dominant['rule'])
    if translated:
        scope = 'excerpt' if document.get('excerpt') else 'full-article'
        result['reason'] += f'; {scope} translation independently approved by Qwen (reasoning off)'
    # Used for narrow near-copy grouping; never group on event category alone.
    result['signature'] = normalized(' '.join(e['sentence'] for e in result['evidence']))
    return result
