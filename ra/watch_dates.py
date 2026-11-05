"""Point-in-time selection from the maintained register of issuer filings."""
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .aliases import normalized
from .scoring import WEEK_TIMEZONE

REGISTER = Path(__file__).resolve().parent.parent / "sources" / "watch-dates.json"
KINDS = {"Earnings release", "Debt maturity", "Covenant test"}


def identifier(value):
    return re.sub(r"[\W_]", "", value.casefold())


def upcoming(subject, as_of, records=None):
    if records is None:
        records = json.loads(REGISTER.read_text(encoding="utf-8"))
    country = normalized(subject.get("country_registration") or "")
    latest = {}
    for row in records:
        field = row["id_field"]
        if field not in ("tax_id", "registration_number") or normalized(row["country"]) != country:
            continue
        issuer_id = identifier(row["issuer_id"])
        if not issuer_id or issuer_id != identifier(subject.get(field) or ""):
            continue
        filed = datetime.fromisoformat(row["filed_at"])
        if filed.tzinfo is None or filed > as_of:
            continue
        key = row["event_key"]
        if key not in latest or filed > latest[key][0]:
            latest[key] = (filed, row)
    events = []
    for _, row in latest.values():
        url = urlsplit(row["source_url"])
        if (not row["event_date"] or row["kind"] not in KINDS or not row["evidence"].strip()
                or not row["filing"].strip() or url.scheme != "https" or not url.hostname):
            continue
        day = date.fromisoformat(row["event_date"])
        if day >= as_of.astimezone(WEEK_TIMEZONE).date():
            events.append({**row, "day": day})
    return sorted(events, key=lambda e: (e["day"], e["label"]))
