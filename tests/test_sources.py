import csv
from pathlib import Path


def test_source_registry_has_unique_ids_and_valid_primary_links():
    path = Path(__file__).resolve().parents[1] / "sources" / "registry.tsv"
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) >= 25
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(
        row["primary_url"].startswith("https://")
        or (not row["primary_url"] and row["pilot_status"] == "hold_identity")
        for row in rows
    )
    assert all(row["checked_on"] for row in rows)
