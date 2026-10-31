"""Small PostgreSQL data layer for the local pilot."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("RA_DATABASE_URL", "postgresql://localhost/RA")
FIELDS = (
    "name", "country_registration", "registration_number", "key_owners",
    "top_management", "primary_countries", "affiliates", "primary_industries",
    "comment",
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


def get_identity_evidence(company_id: int, registration_number: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM company_identity_evidence WHERE company_id = %s "
            "AND identifier_value = %s ORDER BY checked_at DESC",
            (company_id, registration_number),
        ).fetchall()


def find_company(name: str, country: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM companies WHERE lower(btrim(name)) = lower(btrim(%s)) "
            "AND lower(btrim(country_registration)) = lower(btrim(%s)) ORDER BY id LIMIT 1",
            (name, country),
        ).fetchone()


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
