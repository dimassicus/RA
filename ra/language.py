"""Detect the article's language independently of its news feed's locale."""

from __future__ import annotations

import re
import threading


_detector_lock = threading.Lock()

# Short filing filenames give the statistical detector little prose to identify.
_ENGLISH_FILING_TITLE = re.compile(
    r"(?i:(?:financial[ _-]*statements?|annual[ _-]*report))"
    r"(?:[ _-]+(?:[0-9]{2,4}|[IV]{1,3}|Q[1-4]|H[12]|[A-Z]{2,6})){0,4}"
    r"(?:\.pdf|\s*\|\s*PDF(?:\s+-\s+[^\n]+)?)?"
)


def base_language(code: str) -> str:
    return (code or "").lower().replace("_", "-").split("-", 1)[0]


def _detected(text: str) -> tuple[str, float]:
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException

    try:
        # langdetect publishes its global factory before loading all profiles.
        with _detector_lock:
            DetectorFactory.seed = 0
            guesses = detect_langs(text)
    except LangDetectException:
        return "", 0.0
    return (guesses[0].lang, guesses[0].prob) if guesses else ("", 0.0)


def detect_language(title: str, feed_locale: str = "") -> str:
    """Return a language code, zxx for ticker lists, or an empty string if unknown."""
    text = re.sub(r"\s+-\s+[^-]{2,60}$", "", title or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\*+[A-Z0-9]{2,12}\*+(?:\s+\*+[A-Z0-9]{2,12}\*+)+", text):
        return "zxx"  # An exchange ticker list contains no linguistic content.
    if _ENGLISH_FILING_TITLE.fullmatch(title.strip()):
        return "en"
    hint = feed_locale.split("-", 1)[0].lower()
    if re.match(r"^(fato relevante|comunicado ao mercado)\b", text, re.I):
        return "pt"
    greek = len(re.findall(r"[\u0370-\u03ff\u1f00-\u1fff]", text))
    if greek >= 3 and greek > sum(c.isalpha() for c in text) / 2:
        return "el"
    code, confidence = _detected(text)
    if re.search(r"[ўқғҳәңөұүһі]", text, re.I):
        # A local name inside Russian prose does not change the document's language.
        russian_words = set(re.findall(r"\b(?:и|в|на|по|для|о|от|из|при|что|с|со|не|об)\b", text.casefold()))
        if code == "ru" and confidence >= 0.85 and len(russian_words) >= 3:
            return "ru"
        if re.search(r"[ўҳ]", text, re.I):
            return "uz"
        if re.search(r"[әңөұүһ]", text, re.I):
            return "kk"
        if hint in ("kk", "uz"):
            return hint
        return code if code in ("uk", "be") and confidence >= 0.85 else ""
    if code and confidence >= 0.85:
        if hint in ("uz", "kk") and not re.search(r"[\u0400-\u052f]", text) and code not in ("en", "ru"):
            return hint  # Those two languages are absent from langdetect's profiles.
        return code
    if re.search(r"[\u0400-\u052f]", text):
        return "ru" if hint in ("ru", "uz", "kk") else ""
    return hint
