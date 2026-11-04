Automated translation review experiment

This harness is isolated from the live RA app. It neither imports application
translation/scoring code nor connects to the database or news providers.
It does not replace the active Argos translator. Every output explicitly has
score_eligible=false, including candidates that pass experimental checks.

Policy for this experiment:
- Keep the existing translation when it passes source-based checks.
- Otherwise consider MADLAD, then a separately checked proposed correction.
- NLLB is included only as an additional challenge set, not a routing choice.
- Unresolved cases receive translation_uncertain and are automatically withheld.
- There is no request for an analyst to understand the source language.
- No consensus vote: two agreeing translations can both be wrong.

Checks:
1. Currency, numeric-surface and suspicious separator diagnostics compare the
   original with each translation. These are high-recall flags, not semantic
   proofs. Spelled-out numbers and valid formatting can cause false positives.
2. A separate local multilingual model receives the original and randomized
   anonymous candidates. It never receives engine names, previous judgments or
   reference checklists. It checks seven categories, returns structured JSON
   and must support reported errors with exact source and translation quotes.
3. Missing categories, invalid evidence, incomplete generation or unavailable
   reviewer fail closed. No automatic retries are used to fish for a pass.
4. A proposed correction is checked in a fresh request without the prior
   rationale. This is the SAME reviewer model: repeated approval is correlated,
   not independent validation. A fluent or quote-supported claim can be wrong.

Reproduce from repository root:
  .venv/bin/python -m pytest -q tests/test_translation_review.py
  .venv/bin/python scripts/evaluate_translation_review.py --rules-only --work .local/translation-benchmark/automated-review/rules-only

The model used in this experiment is Ollama qwen3:8b (Q4_K_M, about 5.2 GB).
Model artifacts stay in the ignored benchmark directory. A separate Ollama
server uses loopback port 11435; the client never falls back to a cloud service.
Example setup in separate terminals, from repository root:
  OLLAMA_MODELS="$PWD/.local/translation-benchmark/automated-review/models" OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_PARALLEL=1 ollama serve
  OLLAMA_HOST=127.0.0.1:11435 ollama pull qwen3:8b
  .venv/bin/python scripts/evaluate_translation_review.py
Use --resume only for the same corpus, prompt, implementation and model.
Use a different --work directory for a different experiment; results otherwise
replace that directory's previous results.json and review.html.

Input data:
- Committed fictional cases and provisional prior labels live in
  docs/translation-evaluation/2026-11-04/translation-fidelity-results.json.
- The optional eight publisher excerpts require the existing ignored files
  .local/translation-benchmark/watchlist-languages/{baseline,madlad}.json.
  Without those files, only the fictional evaluation runs. They are not
  downloaded automatically. Real source text remains outside version control.
- Reports label actual case counts, invalid responses and missed known errors.
  Previous labels are assistant judgments, not expert or native-speaker truth.
  Known examples are regression tests, not an independent held-out benchmark.

Outputs under the chosen --work directory:
  results.json: raw accepted/invalid responses, source hashes, diagnostics,
                proposed corrections, routing and reviewer metadata.
  review.html: readable summary and fictional-case issues/corrections.
Publisher quotations are omitted from the HTML to avoid reproducing source
material again. They remain available in the previous side-by-side reports.

References:
  https://ollama.com/library/qwen3:8b
  https://huggingface.co/Qwen/Qwen3-8B
  https://docs.ollama.com/capabilities/structured-outputs

LM Studio follow-up:
The same prompt/schema and frozen input set can be evaluated against models
already downloaded in LM Studio. No cloud keys or model downloads are needed.
Load one model at a time with at least 16384 context tokens, then run:
  lms server start --port 1234
  lms load qwen/qwen3.8-27b --identifier ra-review-qwen --context-length 16384 --gpu max
  .venv/bin/python scripts/evaluate_translation_review.py --provider lmstudio --port 1234 --model ra-review-qwen --work .local/translation-benchmark/lmstudio-review/qwen
  lms unload ra-review-qwen
  lms load google/gemma-4-31b-qat --identifier ra-review-gemma --context-length 16384 --gpu max
  .venv/bin/python scripts/evaluate_translation_review.py --provider lmstudio --port 1234 --model ra-review-gemma --work .local/translation-benchmark/lmstudio-review/gemma
  lms unload ra-review-gemma
Stop the server with lms server stop only if it was started for the experiment.
Preserve any models/server state that predated the experiment.

The LM Studio adapter uses /v1/chat/completions with JSON Schema, temperature 0,
seed 42 and 4096 maximum output tokens. Explicit reasoning_effort=none and
chat_template_kwargs.enable_thinking=false prevent structured JSON from being
routed into a reasoning field. This was checked on a readiness prompt before
any benchmark examples; reasoning output is not substituted for a final answer.
This evaluates the models without reasoning, not their best possible performance
with a reasoning budget. Runtime/quantization differ from the earlier Ollama
Qwen3 8B run, so timing is an observed pipeline comparison, not a hardware-normalized
model speed ranking. Corpus and prompt hashes allow checking identical inputs.

The local artifact inventory records sizes/mtimes and hashes for small model
configuration/tokenizer files. Large downloaded weights are not re-hashed;
this identifies the installation but is not cryptographic verification of
every weight shard. Models are used as installed by the user.

LM Studio structured-output interface:
  https://lmstudio.ai/docs/developer/openai-compat/structured-output

Reasoning-enabled follow-up:
  scripts/evaluate_reasoning_review.py
  scripts/compare_reasoning_review.py
  scripts/translation_review/fresh_cases.py

The 12 fresh cases are fictional and manually constructed before inference.
Their 24 candidate translations contain nine deliberately wrong and 15 correct
examples, including four clean pairs and a shared-error pair. They are not
outputs of Argos/MADLAD, nor a native-speaker-labelled news sample. Expected
labels and focus notes are never sent to reviewers. All source-language pairs
from the earlier evaluation are covered. The Portuguese case is explicitly a
targeted regression of the previous miss, not an unrelated held-out challenge.

Reasoning readiness was checked on arithmetic, without benchmark content.
With reasoning and JSON grammar constraints together, the installed Qwen/MLX
backend put constrained output into reasoning_content and left content empty.
The experiment therefore uses unconstrained final JSON, with the schema in the
system message and strict validation afterward. It never treats reasoning text
as the final answer. One enclosing JSON Markdown fence is accepted and recorded;
arbitrary prose around JSON is rejected. No verdict/evidence repair is applied.

Both reasoning-on and matched-off controls use the same interface, prompt,
32768-token context and 8192-token combined output budget. Temperature is zero
and seed 42. On requests set reasoning_effort=medium and enable_thinking=true.
Successful on results must report positive reasoning tokens and nonempty
reasoning_content as well as a separate valid final answer. Off controls must
show no reasoning. The earlier grammar-constrained runs remain preserved and
are reported separately: comparing only with those would confound reasoning
with transport, prompt-schema exposure, context and output-budget changes.

Example (model already loaded with 32768 context, local server on port 1234):
  .venv/bin/python scripts/evaluate_reasoning_review.py --model ra-reason-qwen --mode off --suite fresh --work .local/translation-benchmark/reasoning-review/qwen-off-fresh
  .venv/bin/python scripts/evaluate_reasoning_review.py --model ra-reason-qwen --mode on --suite all --work .local/translation-benchmark/reasoning-review/qwen-on
  .venv/bin/python scripts/evaluate_reasoning_review.py --model ra-reason-qwen --mode off --suite old --work .local/translation-benchmark/reasoning-review/qwen-off-old
Repeat for ra-reason-gemma with corresponding gemma-* output directories.
Run sequentially; unload only model instances created for this experiment.
The runner refuses to replace existing results without --resume; resuming
requires identical model metadata, inputs, prompt and implementation hashes.

After all runs finish:
  .venv/bin/python scripts/compare_reasoning_review.py
This produces reasoning-review-comparison.html and reasoning-review-summary.json
under docs/translation-evaluation/2026-11-04. Raw reasoning traces and publisher
source excerpts remain in the ignored local experiment directory. No model
downloads, app/database mutations, cloud API calls or news-provider calls are
part of these runs. Proposed corrections remain experimental, even if the same
model endorses them in a second call. An accepted correction still requires
separate quality evidence before production use.
