"""Preview/apply the sourced November 2026 alias review; never runs news searches.

Usage: .venv/bin/python scripts/review_company_aliases.py [--apply]
Back up PostgreSQL first. Existing aliases/notes are preserved and merged.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ra import db
from ra.aliases import parse_aliases, validate_aliases

ROOT = Path(__file__).resolve().parents[1]


def planned_changes(company, review):
    changes = {}
    for field, (expected, target) in review["changes"].items():
        if field not in ("name", "short_name", "tax_id"):
            raise ValueError("Unsupported review field")
        if company[field] not in (expected, target):
            raise ValueError(f"{company['id']}: {field} changed since review; inspect before applying")
        if company[field] != target:
            changes[field] = target
    aliases = validate_aliases("\n".join([*parse_aliases(company.get("aliases", "")), *review["aliases"]]))
    if aliases != company.get("aliases", ""):
        changes["aliases"] = aliases
    note = review["note"] + "\n" + "\n".join(review["sources"])
    previous = company.get("alias_notes", "")
    if note not in previous:
        changes["alias_notes"] = (previous + "\n\n" + note).strip()
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reviews = json.loads((ROOT / "sources/company-alias-review.json").read_text())
    # Migration only on explicit apply; preview works against the old schema.
    if args.apply:
        db.init_schema()
    plan = []
    with db.connect() as conn:
        for review in reviews:
            companies = conn.execute("SELECT * FROM companies WHERE country_registration=%s AND registration_number=%s FOR UPDATE",
                                     (review["country"], review["registration_number"])).fetchall()
            if len(companies) != 1:
                raise ValueError(f"Expected one matching company: {review['expected_name']}")
            company = companies[0]
            permitted_names = {review["expected_name"], review["changes"].get("name", [None, None])[1]}
            if company["name"] not in permitted_names:
                raise ValueError("Company name changed since review")
            changes = planned_changes(company, review)
            if changes:
                plan.append({"id": company["id"], "name": company["name"],
                             "before": {field: company.get(field, "") for field in changes}, "after": changes})
        if args.apply and plan:
            # Persist exact undo values before committing any company updates.
            folder = ROOT / ".local/before-aliases"
            folder.mkdir(parents=True, exist_ok=True)
            audit = folder / ("changes-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
            audit.touch(mode=0o600, exist_ok=False)
            audit.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
            for item in plan:
                changes = item["after"]
                conn.execute("UPDATE companies SET " + ", ".join(field + "=%s" for field in changes) +
                             ", updated_at=now() WHERE id=%s", (*changes.values(), item["id"]))
    print(json.dumps({"applied": args.apply, "companies_changed": len(plan), "changes": plan}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
