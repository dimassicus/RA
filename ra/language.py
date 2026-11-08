"""Detect the article's language independently of its news feed's locale."""

from __future__ import annotations

import re


def _detected(text: str) -> tuple[str, float]:
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException

    DetectorFactory.seed = 0
    try:
        guesses = detect_langs(text)
    except LangDetectException:
        return "", 0.0
    return (guesses[0].lang, guesses[0].prob) if guesses else ("", 0.0)


def detect_language(title: str, feed_locale: str = "") -> str:
    """Use script plus local detection; return ISO 639-1 or an empty string."""
    text = re.sub(r"\s+-\s+[^-]{2,60}$", "", title or "").strip()
    if not text:
        return ""
    hint = feed_locale.split("-", 1)[0].lower()
    if re.match(r"^(fato relevante|comunicado ao mercado)\b", text, re.I):
        return "pt"
    greek = len(re.findall(r"[\u0370-\u03ff\u1f00-\u1fff]", text))
    if greek >= 3 and greek > sum(c.isalpha() for c in text) / 2:
        return "el"
    if re.search(r"[ўқғҳ]", text, re.I):
        return "uz"
    if re.search(r"[әғқңөұүһі]", text, re.I):
        return "kk" if hint == "kk" else "uz" if hint == "uz" else ""
    code, confidence = _detected(text)
    if code and confidence >= 0.85:
        if hint in ("uz", "kk") and not re.search(r"[\u0400-\u052f]", text) and code not in ("en", "ru"):
            return hint  # Those two languages are absent from langdetect's profiles.
        return code
    if re.search(r"[\u0400-\u052f]", text):
        return "ru" if hint in ("ru", "uz", "kk") else ""
    return hint
