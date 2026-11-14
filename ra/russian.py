"""Local Russian display translations of canonical English text."""
from collections import Counter
from functools import lru_cache
import re

from scripts.translation_review.checks import SCALES, currency_set

from . import keywords, local_models, model_store, translation, translation_names
from .article_translation import passages

# Keep URLs, filing codes, figures, and their signs verbatim in display copies.
_IDENTIFIERS = re.compile(r"https?://[^\s<>]+|\b(?=[A-Z0-9/-]*[A-Z])(?=[A-Z0-9/-]*\d)[A-Z0-9]+(?:[-/][A-Z0-9]+)*\b")
_NUMBERS = re.compile(r"[-+−]?\d+(?:[.,]\d+)*")
_LITERALS = re.compile(r"ZXQ[A-Z]*\d+ZXQ|" + _IDENTIFIERS.pattern + "|" + _NUMBERS.pattern)
_RUSSIAN = re.compile(r"[А-Яа-яЁё]")


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
    for pattern in (_NUMBERS, _IDENTIFIERS):
        if Counter(pattern.findall(source)) != Counter(pattern.findall(result)):
            raise ValueError("Translation changed a number or identifier")
    changed_currencies = _currencies(source) ^ _currencies(result)
    # In English headlines "won" can be a verb, even without any financial amount.
    if changed_currencies - {"KRW"} or ("KRW" in changed_currencies and re.search(r"KRW|₩", source + result)):
        raise ValueError("Translation changed a currency")
    # Russian проценты can also mean interest. Count percentage units on amounts.
    percent = r"%|\bpercent(?:ages?)?\b"
    if re.search(r"\binterest\b", source, re.I):
        percent += r"|(?<=\d)\s*процент\w*\b"
    else:
        percent += r"|\bпроцент\w*"
    million = SCALES['million'] + r"|(?<=\d)m\b|\bmln\b|(?<=\d)\s*(?:mi|εκ)\b"
    billion = SCALES['billion'] + r"|(?<=\d)b\b|\bbln\b|(?<=\d)\s*bi\b"
    for unit in (million, billion, percent):
        if len(re.findall(unit, source, re.I)) != len(re.findall(unit, result, re.I)):
            raise ValueError("Translation changed a magnitude or percentage")
    return translation_names.restore(result.strip(), names)


@lru_cache(maxsize=2048)
def _passage(source, names):
    # Failures raise so that a retry after model installation can succeed.
    masked = dict(names)
    unprotected = source
    for marker in masked:
        unprotected = unprotected.replace(marker, "")
    if not re.search(r"[A-Za-z]", unprotected) or _IDENTIFIERS.fullmatch(source):
        return translation_names.restore(source, masked)
    try:
        return _gemma_passage(source, masked)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        # Preserve the original failure if the offline pair cannot supply a checked copy.
        try:
            model = translation._model("en", "ru")
            if model is None and model_store.ensure_model("en", "ru")[0]:
                translation._model.cache_clear()
                model = translation._model("en", "ru")
            if model is not None:
                tokenizer, translator, prefix = model
                tokens = tokenizer.encode(source, out_type=str)
                result, = translator.translate_batch(
                    [tokens], target_prefix=[[prefix]] if prefix else None, replace_unknowns=True,
                    beam_size=4, max_input_length=0, max_decoding_length=2048)
                pieces = result.hypotheses[0]
                if len(pieces) >= 2048:
                    raise ValueError("Incomplete offline translation")
                candidate = tokenizer.decode_pieces(pieces).removeprefix(prefix)
                return _checked(source, candidate, masked)
        except (OSError, ImportError, RuntimeError, ValueError):
            pass
        raise


def _gemma_passage(source, masked):
    instruction = (
        "Translate the complete passage into Russian. Preserve credit meaning, negation, units and uncertainty. "
        "Copy identifiers, URLs, numbers (including signs and separators), names and ZXQ placeholders exactly. "
        "Return an empty string if unable to translate the whole passage faithfully.")
    schema = keywords.object_schema({"russian": {"type": "string"}})
    result = keywords.model_json(instruction, source, schema, model=local_models.GEMMA)
    try:
        return _checked(source, result["russian"], masked)
    except ValueError as rejection:
        # Shield exact values from localization on the repair pass, then validate the restored passage.
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
        repair = (f"Translate the entire passage into Russian. Previous output failed: {rejection}. "
                  "Return the translation without surrounding quotes. Render headings, labels and country names "
                  "in Russian. Transcribe unprotected Latin-script names into Cyrillic, including standalone "
                  "brands and company names. Preserve every ZXQ marker verbatim once. Keep the currency, scale, "
                  "time convention, credit meaning, and uncertainty. Never add new numbers or examples.")
        result = keywords.model_json(repair, protected, schema, model=local_models.GEMMA)
        candidate = result["russian"]
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Missing Russian translation")
        restored = translation_names.restore(candidate, literals)
        if prefix in restored:
            raise ValueError("Translation invented a protected value")
        return _checked(source, restored, masked)


def checked_translation(text, subject=None):
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
            parts.append(_passage(masked, tuple(names.items())))
        output.append(" ".join(parts))
    return "".join(output)


def translate(text: str, subject=None) -> str:
    """Return the complete Russian rendering, or an empty string for retry."""
    try:
        return checked_translation(text, subject)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        return ""
