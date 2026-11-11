"""Keep watchlist names as written in publisher text across translation providers."""
import re
from collections import Counter
from functools import lru_cache

from .aliases import parse_aliases


def names(subject):
    if isinstance(subject, str):
        return (subject,) if subject else ()
    subject = subject or {}
    return tuple(dict.fromkeys(filter(None, (subject.get('name'), subject.get('short_name'),
                                             *parse_aliases(subject.get('aliases', ''))))))


@lru_cache(maxsize=256)
def pattern(labels):
    # Han, kana and Thai names can appear without spaces on either side.
    unspaced = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\u0e00-\u0e7f]')
    forms = []
    for label in sorted(labels, key=len, reverse=True):
        words = r'\s+'.join(re.escape(word) for word in label.split())
        if words:
            forms.append(('' if unspaced.match(label[0]) else r'(?<!\w)') + words +
                         ('' if unspaced.match(label[-1]) else r'(?!\w)'))
    return re.compile('|'.join(forms), re.I) if forms else None


def preserved(source, english, subject):
    matcher = pattern(names(subject))
    if not matcher:
        return True
    def counts(text):
        return Counter(' '.join(m.group().casefold().split()) for m in matcher.finditer(text))
    return not (counts(source) - counts(english))


def mask(source, subject):
    matcher = pattern(names(subject))
    originals = {}
    prefix = 'ZXQNAME'
    while prefix.casefold() in source.casefold():
        prefix += 'X'
    def replace(match):
        marker = f'{prefix}{len(originals)}ZXQ'
        originals[marker] = match.group()
        return marker
    return (matcher.sub(replace, source) if matcher else source), originals


def restore(english, originals):
    for marker, name in originals.items():
        if english.count(marker) != 1:
            raise ValueError('Translation changed a protected company name')
        english = english.replace(marker, name)
    return english
