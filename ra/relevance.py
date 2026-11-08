"""Explainable headline triage; never treat a search hit as entity verification."""

from __future__ import annotations

import re
from datetime import timedelta, timezone


from .aliases import company_names, contains, headline_identity, normalized


from .keywords import matches


def risk_signals(text: str, language: str = "") -> list[str]:
    return list(dict.fromkeys(group for _, group, direction in matches(text, language) if direction == "adverse"))


def headline(article: dict, field: str) -> str:
    text = article.get(field) or ""
    # Google appends publisher names, which are not evidence of the subject.
    publisher = article.get("publisher_en" if field == "title_en" else "publisher") or ""
    if publisher and text.endswith(" - " + publisher):
        text = text[:-(len(publisher) + 3)]
    return text


def governance_cues(article, subject):
    original = headline(article, "title")
    trusted_english = (article.get("headline_review") or {}).get("status") == "faithful"
    english = headline(article, "title_en") if trusted_english else ""
    text = normalized(original + " " + english)
    if ((article.get("verification") or {}).get("status") == "unmatched" or
        (article.get("source_kind") != "official" and not headline_identity(text, subject))):
        return []
    hits = matches(original, article.get("language")) + matches(english, "en")
    groups = {group for _, group, _ in hits}
    labels = []
    if groups & {"management change", "leadership action"} and (
            "executive role" in groups or (article.get("source_kind") == "official" and "management change" in groups)):
        labels.append("Leadership")
    if (article.get("discovery_service") == "PVFCCo investor disclosures" and
            contains(normalized(original), "thay doi nhan su") and "Leadership" not in labels):
        labels.append("Leadership")
    if "ownership" in groups:
        labels.append("Major ownership")
    return labels


def classify(article: dict, subject: dict, as_of=None) -> dict:
    title = headline(article, "title")
    english = headline(article, "title_en")
    text = normalized(title + " " + english)
    hits = sorted(set(matches(title, article.get("language")) + matches(english, "en")))
    signals = sorted({group for _, group, direction in hits if direction == "adverse"})
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
    verification = article.get("verification") or {}
    # When loaded from the database, source verification gates the risk queue.
    if "verification" in article:
        assessed = article.get("score_status") == "reviewed" or verification.get("status") == "supported"
        if assessed and article.get("score_included"):
            category = "risk" if (article.get("score") or 0) < 0 else "background"
            reason = article.get("score_reason", verification.get("reason", ""))
        elif verification.get("status") == "unmatched":
            category, reason = "background", verification["reason"]
        else:
            category, reason = "review", verification.get("reason", "Full-article verification pending")
    published = article.get("published_at")
    if category == "risk" and as_of:
        as_of = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of
        published = published.replace(tzinfo=timezone.utc) if published and published.tzinfo is None else published
        if not published or published > as_of + timedelta(days=1):
            category, reason = "review", "Missing or unexpected publication date; " + reason
        elif published < as_of - timedelta(days=30):
            category, reason = "older", "Older than 30 days at search time; " + reason
    return {**article, "governance": governance_cues(article, subject), "relevance": category, "relevance_reason": reason, "signal_terms": signals,
            "keyword_hits": [{"term": term, "direction": direction} for term, _, direction in hits]}


def classify_results(run: dict, results: list[dict]) -> list[dict]:
    subjects = run.get("subjects", [])
    by_id = {str(subject["id"]): subject for subject in subjects if subject.get("id")}
    by_name = {subject["name"]: subject for subject in subjects}
    return [classify(article, by_id.get(str(article.get("company_id"))) or
                     by_name.get(article["subject_name"], {"name": article["subject_name"]}),
                     run.get("window_end") or run.get("created_at")) for article in results]
