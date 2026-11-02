"""Read the user's full 11-column watchlist into reviewable intake records."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from . import db


HEADER_MAP = {
    "company name (full)": "name",
    "company name": "name",
    "company name (short)": "short_name",
    "country of registration": "country_registration",
    "registration number/tax id": "registration_number",
    "registration number": "registration_number",
    "key owners": "key_owners",
    "top management": "top_management",
    "primary countries/regions of presence": "primary_countries",
    "primary countries of presence": "primary_countries",
    "affiliated companies or alias or brandname company is known for": "affiliates",
    "affiliated companies": "affiliates",
    "primary industries": "primary_industries",
    "legal address": "legal_address",
    "comment": "comment",
}


def read_workbook(path: str | Path) -> list[tuple[int, dict]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        headers = next(rows, ())
        columns = {HEADER_MAP[str(value).strip().casefold()]: index
                   for index, value in enumerate(headers)
                   if value is not None and str(value).strip().casefold() in HEADER_MAP}
        if "name" not in columns or "country_registration" not in columns:
            raise ValueError("Workbook needs Company name (full) and Country of registration columns")
        output = []
        for row_number, row in enumerate(rows, 2):
            data = {
                field: str(row[index]).strip() if index < len(row) and row[index] is not None else ""
                for field, index in columns.items()
            }
            if not any(data.values()):
                continue
            if not data.get("name") or not data.get("country_registration"):
                raise ValueError(f"Workbook row {row_number} lacks a company name or country")
            output.append((row_number, {field: data.get(field, "") for field in db.FIELDS}))
        return output
    finally:
        workbook.close()


def stage_workbook(path: str | Path) -> tuple[int, int]:
    staged = skipped = 0
    for row_number, data in read_workbook(path):
        if db.stage_company(data, source="workbook", workbook_row=row_number):
            staged += 1
        else:
            skipped += 1
    return staged, skipped


if __name__ == "__main__":
    db.init_schema()
    print("Staged, already staged:", stage_workbook("companies.xlsx"))
