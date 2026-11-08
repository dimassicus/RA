"""Versioned, explainable article sentiment and publication-week aggregation.

This is credit-oriented headline triage, not a credit rating or verified event.
No network or paid model calls are made here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .relevance import headline
from .aliases import headline_identity, normalized

VERSION = "source-credit-v5"
WEEK_TIMEZONE = ZoneInfo("Europe/Moscow")
LABELS = {-3: "Severe negativity", -2: "Strong negativity", -1: "Limited negativity",
          0: "Neutral / no directional evidence", 1: "Limited positivity",
          2: "Strong positivity", 3: "Severe positivity"}
RUBRIC = {
    -3: "Payment default, bankruptcy, insolvency or liquidation",
    -2: "Restructuring, serious liquidity stress, shutdown, material legal action or downgrade",
    -1: "Limited deterioration, losses, layoffs or management resignation",
    0: "No supported direction, balanced/uncertain context, or insufficient evidence",
    1: "Limited improvement, reduced losses, ordinary earnings or contract growth",
    2: "Material improvement: upgrade, restored operations, return to profit or secured refinancing",
    3: "Transformational recovery: completed bankruptcy exit or cure of a payment default",
}

# Longest overlapping phrase wins: 'emerges from bankruptcy' must not also
# become a bankruptcy event. Independent events retain separate evidence.
RULES = [
    (3, "completed recovery", r"(?:emerges?|exits?|emerged|exited) from bankruptcy|(?:completes?|completed) (?:its )?bankruptcy exit|(?:cures?|cured) (?:its )?(?:payment )?default|(?:fully repays?|fully repaid) defaulted debt|encerra recuperacao judicial|sai da recuperacao judicial"),
    (2, "credit or funding improvement", r"(?:credit |rating )upgrad\w*|upgrad\w* (?:its |the )?(?:credit |issuer |debt )?rating|(?:secures?|secured|completes?|completed) refinancing|(?:returns?|returned) to profit(?:ability)?|(?:resumes?|resumed|restores?|restored) (?:production|operations)|volta ao lucro|retoma (?:a )?(?:producao|operacoes)"),
    (1, "limited improvement", r"(?:reduces?|reduced|narrows?|narrowed|cuts?|cut) (?:its )?losses|(?:losses|net loss) (?:narrow\w*|fall\w*|fell|shrink\w*|declin\w*|decreas\w*|reduc\w*)|(?:profit|earnings|revenue|sales) (?:grow\w*|ris\w*|rose|increas\w*|improv\w*)|(?:wins?|won|secures?|secured) (?:a |new |major )?contract|lucro (?:cresce|aumenta)|reduz (?:o )?prejuizo|queda (?:do|no) prejuizo"),
    (-3, "default or insolvency", r"bankrupt\w*|insolven\w*|liquidation|(?:payment |debt |bond )?default(?:s|ed)?|missed (?:debt |bond |interest )?payments?|fails? to (?:repay|pay creditors)|falencia|inadimplencia|vo no|pha san|iflas"),
    (-2, "material financial deterioration", r"restructur\w*|recuperacao (?:extra)?judicial|liquidity crisis|cash (?:crisis|shortfall)|profit warning|(?:credit |rating )downgrad\w*|downgrad\w* (?:its |the )?(?:credit |issuer |debt )?rating|negative outlook|debt crisis|konkordato\w*|temerrut\w*|(?:profit|earnings) (?:plung\w*|collaps\w*)"),
    (-2, "operating disruption", r"shutdown\w*|plant closure|plant fire|explosion|(?:halts?|suspends?) (?:production|operations)|hibernacao|hibernar|paralisacao|fechamento|suspensao (?:das? )?(?:operacoes|producao)"),
    (-2, "legal or creditor pressure", r"(?:faces?|facing|hit by) (?:a |an )?(?:lawsuit|investigation|sanctions)|(?:fined|convicted)|(?:creditor |debt )?mediation|procedimento de mediacao|tutela de urgencia|arbitration|arbitragem|multas?|condena\w*"),
    (-1, "limited financial deterioration", r"loss(?:es)?|prejuizo\w*|(?:profit|earnings|revenue|sales) (?:fall\w*|fell|declin\w*|drop\w*|decreas\w*)|queda (?:do|no|de) lucro|lucro cai|zarar\w*|thua lo"),
    (-1, "staff or management disruption", r"layoffs?|resign\w*|renuncia\w*|demiss\w*|istifa\w*|strike|greve|tu chuc"),
]
COMPILED = [(score, label, re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)"))
            for score, label, pattern in RULES]
UNCERTAIN = re.compile(r"\b(?:denies?|denied|alleged|rumou?r\w*|may|might|could|potential|risk of|no evidence of|no signs of|nao|nega|sem)\b")
NEGATION = re.compile(r"\b(?:not|no|avoids?|averts?|avoided|averted)\b")


def week_start(value: datetime) -> object:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    date = value.astimezone(WEEK_TIMEZONE).date()
    return date - timedelta(days=date.weekday())


def input_hash(article: dict, subject: dict, now: datetime | None = None) -> str:
    values = {key: article.get(key) for key in
              ("title", "title_en", "publisher", "publisher_en", "language", "published_at", "source_kind", "discovery_service", "verification")}
    values["subject"] = subject
    published = article.get("published_at")
    if published and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    values["publication_date_valid"] = bool(published and published <= (now or datetime.now(timezone.utc)))
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def score_article(article: dict, subject: dict, now: datetime | None = None) -> dict:
    """Score every lead; keep uncertain/irrelevant zeroes out of weekly means."""
    now = now or datetime.now(timezone.utc)
    original, english = headline(article, "title"), headline(article, "title_en")
    text = normalized(original + " " + english)
    official = article.get("source_kind") == "official"
    identity = official or headline_identity(text, subject)
    evidence = []
    for field, raw in (("original headline", original), ("English headline", english)):
        folded = normalized(raw)
        candidates = [(match.start(), match.end(), score, label, match.group())
                      for score, label, pattern in COMPILED for match in pattern.finditer(folded)]
        accepted = []
        for start, end, score, label, phrase in sorted(candidates, key=lambda row: -(row[1] - row[0])):
            if any(start < other_end and end > other_start for other_start, other_end in accepted):
                continue
            accepted.append((start, end))
            evidence.append({"score": score, "rule": label, "phrase": phrase, "field": field})
    result = {"score": 0, "score_auto": 0, "score_status": "provisional",
              "score_included": True, "score_version": VERSION,
              "score_evidence": evidence, "score_input_hash": input_hash(article, subject, now),
              "score_reason": "No directional credit evidence in the available headline"}
    problem = ""
    if article.get("source_kind") == "context":
        problem = "Potential exposure requires source evidence of an impact on this company"
    elif not identity:
        problem = "Company identity is not supported by the headline"
    elif article.get("discovery_service") == "FCA Warning RSS":
        problem = "Possible clone warning; confirm which entity is affected"
    elif article.get("language", "").split("-")[0] not in ("", "en", "pt") and not english:
        problem = "English translation unavailable; native-language coverage is incomplete"
    elif evidence and (UNCERTAIN.search(text) or NEGATION.search(text)):
        problem = "Conditional, denied, negated or uncertain event; review context"
    if evidence and not problem:
        # Never add repeated terms; most material direction wins, ties adverse.
        dominant = min(evidence, key=lambda item: (-abs(item["score"]), item["score"]))
        result["score"] = result["score_auto"] = dominant["score"]
        result["score_reason"] = dominant["rule"] + ": “" + dominant["phrase"] + "”"
        if any(item["score"] < 0 for item in evidence) and any(item["score"] > 0 for item in evidence):
            result["score_reason"] += "; mixed directions, strongest magnitude used (negative wins ties)"
    if problem:
        result.update(score_included=False, score_status="needs_review", score_reason=problem)
    verification = article.get("verification") or {}
    if verification.get("status") == "supported":
        result.update(score=verification["score"], score_auto=verification["score"],
                      score_evidence=verification["evidence"], score_status="provisional",
                      score_included=True, score_reason=verification["reason"])
    else:
        result.update(score_included=False, score_status="needs_review",
                      score_reason=result["score_reason"] + "; " + verification.get("reason", "Full-article verification pending"))
    published = article.get("published_at")
    if published and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if not published or published > now:
        result.update(score_included=False, score_status="needs_review",
                      score_reason=result["score_reason"] + "; missing or future publication date")
    return result


def aggregate_week(articles: list[dict]) -> dict:
    """One contribution per event group; reviewed copy takes precedence."""
    unique = {}
    for article in articles:
        key = article.get("event_group") or article["url"]
        reviewed = article.get("score_status") == "reviewed"
        rank = (reviewed, bool(article.get("score_included")) if not reviewed else False,
                article.get("score_updated_at") or article["created_at"], article["id"])
        if key not in unique or rank > unique[key][0]:
            unique[key] = (rank, article)
    records = [item[1] for item in unique.values()]
    # Conflicting automatic assessments must not silently choose a direction.
    conflicts = {key for key, (_, representative) in unique.items()
                 if representative.get("score_status") != "reviewed" and len({
                     a.get("score") for a in articles
                     if (a.get("event_group") or a["url"]) == key and a.get("score_included")}) > 1}
    included = [a for a in records if a.get("score_included") and a.get("score") is not None
                and (a.get("event_group") or a["url"]) not in conflicts]
    duplicate_ids = sorted(a["id"] for a in articles if a["id"] not in {r["id"] for r in records})
    values = [a["score"] for a in included]
    mean = (Decimal(sum(values)) / len(values)) if values else None
    return {
        "score": int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if mean is not None else None,
        "mean_score": float(mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if mean is not None else None,
        "event_count": len(values), "duplicate_count": len(duplicate_ids), "duplicate_ids": duplicate_ids,
        "group_conflicts": sorted(conflicts),
        "groups": [{"id": key, "representative_id": representative["id"],
                    "article_ids": sorted(a["id"] for a in articles if (a.get("event_group") or a["url"]) == key)}
                   for key, (_, representative) in sorted(unique.items())],
        "article_count": len(values), "excluded_count": len(records) - len(values),
        "negative_count": sum(value < 0 for value in values),
        "neutral_count": values.count(0), "positive_count": sum(value > 0 for value in values),
        "worst_score": min(values) if values else None, "best_score": max(values) if values else None,
        "reviewed_count": sum(a["score_status"] == "reviewed" for a in included),
        "article_ids": sorted(a["id"] for a in included),
        "excluded_ids": sorted(a["id"] for a in records if a not in included),
        "contributions": [{"id": a["id"], "url": a["url"], "score": a["score"],
                           "status": a["score_status"], "reason": a.get("score_reason", ""),
                           "version": a.get("score_version", VERSION), "event_group": a.get("event_group", "")} for a in sorted(included, key=lambda a: a["id"])],
        "version": VERSION, "timezone": str(WEEK_TIMEZONE),
    }


def weekly_groups(articles: list[dict]) -> dict:
    groups = defaultdict(list)
    for article in articles:
        if article.get("company_id"):
            date = article.get("published_at") or article["created_at"]
            groups[(article["company_id"], week_start(date))].append(article)
    return groups
