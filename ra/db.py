"""Small PostgreSQL data layer for the local pilot."""

from __future__ import annotations

import os
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .identifiers import split_legacy_identifier


DATABASE_URL = os.environ.get("RA_DATABASE_URL", "postgresql://localhost/RA")
FIELDS = (
    "name", "short_name", "country_registration", "registration_number", "tax_id", "key_owners",
    "top_management", "primary_countries", "affiliates", "primary_industries",
    "legal_address", "comment", "aliases", "alias_notes",
)


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def reserve_news_api_request(provider: str, daily_limit: int | None) -> bool:
    """Atomically reserve one request against the provider's UTC-day budget."""
    if daily_limit is not None and daily_limit < 1:
        return False
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO news_api_usage (provider, usage_date, request_count) "
            "VALUES (%s, (now() AT TIME ZONE 'UTC')::date, 1) "
            "ON CONFLICT (provider, usage_date) DO UPDATE "
            "SET request_count = news_api_usage.request_count + 1 "
            "WHERE %s::integer IS NULL OR news_api_usage.request_count < %s RETURNING request_count",
            (provider, daily_limit, daily_limit),
        ).fetchone()
    return row is not None


def remaining_news_api_requests(limits):
    with connect() as conn:
        used = {row["provider"]: row["request_count"] for row in conn.execute(
            "SELECT provider,request_count FROM news_api_usage WHERE usage_date=(now() AT TIME ZONE 'UTC')::date"
        ).fetchall()}
    return {provider: max(0, limit - used.get(provider, 0)) for provider, limit in limits.items()}


def reserve_firecrawl_credit():
    """One basic Markdown scrape costs at most one credit. Include earlier daily reservations."""
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ra.firecrawl.monthly'))")
        row = conn.execute("""
            WITH period AS (SELECT (now() AT TIME ZONE 'UTC')::date AS today)
            INSERT INTO news_api_usage (provider, usage_date, request_count)
            SELECT 'firecrawl', today, 1 FROM period
            WHERE (SELECT COALESCE(sum(request_count), 0) FROM news_api_usage
                   WHERE provider = 'firecrawl' AND usage_date BETWEEN date_trunc('month', today)::date AND today) < 1000
            ON CONFLICT (provider, usage_date) DO UPDATE SET request_count = news_api_usage.request_count + 1
            RETURNING usage_date
        """).fetchone()
    return row["usage_date"] if row else None


def release_firecrawl_credit(day):
    with connect() as conn:
        conn.execute("UPDATE news_api_usage SET request_count = request_count - 1 "
                     "WHERE provider = 'firecrawl' AND usage_date = %s AND request_count > 0", (day,))


def init_schema():
    with connect() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())
        from .keywords import seed
        seed(conn)
        from . import risk_factors
        risk_factors.seed(conn)
        # Existing rows used registration_number as a combined identifier field.
        for row in conn.execute(
            "SELECT id, country_registration, registration_number FROM companies "
            "WHERE tax_id = '' AND registration_number <> ''"
        ).fetchall():
            registration, tax_id = split_legacy_identifier(row["registration_number"], row["country_registration"])
            if tax_id:
                conn.execute("UPDATE companies SET registration_number = %s, tax_id = %s WHERE id = %s",
                             (registration, tax_id, row["id"]))
        for row in conn.execute("SELECT id, draft_data, findings FROM company_intake").fetchall():
            data = row["draft_data"]
            if "tax_id" not in data:
                registration, tax_id = split_legacy_identifier(
                    data.get("registration_number", ""), data.get("country_registration", ""))
                data["registration_number"], data["tax_id"] = registration, tax_id
                conn.execute("UPDATE company_intake SET draft_data = %s WHERE id = %s", (Jsonb(data), row["id"]))
            findings = row["findings"]
            changed = False
            for candidate in findings.get("possible_duplicates", []):
                if "tax_id" not in candidate:
                    registration, tax_id = split_legacy_identifier(
                        candidate.get("registration_number", ""), data.get("country_registration", ""))
                    candidate["registration_number"], candidate["tax_id"] = registration, tax_id
                    changed = True
            if changed:
                conn.execute("UPDATE company_intake SET findings = %s WHERE id = %s",
                             (Jsonb(findings), row["id"]))
        # When the original combined-column workbook is still available,
        # translate its staged hashes so the split-column copy imports cleanly.
        workbook_path = Path(__file__).resolve().parent.parent / "companies.xlsx"
        if workbook_path.exists():
            from .importer import read_workbook
            try:
                workbook_rows = read_workbook(workbook_path)
            except (OSError, ValueError):
                workbook_rows = []  # Import errors belong on the dashboard, not app startup.
            for _, imported in workbook_rows:
                legacy = imported.get("_legacy_identifier")
                if not legacy:
                    continue
                current = {field: str(imported.get(field, "")).strip() for field in FIELDS}
                old = {field: value for field, value in current.items() if field not in ("tax_id", "aliases", "alias_notes")}
                old["registration_number"] = legacy
                old_key = hashlib.sha256(json.dumps(["workbook", old], sort_keys=True,
                                                    ensure_ascii=False).encode()).hexdigest()
                new_key = hashlib.sha256(json.dumps(["workbook", intake_hash_data(current)], sort_keys=True,
                                                    ensure_ascii=False).encode()).hexdigest()
                conn.execute("UPDATE company_intake SET source_key = %s "
                             "WHERE source_key = %s AND source = 'workbook'", (new_key, old_key))


def list_companies(active: bool | None = True):
    with connect() as conn:
        if active is None:
            return conn.execute("SELECT * FROM companies ORDER BY active DESC, lower(name), id").fetchall()
        return conn.execute(
            "SELECT * FROM companies WHERE active = %s ORDER BY lower(name), id", (active,)
        ).fetchall()


def get_company(company_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM companies WHERE id = %s", (company_id,)).fetchone()


def get_identity_evidence(company_id: int, registration_number: str, tax_id: str = "", country: str = ""):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM company_identity_evidence WHERE company_id = %s ORDER BY checked_at DESC",
            (company_id,),
        ).fetchall()
    supplied = identifier_tokens(registration_number, country) | identifier_tokens(tax_id, country)
    return [row for row in rows if _ids_overlap(identifier_tokens(row["identifier_value"], country), supplied)]


def find_company(name: str, country: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM companies WHERE lower(btrim(name)) = lower(btrim(%s)) "
            "AND lower(btrim(country_registration)) = lower(btrim(%s)) ORDER BY id LIMIT 1",
            (name, country),
        ).fetchone()


def identifier_tokens(value: str, country: str) -> set[str]:
    """Compare whole identifier components; preserve leading zeroes."""
    parts = re.split(r"\s+/\s+", value.strip())
    if country.casefold() not in ("brazil", "brasil"):
        parts = [part for item in parts for part in item.split("/")]
    tokens = {"".join(c for c in unicodedata.normalize("NFKC", part).upper() if c.isalnum())
              for part in parts}
    return {token for token in tokens if len(token) >= 5}


def _name_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def _ids_overlap(left: set[str], right: set[str]) -> bool:
    return any(a == b or (a.isdigit() and b.isdigit() and a.lstrip("0") == b.lstrip("0"))
               for a in left for b in right)


def _company_ids_match(left: dict, right: dict, country: str) -> bool:
    return any(_ids_overlap(identifier_tokens(left.get(field, ""), country),
                            identifier_tokens(right.get(field, ""), country))
               for field in ("registration_number", "tax_id"))


def _company_ids_conflict(left: dict, right: dict, country: str) -> bool:
    return any(bool(a and b and not _ids_overlap(a, b)) for field in ("registration_number", "tax_id")
               for a, b in [(identifier_tokens(left.get(field, ""), country),
                             identifier_tokens(right.get(field, ""), country))])


def find_company_match(data: dict):
    country = data.get("country_registration", "").strip().casefold()
    names = {_name_key(data.get("name", "")), _name_key(data.get("short_name", ""))} - {""}
    local = [company for company in list_companies(None)
             if company["country_registration"].strip().casefold() == country]
    for company in local:
        if _company_ids_match(data, company, country) and not _company_ids_conflict(data, company, country):
            return company
    for company in local:
        known = {_name_key(company["name"]), _name_key(company.get("short_name", ""))} - {""}
        supplied = any(identifier_tokens(data.get(field, ""), country)
                       for field in ("registration_number", "tax_id"))
        recorded = any(identifier_tokens(company.get(field, ""), country)
                       for field in ("registration_number", "tax_id"))
        if (names & known and not _company_ids_conflict(data, company, country)
                and (not supplied or not recorded or _company_ids_match(data, company, country))):
            return company
    return None


def intake_hash_data(data: dict) -> dict:
    # Preserve pre-alias workbook fingerprints when the new fields are empty.
    return {key: value for key, value in data.items()
            if key not in ("aliases", "alias_notes") or value}


def stage_company(data: dict, source: str, workbook_row: int | None = None) -> int | None:
    """Store a draft only. Reimporting the same row is idempotent."""
    from psycopg.types.json import Jsonb

    clean = {field: str(data.get(field, "")).strip() for field in FIELDS}
    if not clean["name"] or not clean["country_registration"]:
        raise ValueError("Company name and country of registration are required")
    digest = hashlib.sha256(json.dumps([source, intake_hash_data(clean)], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Recognize drafts staged from the old combined-ID workbook after migration.
    old_digest = None
    if data.get("_legacy_identifier"):
        old_clean = {key: value for key, value in clean.items() if key not in ("tax_id", "aliases", "alias_notes")}
        old_clean["registration_number"] = data["_legacy_identifier"]
        old_digest = hashlib.sha256(json.dumps([source, old_clean], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    match = find_company_match(clean)
    with connect() as conn:
        if old_digest and conn.execute(
            "SELECT 1 FROM company_intake WHERE source_key = %s AND status <> 'dismissed'", (old_digest,)
        ).fetchone():
            return None
        row = conn.execute(
            "INSERT INTO company_intake (source, source_key, workbook_row, draft_data, matched_company_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source_key) DO UPDATE SET "
            "status = 'queued', findings = '{}'::jsonb, research_error = '', "
            "matched_company_id = EXCLUDED.matched_company_id, updated_at = now() "
            "WHERE company_intake.status = 'dismissed' RETURNING id",
            (source, digest, workbook_row, Jsonb(clean), match["id"] if match else None),
        ).fetchone()
    return row["id"] if row else None


def list_intake(statuses=("queued", "researching", "ready", "error")):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM company_intake WHERE status = ANY(%s) ORDER BY id DESC", (list(statuses),)
        ).fetchall()


def get_intake(intake_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM company_intake WHERE id = %s", (intake_id,)).fetchone()


def claim_intake(intake_id: int) -> bool:
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE company_intake SET status = 'researching', research_error = '', updated_at = now() "
            "WHERE id = %s AND status IN ('queued', 'error') RETURNING id", (intake_id,)
        ).fetchone())


def reset_interrupted_intake():
    with connect() as conn:
        conn.execute("UPDATE company_intake SET status = 'queued', updated_at = now() WHERE status = 'researching'")


def finish_intake(intake_id: int, data: dict, findings: dict, error: str = ""):
    from psycopg.types.json import Jsonb

    with connect() as conn:
        conn.execute(
            "UPDATE company_intake SET draft_data = %s, findings = %s, status = %s, "
            "research_error = %s, updated_at = now() WHERE id = %s AND status = 'researching'",
            (Jsonb(data), Jsonb(findings), "error" if error else "ready", error[:2000], intake_id),
        )


def retry_intake(intake_id: int):
    with connect() as conn:
        return conn.execute(
            "UPDATE company_intake SET status = 'queued', updated_at = now() "
            "WHERE id = %s AND status IN ('ready', 'error') RETURNING id", (intake_id,)
        ).fetchone()


def dismiss_intake(intake_id: int):
    with connect() as conn:
        return conn.execute(
            "UPDATE company_intake SET status = 'dismissed', updated_at = now() "
            "WHERE id = %s AND status IN ('ready', 'error') RETURNING id", (intake_id,)
        ).fetchone()


def confirm_intake(intake_id: int, data: dict):
    """Commit an explicitly reviewed draft and any strong identifier evidence atomically."""
    from psycopg.types.json import Jsonb

    values = tuple(str(data.get(field, "")).strip() for field in FIELDS)
    with connect() as conn:
        draft = conn.execute("SELECT * FROM company_intake WHERE id = %s FOR UPDATE", (intake_id,)).fetchone()
        if not draft or draft["status"] not in ("ready", "error"):
            raise ValueError("This draft is not ready for confirmation")
        match = find_company_match(data)
        if draft["matched_company_id"] and (not match or match["id"] != draft["matched_company_id"]):
            raise ValueError("Identity changed; research this company again before saving")
        target_id = match["id"] if match else None
        if target_id:
            company_id = conn.execute(
                "UPDATE companies SET " + ", ".join(f"{field} = %s" for field in FIELDS) +
                ", active = TRUE, updated_at = now() WHERE id = %s RETURNING id", values + (target_id,)
            ).fetchone()["id"]
        else:
            company_id = conn.execute(
                "INSERT INTO companies (" + ", ".join(FIELDS) + ") VALUES (" +
                ", ".join(["%s"] * len(FIELDS)) + ") RETURNING id", values
            ).fetchone()["id"]
        for evidence in draft["findings"].get("identity_evidence", []):
            if evidence.get("verified") and _ids_overlap({evidence["identifier_value"]},
                    identifier_tokens(data["registration_number"], data["country_registration"]) |
                    identifier_tokens(data["tax_id"], data["country_registration"])):
                conn.execute(
                    "INSERT INTO company_identity_evidence "
                    "(company_id, identifier_type, identifier_value, source_name, source_url) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (company_id, evidence["identifier_type"], evidence["identifier_value"],
                     evidence["source_name"], evidence["source_url"]),
                )
        conn.execute(
            "UPDATE company_intake SET draft_data = %s, status = 'saved', company_id = %s, "
            "updated_at = now() WHERE id = %s", (Jsonb(dict(zip(FIELDS, values))), company_id, intake_id),
        )
        return company_id, bool(target_id)


def save_company(data: dict, company_id: int | None = None):
    values = tuple(data.get(field, "").strip() for field in FIELDS)
    with connect() as conn:
        if company_id is None:
            return conn.execute(
                "INSERT INTO companies (" + ", ".join(FIELDS) + ") VALUES (" +
                ", ".join(["%s"] * len(FIELDS)) + ") RETURNING id", values
            ).fetchone()["id"]
        return conn.execute(
            "UPDATE companies SET " + ", ".join(f"{field} = %s" for field in FIELDS) +
            ", updated_at = now() WHERE id = %s RETURNING id", values + (company_id,)
        ).fetchone()["id"]


def set_active(company_id: int, active: bool):
    with connect() as conn:
        return conn.execute(
            "UPDATE companies SET active = %s, updated_at = now() WHERE id = %s RETURNING id",
            (active, company_id),
        ).fetchone()


def create_run(scope: str, subjects: list[dict], *, window_start=None, window_end=None):
    from psycopg.types.json import Jsonb
    from . import search_settings

    if window_start is not None or window_end is not None:
        if (not isinstance(window_start, datetime) or not isinstance(window_end, datetime)
                or window_start.tzinfo is None or window_end.tzinfo is None
                or window_end <= window_start
                or window_end > datetime.now(timezone.utc)):
            raise ValueError("Choose an ordered, past publication window with timezone offsets.")
        if window_end - window_start > timedelta(days=7) and not (scope in ("selected", "one_off") and len(subjects) == 1):
            raise ValueError("Date ranges longer than seven days require a single-company search.")
    settings = search_settings.read()
    with connect() as conn:
        return conn.execute(
            "INSERT INTO search_runs (scope, subjects, total_companies, retry_translations, window_start, window_end, search_settings) "
            "VALUES (%s, %s, %s, TRUE, %s, %s, %s) RETURNING id",
            (scope, Jsonb(subjects), len(subjects), window_start, window_end, Jsonb(settings)),
        ).fetchone()["id"]


def get_run(run_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM search_runs WHERE id = %s", (run_id,)).fetchone()


def list_runs(limit: int = 12):
    with connect() as conn:
        return conn.execute(
            "SELECT id, scope, status, total_companies, completed_companies, "
            "result_count, error_count, created_at, event_analysis_status, "
            "search_settings ? 'processing_stopped_at' AS processing_stopped FROM search_runs "
            "ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()


def run_topic_check(run_id, search, editions, branch, topic, *, include_factors=False):
    from .exposure import check_topic
    from .news_topics import excluded_story
    with connect() as conn:
        claimed = conn.execute(
            "INSERT INTO exposure_checks (run_id,branch,topic) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING run_id", (run_id, branch, topic),
        ).fetchone()
        if claimed:
            hits, error = check_topic(branch, topic, search, editions.get((branch, topic), ()),
                                      include_factors=include_factors)
            # Preserve publication instants for reinsertion after a worker restart.
            stored = [({**hit, "published_at": hit["published_at"].isoformat() if hit["published_at"] else None}, channel)
                      for hit, channel in hits]
            conn.execute("UPDATE exposure_checks SET hits=%s,error=%s WHERE run_id=%s AND branch=%s AND topic=%s",
                         (Jsonb(stored), error, run_id, branch, topic))
            return hits, error
        row = conn.execute("SELECT hits,error FROM exposure_checks WHERE run_id=%s AND branch=%s AND topic=%s",
                           (run_id, branch, topic)).fetchone()
        # A restarted run may have cached its topic hits before discovery exclusions were added.
        return [(hit, channel) for hit, channel in row["hits"]
                if not excluded_story(hit["title"], hit["url"], publisher=hit.get("publisher", ""))], row["error"]


def exposure_coverage(run_id):
    with connect() as conn:
        return conn.execute("SELECT branch,topic,jsonb_array_length(hits) AS leads,error FROM exposure_checks "
                            "WHERE run_id=%s ORDER BY branch,topic", (run_id,)).fetchall()


def mark_article_copies(conn, rows, *, snapshots=None):
    by_id = {row['id']: row for row in rows}
    if snapshots is None:
        companies = list({r['company_id'] for r in rows if r.get('company_id')})
        snapshots = [r['metrics'] for r in conn.execute(
            "SELECT metrics FROM company_weekly_scores WHERE company_id=ANY(%s) AND metrics ? 'copy_of'",
            (companies,))]
    for metrics in snapshots:
        counted = set(metrics['article_ids'])
        for other, representative in metrics.get('copy_of', {}).items():
            for lead in (int(other), representative):
                if lead in by_id:
                    by_id[lead].update(copy_lead=representative, copy_counted=representative in counted)


def get_results(run_id: int):
    from .exposure import scope_sector_paths
    with connect() as conn:
        run = conn.execute("SELECT subjects FROM search_runs WHERE id=%s", (run_id,)).fetchone()
        subjects = {str(s.get("id") or f"one-off:{run_id}"): s for s in run["subjects"]} if run else {}
        rows = conn.execute(
            "SELECT * FROM search_results WHERE run_id = %s "
            "ORDER BY published_at DESC NULLS LAST, id DESC", (run_id,)
        ).fetchall()
        mark_article_copies(conn, rows)
        return [scope_sector_paths(row, subjects.get(row["subject_key"], {})) for row in rows]


def set_report_selection(run_id: int, selected_ids: list[int], edits: dict[int, tuple[str, str]] | None = None,
                         scores: dict[int, int] | None = None, *, save_all=False, originally_selected=None) -> int:
    """Review translations without DB locks, then save the selection atomically."""
    from . import headline_review, score_store
    with connect() as conn:
        run = conn.execute("SELECT status FROM search_runs WHERE id = %s", (run_id,)).fetchone()
        if run is None or run["status"] in ("queued", "running"):
            raise ValueError("The search must finish before selecting report leads")
        current = {row["id"]: row for row in conn.execute(
            "SELECT id, title, language, title_en, publisher_en FROM search_results WHERE run_id = %s", (run_id,)
        )}
    if save_all:
        selected_ids = list(current)
    if (set(selected_ids) | set(scores or {}) | set(originally_selected or ())) - current.keys():
        raise ValueError("A selected lead does not belong to this run")
    for result_id, (title_en, publisher_en) in (edits or {}).items():
        if result_id not in current:
            raise ValueError("A translation edit does not belong to this run")
        if len(title_en) > 1000 or len(publisher_en) > 250:
            raise ValueError("English text is too long")
    prepared = {}
    for result_id, (title_en, publisher_en) in (edits or {}).items():
        original = current[result_id]
        if (title_en, publisher_en) != (original["title_en"], original["publisher_en"]):
            changed = title_en != original["title_en"]
            qa = headline_review.audit(original["title"], original["language"], title_en) if changed else {}
            prepared[result_id] = (changed, qa, title_en, publisher_en)
    with connect() as conn:
        # Source verification takes this lock before touching article rows too.
        score_store._lock(conn)
        run = conn.execute("SELECT status FROM search_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if run is None or run["status"] in ("queued", "running"):
            raise ValueError("The search must finish before selecting report leads")
        if originally_selected is not None and not save_all and not prepared and not scores and set(selected_ids) == originally_selected:
            return conn.execute("SELECT count(*) AS n FROM search_results WHERE run_id=%s AND report_selected",
                                (run_id,)).fetchone()["n"]
        for result_id in prepared:
            latest = conn.execute(
                "SELECT id, title, language, title_en, publisher_en FROM search_results WHERE id=%s FOR UPDATE",
                (result_id,),
            ).fetchone()
            if latest != current[result_id]:
                raise ValueError("The translation changed during review; reload before saving")
        for result_id, (changed, qa, title_en, publisher_en) in prepared.items():
            if changed:
                from .bilingual import enqueue
                enqueue(conn, title_en)
            conn.execute(
                "UPDATE search_results SET headline_review = CASE WHEN %s THEN %s ELSE headline_review END, title_en = %s, publisher_en = %s, "
                "translation_provider = 'analyst edited' WHERE id = %s",
                (changed, Jsonb(qa), title_en, publisher_en, result_id),
            )
        if originally_selected is None or save_all:
            conn.execute("UPDATE search_results SET report_selected=(%s OR id=ANY(%s)) WHERE run_id=%s",
                         (save_all, selected_ids, run_id))
        else:
            changed_ids = list(set(selected_ids).symmetric_difference(originally_selected))
            conn.execute("UPDATE search_results SET report_selected=(id=ANY(%s)) WHERE run_id=%s AND id=ANY(%s)",
                         (selected_ids, run_id, changed_ids))
        score_store._assess(conn, run_id)
        for rid, value in (scores or {}).items():
            article = conn.execute("SELECT * FROM search_results WHERE id=%s AND run_id=%s FOR UPDATE", (rid, run_id)).fetchone()
            dated = article["published_at"] and article["published_at"] <= datetime.now(timezone.utc)
            supported = not article["exposure_paths"] or article["verification"].get("status") == "supported"
            score_store._review(conn, article, value, bool(dated and supported),
                                "Analyst confirmed article sentiment on the run report")
        conn.execute("UPDATE search_runs SET selection_saved=TRUE WHERE id=%s", (run_id,))
        score_store._weekly(conn)
        return conn.execute("SELECT count(*) AS n FROM search_results WHERE run_id=%s AND report_selected",
                            (run_id,)).fetchone()["n"]



def get_errors(run_id: int):
    from .rss_recovery import coverage
    with connect() as conn:
        errors = conn.execute("SELECT * FROM search_errors WHERE run_id=%s AND NOT rss_imported ORDER BY id",
                              (run_id,)).fetchall()
        gaps = coverage(conn,run_id)
        if gaps:
            from . import recovery_providers
            waiting_for_budget = recovery_providers.configured('Brave') and not remaining_news_api_requests(
                {'brave': recovery_providers.daily_limit('Brave')})['brave']
            run = conn.execute("SELECT subjects, search_settings FROM search_runs WHERE id=%s",(run_id,)).fetchone()
            names = {str(s.get("id") or f"one-off:{run_id}"): s["name"] for s in run["subjects"]}
            closed = bool(run['search_settings'].get('processing_stopped_at'))
            for gap in gaps:
                state = "stopped by operator" if closed else "awaiting retry"
                message = (f"Google News RSS · {gap['total']} deferred searches, {gap['recovered']} recovered, "
                           f"{gap['total']-gap['recovered']-gap['uncertain']} {state}.")
                if gap['uncertain']:
                    message += f" Original edition could not be confirmed for {gap['uncertain']} legacy searches."
                if gap['brave']:
                    message += f" Brave Search: {gap['brave']}."
                elif not closed and waiting_for_budget and gap['recovered'] < gap['total']:
                    message += " Brave Search is waiting for daily quota."
                errors.append(dict(subject_name=names[gap['subject_key']], message=message, created_at=gap['first_seen'],
                                   resolved=gap['recovered'] == gap['total']))
        return errors


def subject_for(article, subjects):
    return next((s for s in subjects if s.get('id') == article['company_id'] and article['company_id']), None) or next(
        (s for s in subjects if s['name'] == article['subject_name']), {'name': article['subject_name']})


def evidence_subject(article, run):
    """Supplement a frozen search profile only with aliases documented during source recovery."""
    subject = subject_for(article, run["subjects"])
    aliases = (run.get("search_settings") or {}).get("evidence_aliases", {}).get(str(subject.get("id")), [])
    if not aliases:
        return subject
    return dict(subject, aliases="\n".join([subject.get("aliases", ""), *(a["name"] for a in aliases)]))


def company_snapshot(company: dict):
    return {field: company.get(field, "") for field in FIELDS} | {
        "id": company.get("id"), "disclosure_sources": company.get("disclosure_sources", {})}
