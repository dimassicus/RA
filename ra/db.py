"""Small PostgreSQL data layer for the local pilot."""

from __future__ import annotations

import os
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("RA_DATABASE_URL", "postgresql://localhost/RA")
FIELDS = (
    "name", "short_name", "country_registration", "registration_number", "key_owners",
    "top_management", "primary_countries", "affiliates", "primary_industries",
    "legal_address", "comment",
)


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_schema():
    with connect() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


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


def get_identity_evidence(company_id: int, registration_number: str, country: str = ""):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM company_identity_evidence WHERE company_id = %s ORDER BY checked_at DESC",
            (company_id,),
        ).fetchall()
    return [row for row in rows if _ids_overlap({row["identifier_value"]},
                                                identifier_tokens(registration_number, country))]


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


def find_company_match(data: dict):
    country = data.get("country_registration", "").strip().casefold()
    names = {_name_key(data.get("name", "")), _name_key(data.get("short_name", ""))} - {""}
    ids = identifier_tokens(data.get("registration_number", ""), country)
    local = [company for company in list_companies(None)
             if company["country_registration"].strip().casefold() == country]
    for company in local:
        if _ids_overlap(ids, identifier_tokens(company["registration_number"], country)):
            return company
    for company in local:
        known = {_name_key(company["name"]), _name_key(company.get("short_name", ""))} - {""}
        known_ids = identifier_tokens(company["registration_number"], country)
        if names & known and not (ids and known_ids and not _ids_overlap(ids, known_ids)):
            return company
    return None


def stage_company(data: dict, source: str, workbook_row: int | None = None) -> int | None:
    """Store a draft only. Reimporting the same row is idempotent."""
    from psycopg.types.json import Jsonb

    clean = {field: str(data.get(field, "")).strip() for field in FIELDS}
    if not clean["name"] or not clean["country_registration"]:
        raise ValueError("Company name and country of registration are required")
    digest = hashlib.sha256(json.dumps([source, clean], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    match = find_company_match(clean)
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO company_intake (source, source_key, workbook_row, draft_data, matched_company_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source_key) DO NOTHING RETURNING id",
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
                    identifier_tokens(data["registration_number"], data["country_registration"])):
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


def create_run(scope: str, subjects: list[dict]):
    from psycopg.types.json import Jsonb

    with connect() as conn:
        return conn.execute(
            "INSERT INTO search_runs (scope, subjects, total_companies) "
            "VALUES (%s, %s, %s) RETURNING id",
            (scope, Jsonb(subjects), len(subjects)),
        ).fetchone()["id"]


def get_run(run_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM search_runs WHERE id = %s", (run_id,)).fetchone()


def list_runs(limit: int = 12):
    with connect() as conn:
        return conn.execute(
            "SELECT id, scope, status, total_companies, completed_companies, "
            "result_count, error_count, created_at FROM search_runs "
            "ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()


def get_results(run_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM search_results WHERE run_id = %s "
            "ORDER BY published_at DESC NULLS LAST, id DESC", (run_id,)
        ).fetchall()


def set_report_selection(run_id: int, selected_ids: list[int], edits: dict[int, tuple[str, str]] | None = None) -> int:
    """Replace the selected lead set for one completed run atomically."""
    with connect() as conn:
        run = conn.execute("SELECT status FROM search_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if run is None or run["status"] in ("queued", "running"):
            raise ValueError("The search must finish before selecting report leads")
        current = {row["id"]: row for row in conn.execute(
            "SELECT id, title_en, publisher_en FROM search_results WHERE run_id = %s", (run_id,)
        )}
        valid = set(current)
        if not set(selected_ids) <= valid:
            raise ValueError("A selected lead does not belong to this run")
        for result_id, (title_en, publisher_en) in (edits or {}).items():
            if result_id not in valid:
                raise ValueError("A translation edit does not belong to this run")
            if len(title_en) > 1000 or len(publisher_en) > 250:
                raise ValueError("English text is too long")
            if (title_en, publisher_en) != (current[result_id]["title_en"], current[result_id]["publisher_en"]):
                conn.execute(
                    "UPDATE search_results SET title_en = %s, publisher_en = %s, "
                    "translation_provider = 'analyst edited' WHERE id = %s",
                    (title_en, publisher_en, result_id),
                )
        return conn.execute(
            "UPDATE search_results SET report_selected = (id = ANY(%s)) WHERE run_id = %s",
            (selected_ids, run_id),
        ).rowcount


def get_errors(run_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM search_errors WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()


def company_snapshot(company: dict):
    return {field: company.get(field, "") for field in FIELDS} | {"id": company.get("id")}
