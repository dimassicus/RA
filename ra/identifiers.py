"""Conservative conversion of the old combined identifier column."""

from __future__ import annotations

import re


BRAZIL_CNPJ = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|(?<!\d)\d{14}(?!\d)")


def split_legacy_identifier(value: str, country: str) -> tuple[str, str]:
    """Return (registration number, tax ID), without guessing a single foreign ID's type.

    The old workbook used a spaced slash between two IDs. An unspaced slash
    inside a CNPJ is part of the tax ID and must never be treated as a divider.
    """
    value = str(value or "").strip()
    if not value:
        return "", ""
    parts = re.split(r"\s+/\s+", value, maxsplit=1)
    if len(parts) == 1 and country.strip().casefold() in ("turkey", "türkiye"):
        parts = re.split(r"(?<=\d)/(?=TR\d{10}$)", value, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    if country.strip().casefold() in ("brazil", "brasil") and BRAZIL_CNPJ.fullmatch(value):
        return "", value
    return value, ""
