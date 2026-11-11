"""Select attributable passages from long filings before translation or model review."""
import re

from .decision_review import OPERATIVE

TABLE = re.compile(r'statements? of (?:consolidated )?(?:profit or loss|income)|income statements?|'
                   r'laporan laba rugi|demonstração do resultado|отчет о финансовых результатах', re.I)
UNITS = re.compile(r'(?:in|dalam|em|en)\s+(?:millions?|thousands?|jutaan|ribuan|milhares|millones)\b|'
                   r'jutaan rupiah|satuan penyajian|rounding level|уровень округления', re.I)
PROFIT = re.compile(r'profit\s*\(loss\) attributable to\s+(?:owners|parent)|net (?:profit|income|loss)|'
                    r'laba bersih|laba \(rugi\) yang dapat\s+diatribusikan ke\s+entitas induk|'
                    r'lucro líquido|utilidad neta|чистая прибыль|bénéfice net|净利润|純利益', re.I)
RESIDUAL = re.compile(r'(?:remain|still|continue|tetap|masih).{0,75}(?:seiz|siz|sita|restrict|frozen|confiscat)|'
                      r'(?:seiz|sita|restrict).{0,35}(?:remain|tetap)', re.I | re.S)
PAGE = re.compile(r'^PDF page (\d+)$', re.M)
SEPARATOR = '\n\n[…]\n\n'


def select(body, size, matcher, decision):
    """Store original offsets and PDF page numbers; mark gaps between quoted passages."""
    anchors = []

    def add(pattern, share, numeric=False):
        matches = pattern.finditer(body)
        if numeric:
            match = max(matches, key=lambda m: min(2, len(re.findall(r'\d[\d.,]+', body[max(0, m.start()-80):m.end()+110]))), default=None)
        else:
            match = next(matches, None)
        if match:
            anchors.append((match.start(), share))

    if decision:
        add(OPERATIVE, 3)
        add(RESIDUAL, 3)
    if not anchors and (TABLE.search(body) or PROFIT.search(body)):
        add(PROFIT, 5, numeric=True)
        add(TABLE, 3)
        add(UNITS, 2)
    if matcher:
        add(matcher, 1)
    if not anchors:
        anchors.append((0, 1))
    total = sum(weight for _, weight in anchors)
    available = max(1, size - len(SEPARATOR) * (len(anchors) - 1))
    intervals = []
    for pos, weight in anchors:
        length = max(1, available * weight // total)
        start = max(0, min(len(body) - length, pos - length // 3))
        intervals.append((start, start + length))
    spans = []
    for start, stop in sorted(intervals):
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], stop))
        else:
            spans.append((start, stop))
    pages = list(PAGE.finditer(body))
    ranges = []
    for start, stop in spans:
        item = dict(start=start, end=stop)
        numbers = [int(m[1]) for i,m in enumerate(pages) if m.start() < stop and
                   (i+1 == len(pages) or pages[i+1].start() > start)]
        if numbers:
            item['pages'] = numbers
        ranges.append(item)
    return SEPARATOR.join(body[a:b] for a,b in spans), ranges
