"""Local Russian display translations of canonical English text."""
from collections import Counter
from functools import lru_cache
import re

from scripts.translation_review.checks import SCALES, currency_set

from . import keywords, local_models, online_translation, translation_names
from .article_translation import passages

# Keep URLs, filing codes, figures, and their signs verbatim in display copies.
_IDENTIFIERS = re.compile(r"https?://[^\s<>]+|\b(?=[A-Z0-9/-]*[A-Z])(?=[A-Z0-9/-]*\d)[A-Z0-9]+(?:[-/][A-Z0-9]+)*\b")
_NUMBERS = re.compile(r"[-+−]?\d+(?:[.,]\d+)*")
_LITERALS = re.compile(r"ZXQ[A-Z]*\d+ZXQ|\b(?:USD|VND|BRL|EUR|TRY|IDR|RUB|KRW)\b|" + _IDENTIFIERS.pattern + "|" + _NUMBERS.pattern)
_RUSSIAN = re.compile(r"[А-Яа-яЁё]")

FINANCIAL_INSTRUCTIONS = (
    "Keep tickers and quarter codes in Latin script. Keep currency codes verbatim, including VND. "
    "Use Russian word order for dates and amounts, but retain the original digits and punctuation inside "
    "each number (17,600 billion VND = 17,600 млрд VND). Explicitly state the currency of a loss or gain. "
    "For a return measured over a period, use за период; over ten years means за десять лет. "
    "Render compounded annual returns as годовых с учётом капитализации. ")


# Market data sometimes arrives with HTML table cells concatenated into one line.
_TABLE_WORDS = {
    "YearOpenHighLowCloseReturnVolumeSessions": "Год / Открытие / Максимум / Минимум / Закрытие / Доходность / Объём / Сессии ",
    "YearOpenHighLowCloseReturnVolumeSession": "Год / Открытие / Максимум / Минимум / Закрытие / Доходность / Объём / Сессии ",
    "MonthClose end of monthChanges": "Месяц / Закрытие на конец месяца / Изменения ",
    "MonthClose end of monthChange": "Месяц / Закрытие на конец месяца / Изменение ",
    "MonthMonth end closeChange": "Месяц / Закрытие на конец месяца / Изменение ",
    "January": " январь", "February": " февраль", "March": " март", "April": " апрель",
    "May": " май", "June": " июнь", "July": " июль", "August": " август",
    "September": " сентябрь", "October": " октябрь", "November": " ноябрь", "December": " декабрь",
    "million": " млн ", "jt": " млн ", "n/a": "н/д",
}
_TABLE_TOKEN = re.compile("|".join(map(re.escape, _TABLE_WORDS)) + r"|IDR|Rp|[MB]|[\d.,%+−/\s-]+")
_FIXED_LABEL = re.compile(r"(?:PDF|WhatsApp|PV•|[A-Z0-9]{1,12}\.[A-Z]{1,3}|"
                          r"(?:CNPJ n[º°]|NIRE) [\d./-]+|(?:0?[1-9]|1[0-2]):[0-5]\d [AP]M)")


def fixed_display(source):
    if _FIXED_LABEL.fullmatch(source):
        return source
    if source.count("IDR") + source.count("Rp") >= 3:
        tokens = _TABLE_TOKEN.findall(source)
        if "".join(tokens) == source:
            return "".join(_TABLE_WORDS.get(token, token) for token in tokens).strip()
    return None


def _currencies(text):
    # The diagnostic currency matcher also accepts the ordinary English verb "try".
    text = re.sub(r"евро(?=[а-яё])", "", text, flags=re.I)
    currencies = currency_set(re.sub(r"\b[Tt]ry\b", "", text))
    if re.search(r"\b(?:Brazilian real|(?:бразильск\w*|\d+)\s+реал|реал(?:а|ов|ам|ами|ах|ом|ы))\b", text, re.I):
        currencies.add("BRL")
    if re.search(r"\bлир(?:а|ы|е|у|ой|ам|ами|ах)?\b", text, re.I):
        currencies.add("TRY")
    return currencies


def _checked(source, result, names):
    if not isinstance(result, str) or not _RUSSIAN.search(result):
        raise ValueError("Missing Russian translation")
    if Counter(re.findall(r"ZXQ[A-Z]*\d+ZXQ", source)) != Counter(re.findall(r"ZXQ[A-Z]*\d+ZXQ", result)):
        raise ValueError("Translation changed a protected value")
    if Counter(_NUMBERS.findall(source)) != Counter(_NUMBERS.findall(result)):
        result = _restore_number_format(source, result)
    for pattern in (_NUMBERS, _IDENTIFIERS):
        if Counter(pattern.findall(source)) != Counter(pattern.findall(result)):
            raise ValueError("Translation changed a number or identifier")
    changed_currencies = _currencies(source) ^ _currencies(result)
    # In English headlines "won" can be a verb, even without any financial amount.
    if changed_currencies - {"KRW"} or ("KRW" in changed_currencies and re.search(r"KRW|₩", source + result)):
        raise ValueError("Translation changed a currency")
    # Russian проценты can also mean interest. Count percentage units on amounts.
    percent = r"%|\bpercent(?:ages?)?\b"
    if re.search(r"\b(?:interest|compound(?:ed|ing)?)\b", source, re.I):
        percent += r"|(?<=\d)\s*процент\w*\b"
    else:
        percent += r"|\bпроцент\w*"
    million = SCALES['million'] + r"|(?<=\d)m\b|\bmln\b|(?<=\d)\s*(?:mi|εκ)\b"
    billion = SCALES['billion'] + r"|(?<=\d)b\b|\bbln\b|(?<=\d)\s*bi\b"
    # A currency symbol disambiguates a spaced financial suffix from metres.
    money = r"(?:[$€£₽¥₫]\s*\d[\d.,]*\s+)"
    million += "|" + money + r"m\b"
    billion += "|" + money + r"b\b"
    for unit in (million, billion, percent):
        if len(re.findall(unit, source, re.I)) != len(re.findall(unit, result, re.I)):
            raise ValueError("Translation changed a magnitude or percentage")
    return translation_names.restore(result.strip(), names)


def _restore_number_format(source, result):
    # Only restore unambiguous English financial literals. A lone comma before three digits is ambiguous.
    forms = {}
    originals = set(_NUMBERS.findall(source))
    for value in originals:
        if not re.fullmatch(r"[-+−]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)", value):
            continue
        if "," not in value and len(value.split(".")[1]) == 3:
            continue
        form = value.replace(",", " ").replace(".", ",")
        if form not in originals:
            forms[form] = value
    if not forms:
        return result
    # Match whole numbers, outside identifiers, URLs and dates. Recheck every original counter afterwards.
    token = r"(?<![\w/.,+−-])[-+−]?\d+(?:[ \u00a0\u202f]\d{3}\b)*(?:[.,]\d+)*(?![\w/]|[.,]\d)"
    return re.sub(token, lambda m: forms.get(" ".join(m[0].split()), m[0]), result)


@lru_cache(maxsize=2048)
def _passage(source, names):
    # Failures raise so that a retry after model installation can succeed.
    masked = dict(names)
    unprotected = source
    for marker in masked:
        unprotected = unprotected.replace(marker, "")
    if not re.search(r"[A-Za-z]", unprotected) or _IDENTIFIERS.fullmatch(source):
        return translation_names.restore(source, masked)
    if (fixed := fixed_display(source)) is not None:
        return fixed
    try:
        return _gemma_passage(source, masked)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as local_error:
        protected, literals = (protect_literals(source, masked) if isinstance(local_error, ValueError)
                               else (source, {}))
        try:
            candidate = online_translation.google_passage(protected, "en", "ru")
        except online_translation.ProviderError as exc:
            if not isinstance(exc, online_translation.InvalidTranslation):
                raise OSError("Google translation unavailable") from exc
            raise local_error
        try:
            return _checked(source, translation_names.restore(candidate, literals), masked)
        except ValueError:
            if isinstance(local_error, (OSError, RuntimeError)):
                raise local_error
            raise


def protect_literals(source, masked):
    prefix = "ZXQVALUE"
    while prefix in source:
        prefix += "X"
    literals = {}

    def protect(match):
        value = match.group()
        if value in masked:
            return value
        marker = f"{prefix}{len(literals)}ZXQ"
        literals[marker] = value
        return marker

    protected = _LITERALS.sub(protect, source)
    return protected, literals


def _gemma_passage(source, masked):
    instruction = (
        "Translate the complete passage into Russian. Preserve credit meaning, negation, units and uncertainty. "
        "Copy identifiers, URLs, numbers (including signs and separators), names and ZXQ placeholders exactly. "
        + FINANCIAL_INSTRUCTIONS + "Return an empty string if unable to translate the whole passage faithfully.")
    schema = keywords.object_schema({"russian": {"type": "string"}})
    result = keywords.model_json(instruction, source, schema, model=local_models.GEMMA)
    try:
        return _checked(source, result["russian"], masked)
    except ValueError as rejection:
        # Shield exact values from localization on the repair pass, then validate the restored passage.
        protected, literals = protect_literals(source, masked)
        repair = (f"Translate the entire passage into Russian. Previous output failed: {rejection}. "
                  "Return the translation without surrounding quotes. Render headings, labels and country names "
                  "in Russian. Transcribe standalone brand names into Cyrillic. Copy every ZXQ marker verbatim "
                  "once. Keep the scale, credit meaning and uncertainty. Never add new numbers or examples. "
                  + FINANCIAL_INSTRUCTIONS)
        if "currency" in str(rejection):
            repair += (" The previous translation lost or changed a currency. Use the ISO code explicitly "
                       "for each source currency, including currencies in hyphenated adjectives: "
                       + ", ".join(sorted(_currencies(source))))
        result = keywords.model_json(repair, protected, schema, model=local_models.GEMMA)
        candidate = result["russian"]
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Missing Russian translation")
        restored = translation_names.restore(candidate, literals)
        return _checked(source, restored, masked)


def checked_translation(text, subject=None, *, translate_passage=None):
    """Translate every passage or raise the failure so the queue can distinguish rejection from outage."""
    output = []
    for line in re.split(r"(\s*\n\s*)", text):
        if not line.strip():
            output.append(line)
            continue
        parts = []
        for part in passages(line, subject=subject):
            source = part.strip()
            # Truncated legal names in search snippets keep their original spelling and ellipsis.
            prefix = re.fullmatch(r"(.+?)\s*(?:\.\.\.|…)", source)
            if prefix and len(prefix[1].split()) >= 2 and any(name.casefold().startswith(prefix[1].casefold() + " ")
                              for name in translation_names.names(subject)):
                parts.append(source)
                continue
            masked, names = translation_names.mask(source, subject)
            parts.append((translate_passage or _passage)(masked, tuple(names.items())))
        output.append(" ".join(parts))
    return "".join(output)


def translate(text: str, subject=None) -> str:
    """Return the complete Russian rendering, or an empty string for retry."""
    try:
        return checked_translation(text, subject)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        return ""
