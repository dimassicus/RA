"""High-recall diagnostics, never proof that a translation is correct."""
from collections import Counter
import re
import unicodedata

CURRENCIES = {
    'USD': r'\bUSD\b|\bdollars?\b|доллар\w*|долл\.|(?<![Rr])\$',
    'BRL': r'\bBRL\b|\bR\s*\$|\breais\b',
    'EUR': r'\bEUR\b|€|\beuros?\b|евро|ευρώ',
    'RUB': r'\bRUB\b|₽|рубл\w*|руб\.|\br(?:u|ou)bles?\b',
    'VND': r'\bVND\b|\bđồng\b|\bdong\b|донг\w*',
    'TRY': r'\bTRY\b|\bTL\b|₺|\blira\b|\blires?\b',
    'KRW': r'\bKRW\b|₩|\bwon\b',
}
SCALES = {
    'million': r'\bmillion\w*|\bмлн\b|\bмиллион\w*|\bmilh(?:ão|ões)\b|\bmilyon\b|\bεκατ\b|\bεκατομμύρι\w*|\btriệu\b',
    'billion': r'\bbillion\w*|\bмлрд\b|\bмиллиард\w*|\bbilh(?:ão|ões)\b|\bmilyar\b|\bδισεκατομμύρι\w*|\btỷ\b',
}


def currency_set(text):
    # Remove Brazilian R $ before looking for a bare dollar symbol.
    dollar_text = re.sub(r'\bR\s*\$', ' BRL ', text, flags=re.I)
    return {code for code, pattern in CURRENCIES.items()
            if re.search(pattern, dollar_text if code == 'USD' else text, re.I)}


def numeric_surface(text):
    # Separator-insensitive only: intentionally does not claim to validate scale,
    # spelled-out numerals, units, numeric ownership or date interpretation.
    normalized = unicodedata.normalize('NFKC', text)
    normalized = re.sub(r'\b\d{1,3}(?:[ \u202f\u00a0]\d{3})+(?!\d)',
                        lambda m: re.sub(r'\s', '', m.group()), normalized)
    return Counter(re.sub(r'[.,]', '', n)
                   for n in re.findall(r'\d+(?:[.,]\d+)*', normalized))


def diagnostics(source, translation, target_lang):
    flags = []
    src, dst = currency_set(source), currency_set(translation)
    if src != dst:
        flags.append({'code': 'currency_difference', 'source': sorted(src),
                      'translation': sorted(dst)})
    if numeric_surface(source) != numeric_surface(translation):
        flags.append({'code': 'numeric_surface_difference'})
    source_scales = {scale: len(re.findall(pattern, source, re.I)) for scale, pattern in SCALES.items()}
    target_scales = {scale: len(re.findall(pattern, translation, re.I)) for scale, pattern in SCALES.items()}
    if source_scales != target_scales:
        flags.append({'code': 'magnitude_word_difference'})
    # English 250,000.002 has a different magnitude to Greek 250.000.002.
    if target_lang == 'en' and re.search(r'\d{1,3}(?:\.\d{3}){2,}', source):
        if re.search(r'\d{1,3}(?:,\d{3})+\.\d{3}\b', translation):
            flags.append({'code': 'possible_numeric_scale_change'})
    if re.search(r'&(quot|apos|amp|lt|gt);', translation):
        flags.append({'code': 'literal_html_entity'})
    if not translation.strip():
        flags.append({'code': 'empty_translation'})
    return flags
