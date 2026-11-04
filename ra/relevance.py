"""Explainable headline triage; never treat a search hit as entity verification."""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta, timezone


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", value))


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


LEGAL_SUFFIX = re.compile(
    r"\s+(?:s\s*a|s\s*a\s*s|a\s*s|s\s*a\s*r\s*l|ltda|limited|ltd|inc|plc|llc|corporation|"
    r"sociedade anonima(?: fechada)?|anonim sirketi)$"
)


def company_names(subject: dict) -> list[str]:
    names = []
    for value in (subject.get("name", ""), subject.get("short_name", "")):
        name = normalized(value)
        for candidate in (name, LEGAL_SUFFIX.sub("", name)):
            if len(candidate) >= 4 and candidate not in names:
                names.append(candidate)
    return names


def contains(text: str, phrase: str) -> bool:
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


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
    matches = [name for name in company_names(subject) if contains(text, name)]
    identity = official or bool(matches)
    # Short one-token aliases and very short brands need contextual corroboration.
    if not official and matches and all(len(name.replace(" ", "")) < 6 for name in matches):
        contexts = [subject.get("country_registration", "")]
        contexts += re.split(r"[,;\n]", subject.get("primary_industries", ""))
        identity = any(contains(text, normalized(context)) for context in contexts)
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
