"""Editable descriptions used for discovery. A matching search is only a lead."""
import re
import threading
import unicodedata

from psycopg.types.json import Jsonb

from . import db, keywords

SCOPES = ("company", "sector", "country")
MANAGEMENT = "Changes in top management"
OWNERSHIP = "Significant changes in major shareholdings or control"
SEEDS = {
    MANAGEMENT: "company",
    OWNERSHIP: "company",
    "Central bank tightens capital controls": "country",
    "Central bank restricts cross-border payments": "country",
    "New foreign currency restrictions": "country",
    "Currency devaluation (more than 10% in one week)": "country",
    "Local stock market decline (more than 5% in 1–3 trading days)": "country",
}
GOVERNANCE_GROUPS = {
    MANAGEMENT: ("executive role", "leadership action", "management change"),
    OWNERSHIP: ("ownership",),
}


def seed(conn):
    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO risk_factors(english,scope,seed_key) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                           [(text, scope, text) for text, scope in SEEDS.items()])


def languages():
    from .exposure import topic_editions
    from .search import LOCALES
    editions = topic_editions(db.list_companies(True))
    edition_codes = {LOCALES[place][0].split("-")[0] for places in editions.values() for place in places}
    return sorted((set(keywords.languages()) | edition_codes) - {"en"})


def records():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM risk_factors ORDER BY scope,english").fetchall()


_rows = [dict(english=text, scope=scope, enabled=True, translations={}) for text, scope in SEEDS.items()]
_lock = threading.Lock()


def reload():
    global _rows
    with _lock:
        _rows = records()


def active(scope):
    return [row for row in _rows if row["enabled"] and row["scope"] == scope]


def search_phrase(row, code):
    cues = row.get("search_cues", {}).get(code, [])
    if cues:
        return "(" + " OR ".join(f'"{cue}"' for cue in cues) + ")"
    text = row["english"] if code == "en" else row["translations"].get(code, "")
    # Thresholds qualify evidence. Requiring the exact cutoff in a headline would miss larger moves.
    text = re.sub(r"\s*[(（][^()（）]*[%％٪][^()（）]*[)）]\s*$", "", text)
    return " ".join(re.sub(r'[":()<>+=]', " ", unicodedata.normalize("NFC", text)).split())


def save(row_id, english, scope, enabled, previous):
    text = " ".join(english.split())
    if not 3 <= len(text) <= 180 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 %,.'’&()/<>+=:–—-]*", text):
        raise ValueError("Enter a short English risk description, up to 180 characters.")
    if scope not in SCOPES:
        raise ValueError("Choose company, sector or country.")
    if text not in SEEDS and not (row_id and text == previous):
        keywords.validate_english(text, "short English description of a condition to monitor in financial news")
    with db.connect() as conn:
        if row_id:
            updated = conn.execute("UPDATE risk_factors SET english=%s,scope=%s,enabled=%s,"
                                   "translations=CASE WHEN english=%s THEN translations ELSE '{}'::jsonb END, "
                                   "search_cues=CASE WHEN english=%s THEN search_cues ELSE '{}'::jsonb END "
                                   "WHERE id=%s AND english=%s", (text, scope, enabled, text, text, row_id, previous))
            if not updated.rowcount:
                raise ValueError("Risk factor changed. Refresh and try again.")
        else:
            conn.execute("INSERT INTO risk_factors(english,scope,enabled) VALUES (%s,%s,%s)", (text, scope, enabled))
    reload()


def save_translation(row_id, english, code, previous, phrase):
    phrase = " ".join(phrase.split())
    if not row_id or not 3 <= len(english) <= 180:
        raise ValueError("Choose an existing risk factor.")
    if code not in languages() or not 1 <= len(phrase) <= 500:
        raise ValueError("Choose an active language and enter a translation of up to 500 characters.")
    if keywords.percentage_values(english) != keywords.percentage_values(phrase):
        raise ValueError("Keep the percentages from the English description.")
    try:
        checked = keywords.review_translations({english: phrase}, code, True, {})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Translation review unavailable. Try again when local Qwen is ready.") from exc
    if checked.get(english) is not True:
        raise ValueError("Qwen could not confirm the translation. Check the wording, direction and thresholds.")
    with db.connect() as conn:
        updated = conn.execute("UPDATE risk_factors SET translations=translations || %s, search_cues=search_cues - %s::text "
                               "WHERE id=%s AND english=%s AND COALESCE(translations->>%s,'')=%s",
                               (Jsonb({code: phrase}), code, row_id, english, code, previous))
        if not updated.rowcount:
            raise ValueError("The factor or translation changed. Compare the current wording before saving again.")
    reload()


def expand_queries(code):
    """Use varied native news wording to retrieve candidates, independently of the theme assessment."""
    rows = [r for r in records() if r["enabled"] and code not in r["search_cues"]
            and r["english"] not in GOVERNANCE_GROUPS and (code == "en" or code in r["translations"])]
    for i in range(0, len(rows), 12):
        batch = rows[i:i + 12]
        inputs = {r["english"]: r["translations"].get(code, r["english"]) for r in batch}
        schema = {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}
        result = keywords.model_json(
            f"For each theme give 2 to 4 short alternative expressions a news headline in "
            f"{keywords.language_name(code)} might use. Include indirect mechanisms or consequences. "
            "Prefer specific two- or three-word phrases. Omit numbers, exact thresholds and search operators.",
            inputs, keywords.object_schema({name: schema for name in inputs}))
        with db.connect() as conn:
            for row in batch:
                phrases = result.get(row["english"])
                if not isinstance(phrases, list):
                    continue
                cues = list(dict.fromkeys(p.strip() for p in phrases if isinstance(p, str) and 1 < len(p.strip()) <= 80
                                         and not re.search(r'[":()<>\n\r]', p)))
                if 2 <= len(cues) <= 4:
                    conn.execute("UPDATE risk_factors SET search_cues=search_cues || %s WHERE id=%s AND english=%s "
                                 "AND translations->>%s IS NOT DISTINCT FROM %s",
                                 (Jsonb({code: cues}), row["id"], row["english"], code, row["translations"].get(code)))
        reload()


def assess(article, document, subject, factors):
    """Review original text by meaning, preserving evidence independently of the credit assessment."""
    if not factors:
        return {}
    from .relevance import headline
    title = headline(article, "title")
    body = document.get("body", "") if document.get("status") == "available" else ""
    result = {"status": "checked", "coverage": "headline and source text" if body else "headline only", "matches": []}
    target = {key: subject.get(key) for key in ("name", "aliases", "country_registration", "primary_countries", "primary_industries")}
    entry = keywords.object_schema({"id": {"type": "integer"}, "kind": {"type": "string", "enum": ["direct", "partial"]},
                                    "quote": {"type": "string"}, "connection": {"type": "string"}})
    schema = keywords.object_schema({"matches": {"type": "array", "items": entry}})
    prompt = ("Identify THEMATIC relevance of original news, including logical similarities. For every listed theme "
              "ask: does the source describe this idea, an instance, a precursor, or a weaker version? Treat the "
              "description as a theme, never a pass/fail rule. Mark partial when a relevant mechanism is present "
              "but size, timing, actor or scope is missing or different. A benchmark sliding 4% over two sessions "
              "IS a partial match for a >5% stock market decline. Say explicitly that 4% is below 5%. Importers "
              "needing permission to pay foreign suppliers can indicate tighter capital controls and external payment "
              "restrictions. If the source omits who ordered it, mark partial and say the authority is unspecified. "
              "Return all applicable themes even when they overlap. Mark direct only if the text reports all conditions. "
              "Do not invent who ordered a measure, causes, financial impact, figures or facts. Generic shared words, "
              "unrelated events and explicit denials do not match. Match company themes to the watched entity, country "
              "themes to a listed market, sector themes to its industry in a listed market. For each match give its "
              "ID, kind, a short continuous verbatim quote, and a short English explanation grounded in that quote. "
              "Return an empty array for unrelated news.")
    seen = set()
    try:
        for first in range(0, len(factors), 12):
            batch = factors[first:first + 12]
            options = {row["id"]: row for row in batch}
            themes = [{k: row[k] for k in ("id", "english", "scope")} for row in batch]
            # Overlap preserves evidence crossing a chunk boundary in long filings.
            for offset in range(0, max(1, len(body)), 6000):
                passage = body[max(0, offset - 300):offset + 6000]
                data = {"watchlist": target, "themes": themes, "headline": title, "source_text": passage}
                response = keywords.model_json(prompt, data, schema)
                for item in response["matches"]:
                    row = options.get(item["id"])
                    quote, connection = item["quote"].strip(), item["connection"].strip()
                    if (not row or item["kind"] not in ("partial", "direct") or not quote or len(quote) > 600
                            or not connection or (quote not in title and quote not in passage)):
                        result["status"] = "inconclusive"
                        continue
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        result["matches"].append({
                            "factor": row["english"], "scope": row["scope"], "kind": item["kind"],
                            "quote": quote, "from": "source text" if quote in passage else "headline",
                            "connection": connection[:500],
                        })
                if all(key in seen for key in options):
                    break
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["status"] = "incomplete"
    return result


def notes(article):
    return [f'Possible theme: {m["factor"]}. {m["connection"]} ({m["from"]}: “{m["quote"]}”)'
            for m in article.get("risk_factor_review", {}).get("matches", [])]
