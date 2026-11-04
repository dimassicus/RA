RA isolated translation benchmark

This is an exploratory local evaluation, not a production translation switch.
All model weights, caches, frozen sources and raw results live under the ignored
.local/translation-benchmark directory. The separate Python environment is
.local/nllb-benchmark-env. The app's .venv, models, database and configuration
are not modified by these benchmark scripts. Existing local baseline models
are read but never installed or backfilled by this harness.

Setup (from the repository root):
  .venv/bin/python -m venv .local/nllb-benchmark-env
  .local/nllb-benchmark-env/bin/python -m pip install -r scripts/translation_benchmark_requirements.txt
  .local/nllb-benchmark-env/bin/python scripts/prepare_translation_benchmark.py download nllb madlad
  .local/nllb-benchmark-env/bin/python scripts/prepare_translation_benchmark.py convert nllb madlad

The download script pins exact official-hosted repository revisions, resumes
bounded byte ranges and verifies large-file SHA-256 values from the pinned
manifests. It sends no credentials. Conversion uses local files, no remote code,
and CTranslate2 INT8. Conversion refuses to overwrite an existing output; use a
new local directory for a different experiment. Original checkpoint sizes are
about 5.5 GB (NLLB) and 11.8 GB (MADLAD). Temporary range assembly requires extra
disk space; completed ranges are removed after hash verification.

Run each benchmark separately to avoid concurrent CPU contention:
  .venv/bin/python scripts/benchmark_translation.py baseline --segmentation sentence
  .local/nllb-benchmark-env/bin/python scripts/benchmark_translation.py nllb --segmentation sentence
  .local/nllb-benchmark-env/bin/python scripts/benchmark_translation.py madlad --segmentation sentence
Repeat with --segmentation paragraph for the segmentation comparison.
  .venv/bin/python scripts/render_translation_benchmark.py

The output review sheet is output/translation-benchmark-review.html. Open it
locally in a browser. It contains three fictional articles in both directions,
anonymous candidate labels, factual checklists, local review notes and JSON
export. No content is uploaded. Human review has not occurred merely because
the page was generated. The machine-readable results are copied alongside it.

Corpus:
- Three explicitly fictional paired credit-news articles, RU->EN and EN->RU.
  These are independent input texts, not a round-trip test. Models see only the
  source text, never the other-language version or factual checklists.
- Eight synthetic stress examples for legal meaning, entity identity, numbers,
  dates, conditions and negation across RU/EN/PT/VI/TR/EL.
- Five frozen saved headline inputs, when saved-sources.json is available.
  These come from a read-only database export, not new provider requests.
- The cached Heringer PDF was excluded because its extraction contains many
  invalid control characters. See input-quality.json; translation cannot make
  this source reliable. Source extraction needs separate remediation.

Limits:
- This is a small, deliberately adversarial corpus, not representative news
  sampling, a validated quality score or analyst-labelled risk evaluation.
- Sentence splitting is intentionally simple for the controlled fixtures;
  production needs a tested multilingual segmenter, including abbreviations.
- Every source character is submitted. That does NOT prove the model retained
  every fact in its output; semantic omissions require explicit review.
- Candidate source chunks are at most 480 tokens including special tokens.
  A source unit that cannot fit is rejected; it is never silently truncated.
  Output reaching the decoding limit is also reported as an error.
- Numeric surface-change and literal-name flags are diagnostics only. Valid
  decimal/currency formats, spelled-out numbers and transliteration can trigger
  them. A preserved number can still have the wrong unit or owner.
- Baseline keeps its existing name protection and Portuguese glossary. The
  candidates use raw translation without that glossary, so this compares
  prospective pipelines as well as models. It is not a bare-model leaderboard.
- Baseline EN->RU is unsupported, not English passthrough or an invented score.
- INT8 results do not establish parity with full-precision checkpoints.
- Latencies are local observations, with candidate inference on four CPU
  threads; the baseline retains the app's defaults. They are not a controlled
  hardware throughput comparison. Initial model load is recorded separately.

Provenance and references:
Meta NLLB 200 distilled 1.3B, CC-BY-NC-4.0, non-commercial hobby evaluation:
  https://huggingface.co/facebook/nllb-200-distilled-1.3B
  revision 7be3e24664b38ce1cac29b8aeed6911aa0cf0576
The model card limits its intended use to research and warns about legal-domain
and document translation; training input lengths did not exceed 512 tokens.

Google MADLAD-400 3B MT, Apache-2.0, Google-hosted PyTorch conversion:
  https://huggingface.co/google/madlad400-3b-mt
  revision fa184c675da0b5c9e1c8694fccd4e12e2d422094
The model card credits Juarez Bochi with converting the original weights.

CTranslate2 model conversion and special-token conventions:
  https://opennmt.net/CTranslate2/guides/transformers.html
Legal stress case: dismissal without prejudice permits a later filing:
  https://www.uscourts.gov/glossary

Before adoption: obtain independent human reviews, add real articles, resolve
critical errors, validate source extraction, preserve source/translation
alignment, and test downstream company/event attribution and risk scoring.
