"""Persistent defaults, snapshotted once for each search run."""
from . import db

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


def adverse_lead(article, subject):
    """Headline triage only; source checks and analyst review establish actual impact."""
    from .keywords import matches
    from .relevance import headline
    from .scoring import score_article
    if article.get("discovery_service") == "FCA Warning RSS":
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
