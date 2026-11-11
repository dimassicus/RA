"""Local headline translation using a downloaded Argos CTranslate2 model.

Headline-only models are optional. The original text remains the source of
record; an empty English field means no model was available.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from . import translation_names
from .model_store import ensure_model
from .language import detect_language


ROOT = Path(__file__).resolve().parent.parent
MODEL_ROOT = ROOT / ".local" / "models"
PT_CREDIT_GLOSSARY = (
    ("recuperação extrajudicial", "out-of-court restructuring"),
    ("recuperação judicial", "court-supervised restructuring"),
    ("tutela de urgência cautelar", "urgent interim protective relief"),
    ("conselho de administração", "board of directors"),
    ("conselho", "board"),
    ("deferimento", "grant of"),
    ("procedimento de mediação", "mediation proceeding"),
    ("execuções", "enforcement proceedings"),
)


@lru_cache(maxsize=3)
def _model(source_code: str, target_code: str = "en"):
    path = MODEL_ROOT / f"{source_code}_{target_code}"
    if not (path / "model").is_dir() or not (path / "sentencepiece.model").is_file():
        return None
    import ctranslate2
    import sentencepiece

    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(path / "sentencepiece.model"))
    translator = ctranslate2.Translator(str(path / "model"), device="cpu", inter_threads=1)
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    return tokenizer, translator, metadata.get("target_prefix", "")


def installed_keyword_translations(terms, target_code):
    """Use an installed English-to-target Argos pair when available."""
    model = _model("en", target_code)
    if not model:
        return {}
    tokenizer, translator, prefix = model
    inputs = [tokenizer.encode(term, out_type=str) for term in terms]
    outputs = translator.translate_batch(inputs, target_prefix=[[prefix] for _ in terms] if prefix else None,
                                         replace_unknowns=True, beam_size=4)
    return {term: tokenizer.decode_pieces(result.hypotheses[0]).removeprefix(prefix).strip()
            for term, result in zip(terms, outputs)}


def argos_translate(text: str, language: str, company_name: dict | str = "", publisher: str = "", *, complete=False) -> tuple[str, str]:
    """Return English text and provenance, or empty strings when unavailable."""
    text = (text or "").strip()
    source_code = language.split("-", 1)[0].lower()
    if not text:
        return "", ""
    if complete and len(text) > 1000:
        return "", "Article passage exceeds offline translation limit"
    if source_code == "en":
        return text, "original English"
    if source_code == "pt":
        for portuguese_prefix, english_prefix in (
            ("Fato Relevante - ", "Material Fact — "),
            ("Comunicado ao Mercado - ", "Market Notice — "),
        ):
            if text.casefold().startswith(portuguese_prefix.casefold()):
                body, provider = argos_translate(
                    text[len(portuguese_prefix):], language, company_name, publisher, complete=complete
                )
                return (english_prefix + body, provider) if body else ("", provider)
        if text.casefold() == "fato relevante":
            return "Material Fact", "Brazilian filing glossary"
    try:
        model = _model(source_code)
        if model is None:
            ready, status = ensure_model(source_code)
            if not ready:
                return "", status
            _model.cache_clear()
            model = _model(source_code)
        if model is None:
            return "", f"offline model unreadable ({source_code})"
        tokenizer, translator, prefix = model
        protected, names = translation_names.mask(text[:1000], company_name)
        publisher_marker = "ZXQPUBLISHERZXQ"
        if publisher:
            protected = re.sub(re.escape(publisher), publisher_marker, protected, flags=re.IGNORECASE)
        terms = []
        if source_code == "pt":
            for source_term, english_term in PT_CREDIT_GLOSSARY:
                if re.search(re.escape(source_term), protected, flags=re.IGNORECASE):
                    term_marker = f"ZXQTERM{len(terms)}ZXQ"
                    protected = re.sub(re.escape(source_term), term_marker, protected, flags=re.IGNORECASE)
                    terms.append((term_marker, english_term))
        tokens = tokenizer.encode(protected, out_type=str)
        if not tokens:
            return "", ""
        result = translator.translate_batch(
            [tokens], target_prefix=[[prefix]] if prefix else None,
            replace_unknowns=True, beam_size=4, length_penalty=0.2,
            **({"max_input_length": 0, "max_decoding_length": 2048} if complete else {}),
        )[0]
        translated = tokenizer.decode_pieces(result.hypotheses[0]).replace("▁", " ").strip()
        if prefix and translated.startswith(prefix):
            translated = translated[len(prefix):].strip()
        for protected_marker in (publisher_marker, *(term[0] for term in terms)):
            if translated.count(protected_marker) != protected.count(protected_marker):
                return "", "Argos changed protected names or terms; fallback required"
        translated = translation_names.restore(translated, names)
        if publisher:
            translated = translated.replace(publisher_marker, publisher)
        for term_marker, english_term in terms:
            translated = translated.replace(term_marker, english_term)
        return translated, "Argos offline model" if translated else ""
    except Exception as exc:
        return "", f"translation failed: {type(exc).__name__}"


def translate_text(text: str, language: str, company_name: dict | str = "", publisher: str = "") -> tuple[str, str]:
    candidate, provider = argos_translate(text, language, company_name, publisher)
    if (candidate and translation_names.preserved(text, candidate, company_name)) or not (text or "").strip():
        return candidate, provider
    from .headline_review import fallback
    return fallback(text, language, publisher, provider, company_name)


def backfill(force: bool = False, run_id: int | None = None) -> tuple[int, int]:
    """Translate earlier saved leads after a language model is installed."""
    from . import db
    from .headline_review import audit
    from psycopg.types.json import Jsonb

    db.init_schema()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.id,title,publisher,language,subject_name,company_id,s.subjects FROM search_results r JOIN search_runs s ON s.id=r.run_id "
            "WHERE (%s::bigint IS NULL OR run_id=%s) AND (title_en = '' OR (%s AND translation_provider = 'Argos offline model')) ORDER BY r.id",
            (run_id, run_id, force),
        ).fetchall()
    updated = 0
    for row in rows:
        language = detect_language(row["title"], row["language"])
        title_en, provider = translate_text(row["title"], language, db.subject_for(row, row["subjects"]), row["publisher"])
        result = audit(row["title"], language, title_en)
        publisher_en = row["publisher"]  # Publisher names are brands, not prose.
        with db.connect() as conn:
            conn.execute(
                "UPDATE search_results SET title_en = %s, publisher_en = %s, translation_provider = %s, language = %s, headline_review = %s "
                "WHERE id = %s AND (title_en = '' OR (%s AND translation_provider = 'Argos offline model'))",
                (title_en, publisher_en, provider, language, Jsonb(result), row["id"], force),
            )
        updated += bool(title_en)
    return updated, len(rows)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill saved English headline translations")
    parser.add_argument("--force", action="store_true", help="Refresh earlier Argos translations")
    parser.add_argument("--run", type=int, help="Restrict backfill and retry pending reviews to this run")
    args = parser.parse_args()
    count, total = backfill(force=args.force, run_id=args.run)
    if args.run is not None:
        from .headline_review import review_saved
        review_saved(args.run)
    print(f"Translated {count} of {total} leads missing English text")
