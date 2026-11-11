"""Persistent defaults, snapshotted once for each search run."""
import re

from . import db, company_developments
from .aliases import contains, headline_identity, normalized, parse_aliases, related_identity
from .relevance import headline

DEFAULTS = {"include_country": True, "include_industry": True, "adverse_only": False}


def read():
    with db.connect() as conn:
        row = conn.execute("SELECT include_country,include_industry,adverse_only FROM search_preferences WHERE id=TRUE").fetchone()
    return dict(row) if row else dict(DEFAULTS)


def save(values):
    if set(values) != set(DEFAULTS) or any(type(value) is not bool for value in values.values()):
        raise ValueError("Choose On or Off for each search setting.")
    with db.connect() as conn:
        conn.execute("INSERT INTO search_preferences (id,include_country,include_industry,adverse_only) "
                     "VALUES (TRUE,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
                     "include_country=EXCLUDED.include_country,include_industry=EXCLUDED.include_industry,"
                     "adverse_only=EXCLUDED.adverse_only",
                     tuple(values[key] for key in DEFAULTS))


def for_run(run):
    # Old runs retain the behavior in force before these switches existed.
    return DEFAULTS | (run.get("search_settings") or {})


def scope_enabled(scope, values):
    key = {"country": "include_country", "sector": "include_industry"}.get(scope)
    return values[key] if key else True


def named_subject(text, subject):
    folded = normalized(text)
    legal = normalized(subject.get("name", ""))
    # Indonesian business headlines often name the stock only: "laba LTLS" or "saham LTLS".
    if normalized(subject.get("country_registration", "")) in ("indonesia", "id", "idn"):
        for symbol in parse_aliases(subject.get("aliases", "")):
            if re.fullmatch("[A-Z]{4}", symbol) and re.search(
                    r"(?i:\b(?:saham|emiten|laba(?: bersih)?|pendapatan|obligasi|BEI|IDX))[:\s]+" +
                    symbol + r"\b|\b" + symbol + r" (?i:mencatat|membukukan|gagal bayar)\b", text):
                return True
    return (headline_identity(folded, subject) or related_identity(folded, subject)
            or bool(legal and contains(folded, legal) and
                    (len(legal) >= 6 or re.search(r"(?<!\w)" + re.escape(subject["name"]) + r"(?!\w)", text))))


def company_lead(article, subject):
    return (article.get("source_kind") == "official"
            or article.get("discovery_service") == "Discovered company disclosures"
            or any(named_subject(text, subject) for text in
                   (headline(article, "title"), headline(article, "title_en"), article.get("summary", ""))))


def adverse_lead(article, subject):
    """Use company-linked discovery evidence; only source verification can support a score."""
    from .idx_filings import ARCHIVE_SERVICE
    if article.get("discovery_service") == ARCHIVE_SERVICE and named_subject(article.get("title", ""), subject):
        # Issuers describe their own assets as "the Company's assets" in disclosure text.
        for sentence in re.split(r"[.!?\n]", article.get("summary") or ""):
            if re.search(r"\b(?:company s (?:assets?|propert(?:y|ies))|aset perseroan)\b.{0,140}"
                         r"\b(?:subject to seizure|seized|frozen|disita|dirampas)\b", normalized(sentence)):
                return True
    if company_developments.review_topics(article, subject):
        return True
    if article.get("discovery_service") == "FCA Warning RSS":
        return True
    context = bool(article.get("exposure_paths")) or article.get("source_kind") == "context"
    if (context or company_lead({**article, "summary": ""}, subject)) and _adverse_headline(article, subject):
        return True
    # Never join one issuer's name to another issuer's risk in a separate summary sentence.
    for sentence in re.split(r"(?<=[.!?])\s+|\n", article.get("summary", "")):
        if sentence and named_subject(sentence, subject) and _adverse_headline(
                {"title": sentence, "language": article.get("language", "")}, subject):
            return True
    return False


def _adverse_headline(article, subject):
    """Headline triage only; source checks and analyst review establish actual impact."""
    from .keywords import matches
    from .scoring import score_article
    # Indonesian market-close wording is not an operating shutdown.
    article = {**article, "title": re.sub(r"\b(?:pada penutupan (?=\d|sesi|perdagangan|bursa)|penutupan (?:perdagangan|bursa))\b", " ",
                                         article.get("title", ""), flags=re.I)}
    if re.search(r"\b(?:laba(?: bersih)?|pendapatan|penjualan)\s+(?:(?:[A-Z]{4}|bersih)\s+)?"
                 r"(?:turun|anjlok|merosot|menurun)\b", headline(article, "title"), flags=re.I):
        return True
    evidence = score_article(article, subject)["score_evidence"]
    if any(item["score"] < 0 for item in evidence):
        return True
    recovered = set()
    if evidence:
        # Reuse overlap handling: exiting bankruptcy or reducing losses is favourable.
        for item in evidence:
            recovered.update(matches(item["phrase"], article.get("language")))
            recovered.update(matches(item["phrase"], "en"))
    hits = matches(headline(article, "title"), article.get("language"))
    hits += matches(headline(article, "title_en"), "en")
    return any(hit[2] == "adverse" and hit not in recovered for hit in hits)
