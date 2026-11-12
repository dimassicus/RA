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
_RUSSIAN = re.compile(r"[А-Яа-яЁё]")


def _currencies(text):
    # The diagnostic currency matcher also accepts the ordinary English verb "try".
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
    for unit in (*SCALES.values(), r"%|\bpercent(?:age)?\b|\bпроцент\w*"):
        if len(re.findall(unit, source, re.I)) != len(re.findall(unit, result, re.I)):
            raise ValueError("Translation changed a magnitude or percentage")
    return translation_names.restore(result.strip(), names)


@lru_cache(maxsize=2048)
def _passage(source, names):
    # Failures raise so that a retry after model installation can succeed.
    masked = dict(names)
    if not re.search(r"[A-Za-z]", source) or source in masked or _IDENTIFIERS.fullmatch(source):
        return translation_names.restore(source, masked)
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
    result = keywords.model_json(
        "Translate the complete passage into Russian. Preserve credit meaning, negation, units and uncertainty. "
        "Copy identifiers, URLs, numbers (including signs and separators), names and ZXQ placeholders exactly. "
        "Return an empty string if unable to translate the whole passage faithfully.",
        source, keywords.object_schema({"russian": {"type": "string"}}), model=local_models.GEMMA)
    return _checked(source, result["russian"], masked)


def translate(text: str, subject=None) -> str:
    """Return the complete Russian rendering, or an empty string for retry."""
    try:
        output = []
        for line in re.split(r"(\s*\n\s*)", text):
            if not line.strip():
                output.append(line)
                continue
            parts = []
            for part in passages(line, subject=subject):
                source, names = translation_names.mask(part.strip(), subject)
                parts.append(_passage(source, tuple(names.items())))
            output.append(" ".join(parts))
        return "".join(output)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        return ""
