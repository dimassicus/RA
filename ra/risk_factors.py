"""Editable descriptions used for discovery. A matching search is only a lead."""
import copy
import hashlib
import json
import re
import threading
import unicodedata

from psycopg.types.json import Jsonb

from . import db, keywords

SCOPES = ("company", "sector", "country")
MAX_DESCRIPTION = 200
MANAGEMENT = "Changes in top management"
OWNERSHIP = "Significant changes in major shareholdings or control"
# Bump when the thematic prompt, schema or evidence validation changes.
ASSESSMENT_VERSION = "thematic-assessment-v2"
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
    return keywords.languages()


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


def validated_description(english, scope, previous):
    text = " ".join(english.split())
    if not 3 <= len(text) <= MAX_DESCRIPTION or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 %,.'’&()/<>+=:–—-]*", text):
        raise ValueError(f"Enter a short English risk description, up to {MAX_DESCRIPTION} characters.")
    if scope not in SCOPES:
        raise ValueError("Choose company, sector or country.")
    if text not in SEEDS and text != previous:
        keywords.validate_english(text, "short English description of a condition to monitor in financial news")
    return text


def save(row_id, english, scope, enabled, previous):
    text = validated_description(english, scope, previous if row_id else "")
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


def reviewed_translation(english, code, phrase, active_languages):
    phrase = " ".join(phrase.split())
    if not 3 <= len(english) <= MAX_DESCRIPTION:
        raise ValueError("Choose an existing risk factor.")
    if code not in active_languages or not 1 <= len(phrase) <= 500:
        raise ValueError("Choose an active language and enter a translation of up to 500 characters.")
    if keywords.percentage_values(english) != keywords.percentage_values(phrase):
        raise ValueError("Keep the percentages from the English description.")
    try:
        checked = keywords.review_translations({english: phrase}, code, True, {})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Translation review unavailable. Try again when local Qwen is ready.") from exc
    if checked.get(english) is not True:
        raise ValueError("Qwen could not confirm the translation. Check the wording, direction and thresholds.")
    return phrase


def save_translation(row_id, english, code, previous, phrase):
    if not row_id:
        raise ValueError("Choose an existing risk factor.")
    phrase = reviewed_translation(english, code, phrase, languages())
    with db.connect() as conn:
        updated = conn.execute("UPDATE risk_factors SET translations=translations || %s, search_cues=search_cues - %s::text "
                               "WHERE id=%s AND english=%s AND COALESCE(translations->>%s,'')=%s",
                               (Jsonb({code: phrase}), code, row_id, english, code, previous))
        if not updated.rowcount:
            raise ValueError("The factor or translation changed. Compare the current wording before saving again.")
    reload()


def save_all(form):
    edits = []
    active_languages = languages()
    for key in form.getlist("id"):
        if not key.isascii() or not key.isdecimal() or len(key) > 18:
            raise ValueError("Invalid risk factor ID.")
        prefix = key + "."
        previous = form[prefix + "previous"]
        try:
            english = validated_description(form[prefix + "english"], form[prefix + "scope"], previous)
            originals, translations = {}, {}
            for code in form.getlist(prefix + "language"):
                if code not in active_languages:
                    raise ValueError("Choose an active language.")
                originals[code] = form[prefix + code + ".original"]
                try:
                    translations[code] = reviewed_translation(english, code, form[prefix + code + ".translation"], active_languages)
                except ValueError as exc:
                    raise ValueError(f"{keywords.language_name(code)}: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"{previous}: {exc}") from exc
        if any(form[prefix + field] not in ("true", "false") for field in ("enabled", "previous_enabled")):
            raise ValueError("Invalid Active setting.")
        before = (previous, form[prefix + "previous_scope"], form[prefix + "previous_enabled"] == "true")
        after = (english, form[prefix + "scope"], form[prefix + "enabled"] == "true")
        edits.append((int(key), before, after, originals, translations))

    # Finish model calls before taking locks. Apply the entire batch or roll it back.
    with db.connect() as conn, conn.transaction():
        for row_id, before, after, originals, translations in sorted(edits, key=lambda edit: edit[0]):
            row = conn.execute("SELECT * FROM risk_factors WHERE id=%s FOR UPDATE", (row_id,)).fetchone()
            if (not row or (row["english"], row["scope"], row["enabled"]) != before
                    or any(row["translations"].get(code, "") != text for code, text in originals.items())):
                raise ValueError(f"{before[0]} changed since you opened the list. Compare the latest values before retrying.")
            same_english = before[0] == after[0]
            saved = row["translations"] if same_english else {}
            cues = row["search_cues"] if same_english else {}
            conn.execute("UPDATE risk_factors SET english=%s,scope=%s,enabled=%s,translations=%s,search_cues=%s WHERE id=%s",
                         (*after, Jsonb(saved | translations), Jsonb({k: v for k, v in cues.items() if k not in translations}), row_id))
    if edits:
        reload()
    return len(edits)


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


def cached_assess(article, document, subject, factors):
    """Reuse only complete assessments with the same source, target and protocol."""
    if not factors:
        return {}
    from .headline_review import QWEN
    from .relevance import headline
    available = document.get("status") == "available"
    body = document.get("body", "") if available else ""
    inputs = {
        "version": ASSESSMENT_VERSION, "model": QWEN, "reasoning": "off",
        "headline": headline(article, "title"),
        "source_available": available, "source_hash": hashlib.sha256(body.encode()).hexdigest(),
        "language": document.get("language", ""), "review_only": bool(document.get("review_only")),
        "target": {key: subject.get(key) for key in
                   ("name", "aliases", "country_registration", "primary_countries", "primary_industries")},
        "factors": [{key: row[key] for key in ("id", "english", "scope")} for row in factors],
    }
    fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    with db.connect() as conn:
        cached = conn.execute(
            "SELECT risk_factor_review FROM search_results "
            "WHERE risk_factor_review->>'input_hash'=%s AND risk_factor_review->>'status'='checked' "
            "ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
    if cached:
        return copy.deepcopy(cached["risk_factor_review"])
    result = assess(article, document, subject, factors)
    if result.get("status") == "checked":
        result = {**result, "input_hash": fingerprint, "version": ASSESSMENT_VERSION, "model": QWEN}
    return result


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
    seen = {}
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
                    existing = seen.get(row["id"])
                    if existing is None or (result["matches"][existing]["kind"] == "partial" and item["kind"] == "direct"):
                        match = {
                            "factor": row["english"], "scope": row["scope"], "kind": item["kind"],
                            "quote": quote, "from": "source text" if quote in passage else "headline",
                            "connection": connection[:500],
                        }
                        if existing is None:
                            seen[row["id"]] = len(result["matches"])
                            result["matches"].append(match)
                        else:
                            result["matches"][existing] = match
                if all(key in seen and result["matches"][seen[key]]["kind"] == "direct" for key in options):
                    break
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["status"] = "incomplete"
    return result


def notes(article):
    return [f'Possible theme: {m["factor"]}. {m["connection"]} ({m["from"]}: “{m["quote"]}”)'
            for m in article.get("risk_factor_review", {}).get("matches", [])]
