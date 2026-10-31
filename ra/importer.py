"""Import the user's watchlist workbook without discarding later app edits."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from . import db


HEADER_MAP = {
    "company name": "name",
    "country of registration": "country_registration",
    "registration number": "registration_number",
    "key owners": "key_owners",
    "top management": "top_management",
    "primary countries of presence": "primary_countries",
    "affiliated companies": "affiliates",
    "primary industries": "primary_industries",
    "comment": "comment",
}


def import_workbook(path: str | Path) -> tuple[int, int]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    headers = next(rows, ())
    columns = {HEADER_MAP[str(value).strip().casefold()]: index
               for index, value in enumerate(headers)
               if value is not None and str(value).strip().casefold() in HEADER_MAP}
    if "name" not in columns or "country_registration" not in columns:
        workbook.close()
        raise ValueError("Workbook needs Company name and Country of registration columns")
    added = skipped = 0
    try:
        for row in rows:
            data = {
                field: str(row[index]).strip() if index < len(row) and row[index] is not None else ""
                for field, index in columns.items()
            }
            if not data.get("name") and not data.get("country_registration"):
                continue
            if not data.get("name") or not data.get("country_registration"):
                raise ValueError(f"Workbook row {added + skipped + 2} lacks a company name or country")
            if db.find_company(data["name"], data["country_registration"]):
                skipped += 1
                continue
            db.save_company(data)
            added += 1
    finally:
        workbook.close()
    return added, skipped


if __name__ == "__main__":
    db.init_schema()
    print("Added, already present:", import_workbook("companies.xlsx"))
