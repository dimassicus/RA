"""Explicit company names, separate from owners, products and affiliates."""
from __future__ import annotations

import re
import unicodedata

from .adverse_terms import PERSON_TITLES


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.translate(str.maketrans({"đ": "d", "ı": "i"}))
    return " ".join(re.findall(r"\w+", value))


def parse_aliases(value: str) -> list[str]:
    # Newlines/semicolons only: commas can form part of a legal name.
    names, seen = [], set()
    for line in re.split(r"[;\n]", value or ""):
        name = " ".join(line.split()).replace('"', "")
        key = normalized(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def validate_aliases(value: str) -> str:
    names = parse_aliases(value)
    if len(names) > 8 or any(len(name) > 150 or len(normalized(name)) < 3 for name in names):
        raise ValueError("Use up to 8 company aliases, each 3–150 characters")
    return "\n".join(names)


LEGAL_SUFFIX = re.compile(
    r"\s+(?:s\s*a|s\s*a\s*s|a\s*s|s\s*a\s*r\s*l|ltda|limited|ltd|inc|plc|llc|corporation|"
    r"sociedade anonima(?: fechada)?|anonim sirketi)$"
)


def company_names(subject: dict) -> list[str]:
    names = []
    for value in (subject.get("name", ""), subject.get("short_name", ""),
                  *parse_aliases(subject.get("aliases", ""))):
        name = normalized(value)
        for candidate in (name, LEGAL_SUFFIX.sub("", name)):
            if len(candidate) >= 3 and candidate not in names:
                names.append(candidate)
    return names


def contains(text: str, phrase: str) -> bool:
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def ambiguous(name: str) -> bool:
    return len(normalized(name).replace(" ", "")) < 6


def headline_identity(text: str, subject: dict) -> bool:
    matches = [name for name in company_names(subject) if contains(text, name)]
    if not matches:
        return False
    if any(not ambiguous(name) for name in matches):
        return True
    contexts = [subject.get("country_registration", "")]
    contexts += re.split(r"[,;\n]", subject.get("primary_industries", ""))
    return any(contains(text, normalized(context)) for context in contexts)


def related_names(subject: dict) -> list[str]:
    """Explicit management/owner names; these never become company aliases."""
    titles = sorted({normalized(term) for title, variants in PERSON_TITLES.items()
                     for term in (title, *variants)}, key=len, reverse=True)
    names = {}
    for field in ("top_management", "key_owners"):
        for part in re.split(r"[,;\n](?![^()]*\))", subject.get(field) or ""):
            name = re.sub(r"\([^)]*\)", "", part).strip().replace('"', "")
            # Intake stores both 'Name (role)' and registry-style 'Name · role'.
            name = name.split(" · ", 1)[0].strip()
            for title in titles:
                name = re.sub(r"^" + re.escape(title) + r"\s*[:–—-]\s*", "", name, flags=re.I)
            name = re.sub(r"^(?:CEO|CFO|COO|UBO)\s+", "", name, flags=re.I)
            key = normalized(name)
            if len(key) >= 3 and key not in titles and key not in {"unknown", "not available", "none", "n a"}:
                names.setdefault(key, name)
    return list(names.values())


def related_identity(text: str, subject: dict) -> bool:
    return any(contains(text, normalized(name)) for name in related_names(subject))


def discovery_names(subject: dict, *, limit=3) -> list[str]:
    """Reviewed issuer names in preference order; affiliates are separate entities."""
    values = [subject.get("name", ""), *parse_aliases(subject.get("aliases", "")),
              subject.get("short_name", "")]
    names, seen = [], set()
    for value in values:
        name = " ".join(value.split()).replace('"', "")
        key = normalized(name)
        if len(key) >= 3 and key not in seen:
            names.append(name)
            seen.add(key)
    return names[:limit]


def provider_name(subject: dict) -> str:
    """One phrase/request: prefer the first explicit, sufficiently distinctive alias."""
    for name in parse_aliases(subject.get("aliases", "")):
        if not ambiguous(name) and len(name) <= 150:
            return name
    return subject.get("name", "").strip().replace('"', "")
