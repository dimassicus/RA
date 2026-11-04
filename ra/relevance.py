"""Explainable headline triage; never treat a search hit as entity verification."""

from __future__ import annotations

import re
from datetime import timedelta, timezone


from .aliases import company_names, contains, headline_identity, normalized


# Whole words/explicit stems avoid 'loss' in 'gloss' and 'multa' in 'simultaneous'.
# Native cues supplement English translations; unsupported languages stay reviewable.
PATTERNS = {
    "debt / creditors": r"default\w*|missed payments?|overdue|debt crisis|creditor\w*|inadimpl\w*|divida vencida|vỡ nợ|vo no|temerrut\w*",
    "restructuring": r"bankrupt\w*|insolven\w*|restructur\w*|recuperacao (?:extra)?judicial|falencia|mediacao|mediation|tutela de urgencia|konkordato\w*|iflas\w*|pha san|افلاس|إفلاس|πτωχευσ\w*",
    "legal / regulatory": r"lawsuits?|litigation|sanctions?|investigat\w*|fined|fines|fraud\w*|arbitration|arbitragem|investiga\w*|multas?|condena\w*|corrup\w*|sorusturma\w*|ceza\w*|dieu tra",
    "operations": r"shutdowns?|closures?|layoffs?|plant fire|explosion|accident|strike|fechamento|paralis\w*|demiss\w*|hibernacao|suspens\w*|greve|grev|yangin|dinh cong|dong cua",
    "financial deterioration": r"loss(?:es)?|prejuizo\w*|liquidity (?:crisis|shortfall)|cash (?:crisis|shortfall)|profit warning|downgrad\w*|negative outlook|lucro cai|queda (?:do|no|de) lucro|zarar\w*|thua lo|خسائر",
    "management change": r"resign\w*|renuncia\w*|istifa\w*|tu chuc",
}
PATTERNS = {label: re.compile(r"(?<!\w)(?:" + normalized_pattern + r")(?!\w)")
            for label, normalized_pattern in PATTERNS.items()}


def risk_signals(text: str) -> list[str]:
    folded = normalized(text)
    return [label for label, pattern in PATTERNS.items() if pattern.search(folded)]


def headline(article: dict, field: str) -> str:
    text = article.get(field) or ""
    # Google appends publisher names, which are not evidence of the subject.
    publisher = article.get("publisher_en" if field == "title_en" else "publisher") or ""
    if publisher and text.endswith(" - " + publisher):
        text = text[:-(len(publisher) + 3)]
    return text


def classify(article: dict, subject: dict, as_of=None) -> dict:
    title = headline(article, "title")
    english = headline(article, "title_en")
    text = normalized(title + " " + english)
    signals = sorted(set(risk_signals(title) + risk_signals(english)))
    official = article.get("source_kind") == "official"
    if article.get("discovery_service") == "FCA Warning RSS":
        signals = sorted(set(signals + ["regulatory warning / possible clone"]))
    identity = official or headline_identity(text, subject)
    if not signals:
        category, reason = "background", "No adverse-event cue in the available headline or translation"
        if identity and article.get("language", "").split("-")[0] not in ("", "en") and not english:
            category, reason = "review", "Company match, but English translation is unavailable; check original"
    elif not identity:
        category, reason = "review", "Risk cue found, but the company identity needs checking"
    else:
        category, reason = "risk", ("Company-specific official source" if official else "Company name in headline") + "; " + ", ".join(signals)
    # Negated or resolved events need human context, not an automatic adverse flag.
    if category == "risk" and re.search(
        r"\b(?:no (?:evidence of |signs of )?|denies? |avoids? |averts? |sem |nao ha )"
        r"(?:fraud|default|bankruptcy|loss|prejuizo|inadimplencia)", text
    ):
        category, reason = "review", "Possible negated or resolved event; check source context"
    published = article.get("published_at")
    if category == "risk" and as_of:
        as_of = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of
        published = published.replace(tzinfo=timezone.utc) if published and published.tzinfo is None else published
        if not published or published > as_of + timedelta(days=1):
            category, reason = "review", "Missing or unexpected publication date; " + reason
        elif published < as_of - timedelta(days=30):
            category, reason = "older", "Older than 30 days at search time; " + reason
    return {**article, "relevance": category, "relevance_reason": reason, "signal_terms": signals}


def classify_results(run: dict, results: list[dict]) -> list[dict]:
    subjects = run.get("subjects", [])
    by_id = {str(subject["id"]): subject for subject in subjects if subject.get("id")}
    by_name = {subject["name"]: subject for subject in subjects}
    return [classify(article, by_id.get(str(article.get("company_id"))) or
                     by_name.get(article["subject_name"], {"name": article["subject_name"]}),
                     run.get("created_at")) for article in results]
