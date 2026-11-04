"""Offline comparison harness. Does not write the app database or download models.

Candidate models: isolated benchmark Python. Baseline: app .venv Python.
Outputs retain source segments and flag possible number/name changes for human review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local" / "translation-benchmark"
CODES = {"en": "eng_Latn", "ru": "rus_Cyrl", "pt": "por_Latn", "vi": "vie_Latn", "tr": "tur_Latn", "el": "ell_Grek"}


def chunks(text, fits):
    """Losslessly partition at sentence/word boundaries; no silent truncation."""
    if fits(text):
        return [text]
    parts = re.split(r"(?<=[.!?])(?=\s+)", text)
    if len(parts) == 1:
        parts = re.findall(r"\s*\S+", text)
        trailing = text[len("".join(parts)):]
        if trailing:
            parts[-1] += trailing
    result, current = [], ""
    for part in parts:
        if not fits(part):
            if current:
                result.append(current)
                current = ""
            if len(parts) == 1 or not re.search(r"\s", part.strip()):
                raise ValueError("One word exceeds source limit; explicit review required")
            result.extend(chunks(part, fits))
        elif current and not fits(current + part):
            result.append(current)
            current = part
        else:
            current += part
    if current:
        result.append(current)
    assert "".join(result) == text, "Chunking omitted source characters"
    assert all(fits(part) for part in result)
    return result


def number_tokens(text):
    # A diagnostic, not a semantic accuracy score: units, dates, numeral spelling
    # and locale separators still need manual review.
    text = unicodedata.normalize("NFKC", text)
    return re.findall(r"\d+(?:[.,]\d+)*", text)


def cases():
    source = json.loads((ROOT / "scripts/translation_benchmark_cases.json").read_text())
    result = []
    for article in source["articles"]:
        for source_lang, target in [("ru", "en"), ("en", "ru")]:
            result.append({"id": f"{article['id']}_{source_lang}_{target}", "kind": "fictional_article", "title": article["title"],
                           "source_lang": source_lang, "target_lang": target, "company": article["company"],
                           "paragraphs": article[source_lang], "checks": article["checks"]})
    for item in source["sentences"]:
        result.append({**item, "kind": "synthetic_stress", "paragraphs": [item["text"]]})
    saved_path = WORK / "saved-sources.json"
    if saved_path.exists():
        saved = json.loads(saved_path.read_text())
        # Specific frozen saved titles, not new searches or cherry-picked outputs.
        ids = {224, 253, 254, 348, 353}
        for row in saved["headlines"]:
            if row["id"] in ids:
                result.append({"id": f"saved_{row['id']}", "kind": "saved_headline", "source_lang": row["language"],
                               "target_lang": "en", "paragraphs": [row["title"]], "company": row["subject_name"],
                               "publisher": row["publisher"], "checks": ["Retain entities, all amounts/dates and the event meaning"],
                               "previously_saved_translation": row["title_en"]})
    return result


class Candidate:
    def __init__(self, name):
        import ctranslate2
        from transformers import AutoTokenizer
        self.name = name
        self.tokenizer = AutoTokenizer.from_pretrained(str(WORK / name / "original"), local_files_only=True, trust_remote_code=False)
        self.translator = ctranslate2.Translator(str(WORK / name / "int8"), device="cpu", compute_type="int8", inter_threads=1, intra_threads=4)
        self.compute_type = self.translator.compute_type

    def encode(self, text, source, target):
        if self.name == "nllb":
            self.tokenizer.src_lang = CODES[source]
        else:
            text = f"<2{target}> " + text
        return self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text, truncation=False))

    def translate(self, text, source, target, company="", publisher=""):
        encoded = self.encode(text, source, target)
        assert len(encoded) <= 480
        prefix = [[CODES[target]]] if self.name == "nllb" else None
        output = self.translator.translate_batch([encoded], target_prefix=prefix, beam_size=4, max_input_length=0,
                                               max_decoding_length=768, return_scores=True)[0]
        tokens = output.hypotheses[0]
        # Reaching the bound is a visible failure, never an accepted truncated article.
        if len(tokens) >= 768:
            raise ValueError("Target hit decoding bound; requires review")
        if self.name == "nllb":
            assert tokens[0] == CODES[target]
            tokens = tokens[1:]
        decoded = self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(tokens), skip_special_tokens=True)
        if not decoded.strip():
            raise ValueError("Empty translation")
        return decoded, {"source_tokens": len(encoded), "target_tokens": len(tokens), "engine": f"{self.name} CT2 INT8"}

    def fits(self, text, source, target):
        return len(self.encode(text, source, target)) <= 480


class Baseline:
    compute_type = "app-default"

    def __init__(self):
        sys.path.insert(0, str(ROOT))
        from ra import translation
        # Prevent fallback installation: the live model directory remains read-only.
        translation.ensure_model = lambda code: (False, f"not installed ({code}); benchmark downloads disabled")
        self.module = translation

    def fits(self, text, source, target):
        return len(text) <= 1000

    def translate(self, text, source, target, company="", publisher=""):
        if target != "en":
            raise ValueError("Existing app translator only targets English")
        out, engine = self.module.translate_text(text, source, company, publisher)
        if not out:
            raise ValueError(engine)
        return out, {"engine": engine}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["nllb", "madlad", "baseline"])
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--segmentation", choices=["paragraph", "sentence"], default="sentence")
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1", TOKENIZERS_PARALLELISM="false")
    selected = [c for c in cases() if not args.case_ids or c["id"] in args.case_ids]
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "cases.json").write_text(json.dumps(cases(), ensure_ascii=False, indent=2))
    start = time.perf_counter()
    engine = Baseline() if args.model == "baseline" else Candidate(args.model)
    loaded = time.perf_counter() - start
    import ctranslate2
    results = []
    target_path = WORK / f"results-{args.model}-{args.segmentation}.json"
    for case in selected:
        item = {**case, "model": args.model, "segments": [], "status": "ok"}
        started = time.perf_counter()
        try:
            if args.model == "baseline" and case["target_lang"] != "en":
                item["status"] = "unsupported_direction"
            else:
                for paragraph, text in enumerate(case["paragraphs"]):
                    # This deliberately small corpus has controlled sentence boundaries.
                    # This is not a production multilingual sentence segmenter.
                    units = re.split(r"(?<=[.!?])(?=\s+[A-ZА-ЯЁÀ-ÖØ-ÞΑ-Ω])", text) if args.segmentation == "sentence" else [text]
                    assert "".join(units) == text
                    split = [chunk for unit in units for chunk in chunks(unit, lambda s: engine.fits(s, case["source_lang"], case["target_lang"]))]
                    for chunk in split:
                        before = time.perf_counter()
                        out, info = engine.translate(chunk, case["source_lang"], case["target_lang"], case.get("company", ""), case.get("publisher", ""))
                        item["segments"].append({"paragraph": paragraph, "source": chunk, "translation": out,
                                                 "seconds": time.perf_counter() - before, **info})
                item["translation"] = "\n\n".join(" ".join(s["translation"] for s in item["segments"] if s["paragraph"] == p) for p in range(len(case["paragraphs"])))
                item["complete_source_coverage"] = all("".join(s["source"] for s in item["segments"] if s["paragraph"] == p) == t for p, t in enumerate(case["paragraphs"]))
                src = "\n\n".join(case["paragraphs"])
                item["source_sha256"] = hashlib.sha256(src.encode()).hexdigest()
                item["numeric_surface_changed"] = Counter(number_tokens(src)) != Counter(number_tokens(item["translation"]))
                item["literal_company_preserved"] = case.get("company", "").casefold() in item["translation"].casefold() if case.get("company") else None
                if args.model == "baseline" and case["kind"] == "fictional_article" and args.segmentation == "paragraph":
                    raw, provenance = engine.module.translate_text(src, case["source_lang"], case.get("company", ""))
                    item["app_without_chunking"] = {"translation": raw, "engine": provenance, "source_characters": len(src), "input_cap": 1000, "characters_beyond_cap": max(0, len(src) - 1000)}
        except Exception as exc:
            item.update(status="error", error=f"{type(exc).__name__}: {exc}")
        item["seconds"] = time.perf_counter() - started
        results.append(item)
        record = {"model": args.model, "segmentation": args.segmentation, "ctranslate2": ctranslate2.__version__, "compute_type": engine.compute_type,
                  "load_seconds": loaded, "intra_threads": 4 if args.model != "baseline" else "app default", "beam_size": 4,
                  "notes": "Not a quality score. Numeric/name flags require human review. Complete source coverage only proves input submission, not semantic completeness. Baseline uses the app translator on the same segments; unchunked app behavior is separately recorded in paragraph mode. No reference text is supplied to any model.",
                  "results": results}
        temporary = target_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        temporary.replace(target_path)
        print(f"{args.model}: {case['id']} {item['status']} {item['seconds']:.2f}s", flush=True)


if __name__ == "__main__":
    main()
