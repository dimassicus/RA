"""Company developments that warrant credit review before their direction is known."""
import re

from .aliases import contains, normalized, parse_aliases

ASSETS = "Court restrictions on company assets, objections, and full or partial release of seized property"
CAPEX = "Capital investment plans that may increase borrowing needs or strain liquidity"
STRATEGY = "Major, sharp changes of corporate strategies, global plans, decisions to enter new markets or product niches"

# Use the same short expressions for targeted discovery and initial lead screening.
TERMS = {
    ASSETS: {
        "en": ("asset seizure", "assets seized", "asset freeze", "assets frozen", "property attachment", "asset arrest", "court order", "objection petition", "assets returned", "remain frozen", "remain under seizure", "litigation continues"),
        "id": ("penyitaan aset", "aset disita", "tetap disita", "permohonan keberatan", "pengembalian aset", "dikembalikan", "menggugat", "masih disita", "sengketa berlanjut"),
    },
    CAPEX: {
        "en": ("capital expenditure", "capex", "investment plan", "plans to invest", "new plant", "new factory"),
        "id": ("belanja modal", "capex", "rencana investasi", "investasi pabrik", "pabrik baru", "siapkan belanja"),
    },
    STRATEGY: {
        "en": ("new market", "new markets", "new business segment", "diversification", "market entry", "expands into"),
        "id": ("pasar baru", "segmen baru", "diversifikasi", "perluas pasar", "merambah", "lini bisnis baru"),
    },
}

LABELS = {
    ASSETS: "Court proceedings or asset restrictions · check the ruling, any relief and remaining restrictions",
    CAPEX: "Capital spending · check the amount, funding source and effect on liquidity",
    STRATEGY: "Market expansion · check the new exposure, experience and execution risk",
}

# Orders of words in legal reporting often differ from compact search phrases.
ASSET_EVENT = re.compile(r"\b(?:assets?|properties|aset|properti)\b.{0,65}\b(?:seized|frozen|returned|disita|dikembalikan)\b"
                         r"|\b(?:froze|freezes|seizes|seized|returned|arrested|kembalikan|sita)\b.{0,65}\b(?:assets?|properties|aset|properti)\b")


PARTIAL_RELIEF = "Partial asset release reported · relief granted on part of the claim"
RESIDUAL_FREEZE = "Adverse legal exposure · assets reported still frozen or seized"
OPEN_CASE = "Adverse legal exposure · litigation reported ongoing"

_ASSET = r"(?:assets?|propert(?:y|ies)|accounts?|aset|properti|rekening)"
_STILL = r"(?:remain(?:s)? (?:frozen|seized|under seizure)|still (?:frozen|seized)|remain(?:s)? subject to (?:a freeze|seizure)|(?:masih|tetap) (?:disita|dibekukan))"
_PARTIAL = re.compile(r"\b(?:partial(?:ly)? (?:release|relief|lifted)|sebagian (?:dikembalikan|dibebaskan))\b")
_RELEASE = re.compile(r"\b(?:released?|returned|lifted|dikembalikan|kembalikan|dibebaskan)\b")
_NUM = r"(?:[0-9]{1,6}|one|two|three|four|five|six|seven|eight|nine|ten|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh)"
_NUMBERS = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
_NUMBERS.update(zip("satu dua tiga empat lima enam tujuh delapan sembilan sepuluh".split(), range(1, 11)))
_FRACTION = re.compile(r"\b(" + _NUM + r") (?:out of|of|dari) (?:the |total )?(" + _NUM + r")\b")
_QUALIFIED = re.compile(r"\b(?:if|may|might|could|would|whether|denies|denied|previously|not|no longer|no (?:\w+ ){0,4}(?:assets?|properties)|"
                        r"no properties|none|tidak|belum tentu|jika|mungkin|bantah|membantah)\b")
_ONGOING = re.compile(r"\b(?:(?:litigation|court (?:case|battle)|legal proceedings?) (?:continues?|ongoing|"
                      r"(?:is|are|remains?) (?:pending|ongoing))|ongoing (?:litigation|court battle)|"
                      r"(?:sengketa|proses hukum) (?:berlanjut|masih berlangsung))\b")


_CONTINUATION = re.compile(r"(?:(?:but|however|yet|namun|tetapi|sementara) )?(?:the )?"
                           r"(?:other |remaining )?" + _NUM + r"(?: of (?:them|these))?"
                           r"(?: (?:investment )?" + _ASSET + r")? " + _STILL + r"\b")


def legal_signals(text):
    """Describe the reported relief and continuing exposure independently of the credit score."""
    if _QUALIFIED.search(text):
        return []
    labels = []
    if re.search(r"\b" + _ASSET + r"\b", text):
        fraction = _FRACTION.search(text)
        if fraction and _RELEASE.search(text):
            a, b = (int(n) if n.isdecimal() else _NUMBERS[n] for n in fraction.groups())
            if 0 < a < b:
                labels.append(PARTIAL_RELIEF)
        if not labels and _PARTIAL.search(text):
            labels.append(PARTIAL_RELIEF)
        if re.search(r"\b" + _STILL + r"\b", text):
            labels.append(RESIDUAL_FREEZE)
    if _ONGOING.search(text):
        labels.append(OPEN_CASE)
    return labels


def cues(factor, code):
    return tuple(dict.fromkeys((*TERMS.get(factor["english"], {}).get(code, ()),
                                *factor.get("search_cues", {}).get(code, ()))))


def review_topics(article, subject):
    from . import risk_factors
    from .relevance import headline
    from .search_settings import named_subject

    if (article.get("verification") or {}).get("status") == "unmatched" and article.get("score_status") != "reviewed":
        return []
    factors = risk_factors.active("company")
    lang = (article.get("language") or "").split("-")[0]
    units = [(headline(article, "title"), lang), (headline(article, "title_en"), "en"),
             (article.get("summary") or article.get("discovery_summary", ""), lang)]
    tickers = [a for a in parse_aliases(subject.get("aliases", "")) if re.fullmatch("[A-Z]{4}", a)
               and normalized(subject.get("country_registration", "")) in ("id", "idn", "indonesia")]
    found = []
    legal_enabled = any(factor["english"] == ASSETS for factor in factors)
    # The theme reviewer has already attributed these verbatim source quotes to the issuer.
    for match in (article.get("risk_factor_review") or {}).get("matches", []):
        if legal_enabled and match.get("factor") == ASSETS:
            found.extend(legal_signals(normalized(match.get("quote", ""))))
    for text, code in units:
        # Market roundups often give each issuer its own comma-separated headline.
        issuer_assets = False
        for clause in re.split(r"[,;|\n]|(?<=[.!?])\s+", text):
            explicit = named_subject(clause, subject)
            ticker = any(re.match(r"\s*" + symbol + r"\b", clause) for symbol in tickers)
            folded = normalized(clause)
            if not folded:
                continue
            if issuer_assets and _CONTINUATION.fullmatch(folded):
                found.append(RESIDUAL_FREEZE)
            issuer_assets = False
            if not (explicit or ticker or article.get("source_kind") == "official"):
                continue
            for factor in factors:
                key = factor["english"]
                if key not in LABELS:
                    continue
                matched = any(contains(folded, normalized(term)) for language in {code, "en"}
                              for term in cues(factor, language))
                if key == ASSETS:
                    labels = legal_signals(folded)
                    issuer_assets = bool(re.search(r"\b" + _ASSET + r"\b", folded) and
                                         (labels or ASSET_EVENT.search(folded)))
                    if labels:
                        found.extend(labels)
                        continue
                if key == ASSETS and ASSET_EVENT.search(folded):
                    matched = True
                if key == ASSETS and matched and not re.search(r'\b(?:aset|properti|asset\w*|property|properties|court|pengadilan|keberatan|freezing|seizure)\b', folded):
                    matched = False
                if matched and LABELS[key] not in found:
                    found.append(LABELS[key])
    if any(label in found for label in (PARTIAL_RELIEF, RESIDUAL_FREEZE, OPEN_CASE)):
        found = [label for label in found if label != LABELS[ASSETS]]
    return list(dict.fromkeys(found))
