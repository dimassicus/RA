# RA

Local PostgreSQL-backed pilot for multilingual corporate credit monitoring.

- [Research and product specification](RESEARCH_SPEC.md)
- [Initial company list](companies.xlsx)
- [Source registry](sources/README.md)
- [Parallel Perplexity, Gemini and Kimi API proposal](sources/ai-provider-options.md)

The first test case is Fertilizantes Heringer S.A. in Brazil. The intended scale is 100–200 companies across multiple countries and industries.

## Run locally

PostgreSQL must be running and the database name must be exactly `RA` (uppercase). On the current Mac it has already been created. From this folder:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/install_pt_en_model.py
.venv/bin/python -m ra.bootstrap
.venv/bin/python -m ra.translation
.venv/bin/python -m ra.app
```

Open <http://127.0.0.1:8765/>. The app binds to the local machine only. To use a different connection or port, set `RA_DATABASE_URL` (libpq connection URI) or `RA_PORT` in the shell. Keep any database password out of Git. The development server is for local testing; production hosting will need a WSGI server, authentication, backups, and a scheduled worker.

The app can import `companies.xlsx` or a newer uploaded workbook, add/edit/remove/restore watchlist companies, search the whole active list, search a selected company from a dropdown, or search a new company once without saving it. Completed searches have Excel and PDF **lead reports**. Removal archives the watchlist entry while preserving search history.

The pilot currently collects public Google News RSS leads and Heringer's official investor-relations notices. It stores headlines, source links, dates, and simple review keywords. The run page lets an analyst select leads and build a selected-lead Excel or PDF report. Portuguese headlines are translated locally into English with an Argos/OPUS-MT model (original text remains visible); the model derives from OPUS-MT work by Jörg Tiedemann and Santhosh Thottingal under CC BY 4.0. Install models for other languages separately as the watchlist expands. These are **unverified leads**, not a completed credit assessment. Document-level analysis, additional country adapters, scheduled delivery, and email are later work; the [source registry](sources/README.md) records available inputs and prices.
