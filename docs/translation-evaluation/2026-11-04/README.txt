Translation evaluation snapshots — 4 November 2026

Decision: keep the existing Argos translator active. These are offline review
artifacts, not an application change or approval to switch models.

Reasoning-enabled follow-up (all runs complete):
- reasoning-review-comparison.html contains the comparison with previous runs,
  matched reasoning-off controls, and side-by-side fresh candidate texts.
  reasoning-review-summary.json preserves counts, case routing and run hashes.
- Both models tested 46 cases / 105 candidate translations in each mode:
  original 34 cases plus 12 fresh fictional cases. The fresh candidates were
  manually authored, with nine deliberately wrong and 15 correct translations;
  they are not new Argos/MADLAD outputs. Labels are provisional assistant
  judgments, not independent native-speaker ground truth.
- Qwen reasoning on: 9/19 labelled errors flagged, ten unassessed; 20/46
  case responses/checks failed. Inference total 64.23 minutes.
- Gemma reasoning on: 16/19 flagged, one approved, two unassessed; 2/46
  case responses failed, both evidence-quote mismatches. Total 64.42 minutes.
- Qwen matched off: 18/19 flagged, one approved, no response failures;
  12.78 minutes. Gemma matched off: 7/19 flagged, twelve unassessed,
  28/46 response failures; 8.65 minutes. Timing includes correction checks.
- On fresh cases alone, Qwen on flagged 5/9 errors with six case failures;
  Gemma on flagged 8/9 with one failure. Qwen off flagged all nine with no
  failures; Gemma off flagged four with five failures. Failed assessments
  are never counted as detections. No fresh correct candidate was rejected
  by Gemma on; Qwen on rejected one. Failed responses can still withhold
  correct candidates, so these counts are not false-positive-rate estimates.
- Gemma on caught the real Portuguese possibility/eventual-certainty mistake
  that Qwen still approved in both modes. Both reasoning models corrected the
  Vietnamese currency example. Corrections remain experimental: same-model
  rechecking is correlated, and the sample does not establish production quality.
- Prefer Gemma with reasoning for the next source-based quality-review stage;
  Qwen off is the stronger speed/labelled-test alternative. Do not adopt Qwen
  reasoning on or the new unconstrained Gemma off configuration. Keep the
  current translator and live app unchanged. Do not require analyst foreign-
  language approval; unresolved cases stay withheld in the experiment.
- Reasoning and matched-off runs use the same unconstrained JSON interface,
  schema instructions, context 32768, output cap 8192, temperature zero and
  seed 42. Positive reasoning usage plus a separate final answer was required
  for successful reasoning-on calls. Previous runs used grammar-constrained
  JSON and smaller context/output budgets, so comparison with those alone
  cannot isolate a reasoning effect. Model/backend behavior is configuration-
  dependent; these are not general rankings of the underlying model families.
- Joint reasoning approval would retain 21/46 existing candidates and withhold
  25; it excludes proposed corrections that both models have not independently
  checked. No labelled-bad candidate is selected, but agreement is not proof.
- Next: improve evidence-span handling and number/date normalization, simplify
  response formatting, then test new examples with independent review before
  any automated correction or live-scoring integration. No current code path
  promotes benchmark results into scoring.
- Reviewer harness: 21 unit tests passed during this evaluation. Final report
  checks verified all six run lengths, source/prompt/implementation hashes,
  observed reasoning modes, HTML structure/local links and unchanged hashes
  for all 36 app/config files. No visual browser check was performed.
  Both benchmark instances are unloaded and the LM Studio server is stopped.
  No cloud keys were read, cloud/news APIs called, or models downloaded.

LM Studio follow-up (user's already-downloaded models):
- lmstudio-reviewer-comparison.html compares the original Qwen3 8B run with
  Qwen3.8 27B MLX 5-bit and Gemma 4 31B QAT GGUF Q4_0.
  lmstudio-reviewer-summary.json records counts, settings and joint routing.
  lmstudio-qwen-review.html and lmstudio-gemma-review.html contain detailed
  model judgments, which are not verified corrections.
- Same 34 cases / 81 translations, identical source/prompt hashes. Temperature
  zero, reasoning disabled, 16384 context, 4096 maximum output tokens. No cloud
  API/news-provider calls or new model downloads. Existing work was preserved.
- Qwen3.8: five of ten prior material errors incorrectly called faithful,
  one marked uncertain, four without valid assessment. Four case responses
  failed validation. Routing selected one known-bad fictional translation.
  Three rejected responses described material issues but simultaneously called
  those translations faithful; protocol failure is distinct from not noticing.
- Gemma: three of ten material errors flagged, five marked uncertain, two
  incorrectly called faithful. All 34 responses passed validation. Routing
  selected no previously labelled bad fictional translation, but real-source
  errors remain. The small provisional-label corpus is not an accuracy estimate.
- Both models corrected the Vietnamese currency example. Inference totals,
  including any second correction checks: Qwen3.8 384.61 seconds, Gemma 548.02
  seconds, versus Qwen3 8B 145.74 seconds. Different runtimes/quantization and
  correction counts prevent a hardware-normalized speed comparison.
- Requiring both larger reviewers to approve would select 26 cases and withhold
  eight. Shared errors remain; consensus does not justify live auto-approval.
- 18 unit tests passed. HTML parsing, local links, corpus/prompt hashes and
  all 36 app/config hashes were checked. No visual browser check was performed.
  Both benchmark model instances were unloaded and the LM Studio API server
  stopped, restoring its initial state. The RA app and translator are unchanged.
- Next evaluation should test reasoning-enabled review and fresh examples,
  including shared misses, before any production integration. API comparisons
  can use the same harness later; no cloud credentials were read or used here.

Follow-up automated reviewer evaluation:
- automated-review.html and automated-review-summary.json record 34 cases /
  81 candidate translations checked by local Qwen3 8B Q4_K_M via Ollama.
- Ten fictional translations had prior provisional material-error labels.
  The reviewer detected two, called seven faithful, and failed response
  validation on the remaining case. Four previously nonmaterial outputs were
  rejected. These labels are assistant judgments, not expert ground truth.
- Six of 34 case responses failed validation; seven cases were withheld in
  total. The experimental routing selected 27 candidates, including two
  known-bad MADLAD legal translations (L1_en_ru and L2_en_ru).
- In real excerpts it also approved net lending for Greek net borrowing and
  the Turkish company-name/appointment errors. These are outside the ten-case
  fictional error count. The basic checks caught some mistakes missed by AI.
- Verdict: this reviewer failed the adoption test. No scoring or translation
  changes were enabled. Same-model rechecking is not independent verification.
  No correction reached the second model check in this run; correction routing
  was exercised by unit tests only, not validated by live-model results.
- Fifteen failure/routing tests passed; all 36 app/config file hashes remained
  unchanged. Inference took approximately 146 seconds, excluding download.
  The isolated service was stopped after testing; downloaded weights remain
  ignored and available for further experiments.
- Reproduction instructions: scripts/translation_review/README.txt.
  Unresolved translations are withheld automatically in this experiment;
  no analyst foreign-language review is required. Live integration remains off.

Open the HTML files locally for side-by-side comparisons:

- watchlist-translation-comparison.html: excerpts from two saved articles,
  Russian to English and English to Russian; Argos, NLLB and MADLAD.
- translation-fidelity-comparison.html: 26 fictional cases covering negation,
  dates, debt amounts and legal terms in both RU/EN directions.
  translation-fidelity-results.json preserves the fictional inputs, raw
  outputs and provisional observations. Argos EN-to-RU is unsupported.
- watchlist-multilingual-comparison.html: eight articles, two per language,
  Portuguese, Greek, Turkish and Vietnamese into English; Argos versus MADLAD.
  Six sources correspond to saved records. No Vietnamese-language records
  were available, so two public watchlist-company reports were used instead.

Findings are preliminary assistant judgments, pending human review. MADLAD
was stronger on the selected Greek and Turkish excerpts. Portuguese results
were mixed; the existing legal glossary helped. Both engines made material
Vietnamese errors, including invented currencies and incorrect company or
product names. The RU/EN cases also exposed unresolved legal-term errors.
This small selected sample does not establish production accuracy.

The reports preserve model outputs without corrections. Real-article reports
contain short translated excerpts and publisher links, not full originals.
Localhost links to saved records require the original local RA database.
Model revisions, inference settings and limitations are described in the
reports and scripts/translation_benchmark_README.txt at the repository root.
Weights, private database exports, local caches and credentials remain outside
version control. The original benchmark runners remain under scripts/;
ad hoc follow-up runners and raw publisher inputs remain local and ignored.
These snapshots are review evidence, not a complete portable rerun bundle.

Packaging verification: artifact copies match their generated originals;
HTML parsing and JSON case counts passed. No visual browser check was run.
No application code, active translator configuration or database was changed.
