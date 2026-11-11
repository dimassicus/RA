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

VERSION = "company-event-v1"
WEEK_TIMEZONE = ZoneInfo("Europe/Moscow")
LABELS = {-1: "Negative", 0: "Neutral", 1: "Positive"}
RUBRIC = {
    -1: "Evidence of adverse company impact, including losses, payment stress, or disruption.",
    0: "No directional impact established. An unchecked lead is not confirmed neutral evidence.",
    1: "Evidence of improvement, such as recovery, stronger earnings, or improved funding.",
}
MATERIALITY = ("Assess the size, duration and financial consequences for the named company. "
               "There is no universal percentage threshold. Positive leads are archived. "
               "Only selected, eligible evidence can contribute to weekly company events.")

# Longest overlapping phrase wins: 'emerges from bankruptcy' must not also
# become a bankruptcy event. Independent events retain separate evidence.
RULES = [
    (2, "completed recovery", r"(?:emerges?|exits?|emerged|exited) from bankruptcy|(?:completes?|completed) (?:its )?bankruptcy exit|(?:cures?|cured) (?:its )?(?:payment )?default|(?:fully repays?|fully repaid) defaulted debt|encerra recuperacao judicial|sai da recuperacao judicial"),
    (2, "credit or funding improvement", r"(?:credit |rating )upgrad\w*|upgrad\w* (?:its |the )?(?:credit |issuer |debt )?rating|(?:secures?|secured|completes?|completed) refinancing|(?:returns?|returned) to profit(?:ability)?|(?:resumes?|resumed|restores?|restored) (?:production|operations)|volta ao lucro|retoma (?:a )?(?:producao|operacoes)"),
    (1, "limited improvement", r"(?:reduces?|reduced|narrows?|narrowed|cuts?|cut) (?:its )?losses|(?:losses|net loss) (?:narrow\w*|fall\w*|fell|shrink\w*|declin\w*|decreas\w*|reduc\w*)|(?:profit|earnings|revenue|sales) (?:grow\w*|ris\w*|rose|increas\w*|improv\w*)|(?:wins?|won|secures?|secured) (?:a |new |major )?contract|lucro (?:cresce|aumenta)|reduz (?:o )?prejuizo|queda (?:do|no) prejuizo"),
    (-2, "default or insolvency", r"bankrupt\w*|insolven\w*|liquidation|(?:payment |debt |bond )?default(?:s|ed)?|missed (?:debt |bond |interest )?payments?|fails? to (?:repay|pay creditors)|falencia|inadimplencia|vo no|pha san|iflas"),
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


def sentiment(value):
    """Preserve direction and missing values across historical scales."""
    return None if value is None else (value > 0) - (value < 0)


def sentiment_assessment(assessment):
    """Return current-scale source evidence without altering an archived assessment."""
    result = dict(assessment)
    if "score" in result:
        result["score"] = sentiment(result["score"])
    if "evidence" in result:
        result["evidence"] = [dict(item, score=sentiment(item["score"]))
                              for item in result["evidence"]]
    return result


def week_start(value: datetime) -> object:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    date = value.astimezone(WEEK_TIMEZONE).date()
    return date - timedelta(days=date.weekday())


def input_hash(article: dict, subject: dict, now: datetime | None = None) -> str:
    values = {key: article.get(key) for key in
              ("title", "title_en", "publisher", "publisher_en", "language", "published_at", "source_kind", "discovery_service", "verification")}
    if quote := (article.get("event_draft") or {}).get("profit_decline_quote"):
        values["profit_decline_quote"] = quote
    values["draft_sentiment"] = (article.get("event_draft") or {}).get("sentiment")
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
    verification = sentiment_assessment(article.get("verification") or {})
    if verification.get("status") == "supported":
        result.update(score=verification["score"], score_auto=verification["score"],
                      score_evidence=verification["evidence"], score_status="provisional",
                      score_included=True, score_reason=verification["reason"])
    else:
        direction = (article.get("event_draft") or {}).get("sentiment")
        if direction in (-1, 0, 1):
            result.update(score=direction, score_auto=direction, score_reason="AI source triage; not verified")
        result.update(score_included=False, score_status="needs_review",
                      score_reason=result["score_reason"] + "; " + verification.get("reason", "Full-article verification pending"))
    if (article.get("event_draft") or {}).get("profit_decline_quote") and verification.get("status") == "supported" and verification["score"] >= 0:
        result.update(score=-1, score_auto=-1, score_included=False, score_status="needs_review",
                      score_reason="Reported profit decline in mixed results; confirm adverse evidence before weekly inclusion")
    published = article.get("published_at")
    if published and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if not published or published > now:
        result.update(score_included=False, score_status="needs_review",
                      score_reason=result["score_reason"] + "; missing or future publication date")
    return sentiment_assessment(dict(result, score_auto=sentiment(result["score_auto"]),
                                     score_evidence=sentiment_assessment({"evidence": result["score_evidence"]})["evidence"]))


def aggregate_week(articles: list[dict]) -> dict:
    """Aggregate eligible event scores, retaining source IDs for audit and drill-down."""
    from .events import event_assessment
    by_event = defaultdict(list)
    for row in articles:
        by_event[row.get("event_group") or row["url"]].append(row)
    contributions, groups, included, excluded, duplicates = [], [], [], [], []
    for key, members in sorted(by_event.items()):
        result = event_assessment(members, saved=True)
        first = min(result["sources"] or members, key=lambda a: a["id"])
        ids = sorted(a["id"] for a in members)
        groups.append({"id": key, "representative_id": first["id"], "article_ids": ids})
        duplicates.extend(result["duplicate_ids"])
        used = {a["id"] for a in result["sources"]}
        excluded.extend(i for i in ids if i not in used and i not in result["duplicate_ids"])
        included.extend(used)
        if result["score"] is not None:
            draft = first.get("event_draft") or {}
            contributions.append({"id": first["id"], "event_group": key, "url": first["url"],
                "title": draft.get("title") or first.get("title_en") or first["title"],
                "score": result["score"], "reason": result["reason"], "source_count": len(used),
                "article_ids": sorted(used), "version": VERSION,
                "status": "reviewed" if any(a.get("score_status") == "reviewed" for a in result["sources"]) else "provisional"})
    values = [e["score"] for e in contributions]
    mean = Decimal(sum(values)) / len(values) if values else None
    return {
        "score": int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if values else None,
        "mean_score": float(mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if values else None,
        "event_count": len(values), "duplicate_count": len(duplicates), "duplicate_ids": sorted(duplicates),
        "groups": groups, "article_count": len(included), "excluded_count": len(excluded),
        "negative_count": values.count(-1), "neutral_count": values.count(0), "positive_count": values.count(1),
        "worst_score": min(values, default=None), "best_score": max(values, default=None),
        "reviewed_count": sum(e["status"] == "reviewed" for e in contributions),
        "article_ids": sorted(included), "excluded_ids": sorted(excluded), "contributions": contributions,
        "version": VERSION, "timezone": str(WEEK_TIMEZONE),
    }


def weekly_groups(articles: list[dict]) -> dict:
    """One event belongs to its first eligible publication week, even across searches."""
    from .events import event_assessment
    by_event, groups = defaultdict(list), defaultdict(list)
    for article in articles:
        if article.get("company_id"):
            by_event[article["company_id"], article.get("event_group") or article["url"]].append(article)
    for (company, _), members in by_event.items():
        sources = event_assessment(members, saved=True)["sources"]
        date = min(a.get("published_at") or a["created_at"] for a in sources or members)
        groups[company, week_start(date)].extend(members)
    return groups
