"""Conservative quote matching with offsets into the untouched original text."""
import html
import re
import unicodedata

ENTITY = re.compile(r'&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);')
TYPOGRAPHIC = str.maketrans({'‘':"'", '’':"'", '“':'"', '”':'"'})


def canonical(text):
    """Return normalized text and per-character original [start,end) ranges.

    Decode one layer of terminated HTML entities, NFC-combine accents, normalize
    typographic quotes and collapse whitespace. Never fold case, digits, words,
    decimal punctuation, negation, or ellipses. This is not fuzzy matching.
    """
    units=[]; pos=0
    while pos<len(text):
        match=ENTITY.match(text,pos)
        end=match.end() if match else pos+1
        token=match.group() if match else text[pos]
        recognized=match and (token.startswith('&#') or token[1:] in html.entities.html5)
        value=html.unescape(token) if recognized else token
        units.extend((char,pos,end) for char in value)
        pos=end
    chars=[]; ranges=[]; i=0
    while i<len(units):
        j=i+1
        while j<len(units) and unicodedata.combining(units[j][0]):j+=1
        value=unicodedata.normalize('NFC',''.join(u[0] for u in units[i:j])).translate(TYPOGRAPHIC)
        start,end=units[i][1],units[j-1][2]
        for char in value:
            if char.isspace():
                if chars and chars[-1]==' ':ranges[-1]=(ranges[-1][0],end)
                else:chars.append(' ');ranges.append((start,end))
            else:chars.append(char);ranges.append((start,end))
        i=j
    return ''.join(chars),ranges


def resolve_spans(text,quotes,*,allow_empty=False):
    if not isinstance(quotes,list) or len(quotes)>6 or (not quotes and not allow_empty):
        raise ValueError('Evidence must contain one to six separate quoted spans')
    normalized,offsets=canonical(text); spans=[]; seen=set()
    for quote in quotes:
        if not isinstance(quote,str) or not quote.strip() or len(quote)>1600:
            raise ValueError('Invalid evidence text')
        needle=canonical(quote)[0].strip()
        if not needle:raise ValueError('Empty normalized evidence')
        start=normalized.find(needle)
        first_match = start
        while start>=0:
            end=start+len(needle)
            left=start>0 and needle[0].isalnum() and normalized[start-1].isalnum()
            right=end<len(normalized) and needle[-1].isalnum() and normalized[end].isalnum()
            if not left and not right:break
            start=normalized.find(needle,start+1)
        if start<0:
            if first_match >= 0:
                lo, hi = first_match, first_match + len(needle)
                while lo > 0 and normalized[lo-1].isalnum() and normalized[lo].isalnum():
                    lo -= 1
                while hi < len(normalized) and normalized[hi-1].isalnum() and normalized[hi].isalnum():
                    hi += 1
                raise ValueError('Evidence must include complete words, including attached prefixes. Copy the whole span or choose another: '
                                 + repr(normalized[lo:hi][:140]))
            raise ValueError('Evidence does not match an original text span')
        raw_start,raw_end=offsets[start][0],offsets[end-1][1]
        if (raw_start, raw_end) in seen:
            continue
        seen.add((raw_start,raw_end))
        spans.append({'start':raw_start,'end':raw_end,'text':text[raw_start:raw_end],
                      'quote':quote,'normalized_match':quote!=text[raw_start:raw_end]})
    return spans
