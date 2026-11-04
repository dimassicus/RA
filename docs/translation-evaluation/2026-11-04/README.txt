Translation evaluation snapshots — 4 November 2026

Decision: keep the existing Argos translator active. These are offline review
artifacts, not an application change or approval to switch models.

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
