"""Local HTML interface for the RA watchlist and search pilot."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import pycountry
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from psycopg.errors import UniqueViolation

from . import db, disclosures, governance_roster, keywords, risk_factors, intake_worker, model_store, news_apis, preliminary, worker
from .importer import stage_workbook
from .exposure import topic_links
from .company_report import load_report
from .company_exports import company_pdf, company_xlsx
from .aliases import normalized, provider_name, validate_aliases
from .publisher_lookup import original_title
from .search import company_search_plan
from .reports import make_pdf, make_xlsx
from .relevance import classify_results
from . import event_analysis, score_store, verification_store, headline_review, search_settings
from .scoring import LABELS, RUBRIC, MATERIALITY, VERSION, WEEK_TIMEZONE, week_start


ROOT = Path(__file__).resolve().parent.parent
MAX_FIELD = {"name": 300, "short_name": 300, "country_registration": 150,
             "registration_number": 150, "tax_id": 150, "legal_address": 1500, "comment": 5000, "alias_notes": 5000}


def form_company(form) -> dict:
    data = {}
    for field in db.FIELDS:
        value = str(form.get(field, "")).strip()
        if len(value) > MAX_FIELD.get(field, 1500):
            raise ValueError(f"{field.replace('_', ' ').title()} is too long")
        data[field] = value
    data["aliases"] = validate_aliases(data["aliases"])
    if not data["name"] or not data["country_registration"]:
        raise ValueError("Company name and country of registration are required")
    return data


def company_label(company):
    legal = company["name"]
    short = (company.get("short_name") or "").strip()
    return f"{short} · {legal}" if short and short != legal else legal


def alphabetical_companies(companies):
    return sorted(companies, key=lambda c: (normalized((c.get("short_name") or "").strip() or c["name"]),
                                            normalized(c["name"]), c.get("id") or 0))


def create_app() -> Flask:
    app = Flask(__name__)
    app.jinja_env.globals["country_names"] = sorted(country.name for country in pycountry.countries)
    app.jinja_env.filters["week_cutoff"] = lambda start: start + timedelta(days=7)
    app.jinja_env.filters.update(company_label=company_label, alphabetical_companies=alphabetical_companies)
    app.secret_key = os.environ.get("RA_SECRET_KEY") or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024

    @app.context_processor
    def add_csrf():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"]}

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not supplied.isascii() or not hmac.compare_digest(supplied, expected):
                abort(400, "Invalid form token. Refresh the page and try again.")

    @app.get("/")
    def dashboard():
        companies = db.list_companies(True)
        topics = sorted({topic for company in companies for topic in topic_links(company)})
        return render_template(
            "index.html", companies=companies, topics=topics,
            archived=db.list_companies(False), runs=db.list_runs(), intake=db.list_intake(),
            model_coverage=model_store.coverage([company["country_registration"] for company in companies]),
            news_api_providers=news_apis.configured_providers() + [name for name in ("perplexity", "google") if os.environ.get(f"RA_{name.upper()}_API_KEY")],
            search_today=datetime.now(WEEK_TIMEZONE).date().isoformat(),
            reviewer=headline_review.readiness(),
        )

    @app.route("/risk-factors", defaults={"factors": True}, methods=["GET", "POST"])
    @app.route("/keywords", defaults={"factors": False}, methods=["GET", "POST"])
    def keyword_settings(factors):
        catalog = risk_factors if factors else keywords
        label = "risk factor" if factors else "keyword"
        editing = request.args.get("factor", type=int)
        draft = {}
        if request.method == "POST":
            try:
                if factors and request.form.get("action") == "translation":
                    editing = request.form.get("id", type=int)
                    draft = request.form
                    catalog.save_translation(editing, draft.get("previous", ""), draft.get("language", ""),
                                             draft.get("original", ""), draft.get("translation", ""))
                    keywords.schedule()
                    flash("Translation saved.", "success")
                    return redirect(url_for("keyword_settings", factors=True, factor=editing, _anchor=f"translations-{editing}"))
                elif request.form.get("action") != "retry":
                    row_id = int(request.form.get("id") or 0)
                    catalog.save(row_id, request.form.get("english", ""),
                                 request.form.get("scope" if factors else "direction", ""),
                                 request.form.get("enabled") == "yes", request.form.get("previous", ""))
                keywords.schedule()
                flash(f"{label.capitalize()} settings saved. Missing translations queued.", "success")
            except UniqueViolation:
                flash(f"This English {label} already exists.", "error")
            except ValueError as exc:
                flash(str(exc), "error")
            if not draft:
                return redirect(url_for("keyword_settings", factors=factors))
        return render_template("keywords.html", rows=catalog.records(), languages=catalog.languages(),
                               language_name=keywords.language_name, factors=factors, label=label,
                               options=risk_factors.SCOPES if factors else keywords.DIRECTIONS, editing=editing, draft=draft,
                               error=keywords.last_error, risk_factor_limit=risk_factors.MAX_DESCRIPTION)

    @app.get("/api/translations/status")
    def translation_status():
        from . import local_models
        catalog = risk_factors if request.args.get("factors") == "1" else keywords
        codes = catalog.languages()
        rows = [row for row in catalog.records() if row["enabled"]]
        if catalog is keywords:
            rows.extend(keywords.active_phrases(rows))
        saved = sum(code in row["translations"] for row in rows for code in codes)
        total = len(rows) * len(codes)
        return jsonify(**keywords.translation_status(), saved=saved, total=total,
                       missing=total - saved, model_activity=local_models.activity())

    @app.post("/risk-factors/save-all")
    def save_all_risk_factors():
        try:
            count = risk_factors.save_all(request.form)
        except UniqueViolation:
            return jsonify(error="An English risk factor with that description already exists."), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if count:
            keywords.schedule()
            flash("All risk factor edits saved.", "success")
        return jsonify(saved=count)

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if request.method == "POST":
            if any(request.form.get(key) not in ("on", "off") for key in search_settings.DEFAULTS):
                abort(400, "Choose On or Off for each search setting.")
            search_settings.save({key: request.form[key] == "on" for key in search_settings.DEFAULTS})
            flash("Search settings saved for all new searches.", "success")
            return redirect(url_for("setup"))
        runs = db.list_runs()
        for run in runs:
            run["checks"] = verification_store.job_summary(run["id"])
        return render_template("setup.html", runs=runs, reviewer=headline_review.readiness(),
                               search_settings=search_settings.read())

    @app.post("/setup/runs/<int:run_id>/retry")
    def retry_run_stage(run_id):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        if run["status"] in ("queued", "running"):
            abort(409, "Wait for the search to finish.")
        stage = request.form.get("stage")
        if stage == "translations":
            count = headline_review.request_retry(run_id)
            reviewer = headline_review.readiness()
            flash(f"{count} unfinished translations queued for retry when the search worker is idle. "
                  + reviewer["message"], "success" if reviewer["ready"] else "warning")
        elif stage == "sources":
            count = verification_store.retry_failed_run(run_id)
            flash(f"Queued {count} failed or inaccessible source checks. Publisher recovery may use API credits.", "success")
        elif stage == "search":
            if run["status"] not in ("failed", "partial"):
                abort(409, "This search has no recorded failure.")
            if run["scope"] == "one_off" or any(not (db.get_company(s.get("id")) or {}).get("active") for s in run["subjects"]):
                abort(409, "Choose an active watchlist company to start a new search.")
            new_id = db.create_run(run["scope"], run["subjects"], window_start=run.get("window_start"),
                                   window_end=run.get("window_end"))
            worker.enqueue(new_id)
            flash(f"Search restarted as run #{new_id} using run #{run_id}'s company list.", "success")
            return redirect(url_for("run_detail", run_id=new_id))
        else:
            abort(400, "Unknown process.")
        return redirect(url_for("setup"))

    @app.get("/scores")
    def scores():
        weeks = request.args.get("weeks", "12")
        if weeks not in ("12", "26", "52", "156"):
            abort(400, "Choose 12, 26, 52 or 156 weeks")
        current = week_start(datetime.now(timezone.utc))
        dates = [current - timedelta(weeks=offset) for offset in reversed(range(int(weeks)))]
        records = score_store.weekly_history()
        lookup = {(row["company_id"], row["week_start"]): row for row in records}
        scored_ids = {r["company_id"] for r in records if r["score"] is not None}
        return render_template("scores.html", companies=[c for c in db.list_companies(None) if c["active"] or c["id"] in scored_ids], dates=dates,
                               lookup=lookup, labels=LABELS, rubric=RUBRIC, materiality=MATERIALITY, weeks=weeks,
                               version=VERSION, current=current)

    @app.post("/scores/refresh")
    def refresh_scores():
        result = score_store.refresh_scores()
        flash(f"Scoring refreshed: {result['articles_updated']} articles and {result['weeks_updated']} weekly records updated. No news requests made.", "success")
        return redirect(url_for("scores"))

    @app.get("/api/scores/weekly")
    def weekly_scores_api():
        supplied = request.args.get("company_id")
        try:
            company_id = int(supplied) if supplied is not None else None
        except ValueError:
            abort(400, "Invalid company ID")
        if company_id is not None and not db.get_company(company_id):
            abort(404)
        rows = score_store.weekly_history(company_id)
        return jsonify({"timezone": "Europe/Moscow", "week_starts": "Monday", "version": VERSION,
                        "weeks": [{"company_id": r["company_id"], "company_name": r["name"],
                                   "week_start": r["week_start"].isoformat(), **r["metrics"],
                                   "cutoff_date": (r["week_start"] + timedelta(days=7)).isoformat(),
                                   "updated_at": r["updated_at"].isoformat()} for r in rows]})

    @app.get("/scores/<int:company_id>/<start>")
    def score_week(company_id: int, start: str):
        try:
            date_start = date.fromisoformat(start)
        except ValueError:
            abort(404)
        company = db.get_company(company_id)
        if not company:
            abort(404)
        record, articles, history = score_store.week_detail(company_id, date_start)
        if not record:
            abort(404)
        return render_template("score_week.html", company=company, record=record, articles=articles,
                               history=history, labels=LABELS, review_note=headline_review.review_note)

    @app.route("/articles/<int:result_id>/score", methods=["GET", "POST"])
    def article_score(result_id: int):
        article = score_store.get_article(result_id)
        if not article:
            abort(404)
        if request.method == "POST":
            try:
                score_store.review_score(result_id, int(request.form.get("score", "")),
                                         request.form.get("included") == "yes", request.form.get("reason", ""))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("article_score", result_id=result_id))
            flash("Article score saved. Weekly history updated and the decision recorded.", "success")
            return redirect(url_for("article_score", result_id=result_id))
        return render_template("article_score.html", article=article, history=score_store.article_history(result_id),
                               labels=LABELS, rubric=RUBRIC, materiality=MATERIALITY, lookup_title=original_title(article), **verification_store.details(result_id))

    @app.post("/articles/<int:result_id>/verify")
    def verify_article(result_id):
        try:
            verification_store.enqueue(result_id, request.form.get("source_url", "").strip())
            flash("Source check queued. RA reuses cached sources and tries publisher recovery when needed.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("article_score", result_id=result_id))

    @app.post("/articles/<int:result_id>/group")
    def article_group(result_id):
        try:
            target = int(request.form.get("target_id") or "0")
            verification_store.review_group(result_id, request.form.get("action"), target, request.form.get("reason", ""))
            flash("Event grouping saved and weekly history updated.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("article_score", result_id=result_id))

    @app.post("/runs/<int:run_id>/verify")
    def verify_run(run_id):
        try:
            count = verification_store.enqueue_run(run_id)
            flash(f"Queued {count} source checks (up to eight new articles per company).", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("run_detail", run_id=run_id, view="review"))

    @app.post("/companies")
    def add_company():
        try:
            data = form_company(request.form)
            existing = db.find_company(data["name"], data["country_registration"])
            if existing:
                flash("This company is already in the database. Edit or restore its existing record.", "warning")
                return redirect(url_for("dashboard"))
            intake_id = db.stage_company(data, source="manual")
            if intake_id:
                intake_worker.enqueue(intake_id)
                flash(f"Preliminary research started for {data['name']}. Review the draft before saving.", "success")
                return redirect(url_for("review_intake", intake_id=intake_id))
            flash("This exact draft has already been submitted. Review it in the intake queue.", "warning")
        except (ValueError, UniqueViolation) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.get("/companies/<int:company_id>/edit")
    def edit_company(company_id: int):
        company = db.get_company(company_id)
        if not company:
            abort(404)
        return render_template(
            "edit.html", company=company,
            search_queries=[query for query, _ in company_search_plan(company)], provider_name=provider_name(company),
            identity_evidence=db.get_identity_evidence(
                company_id, company["registration_number"], company["tax_id"], company["country_registration"]),
        )

    @app.post("/companies/<int:company_id>/edit")
    def update_company(company_id: int):
        if not db.get_company(company_id):
            abort(404)
        try:
            data = form_company(request.form)
            other = db.find_company(data["name"], data["country_registration"])
            if other and other["id"] != company_id:
                raise ValueError("Another record already has this name and registration country")
            db.save_company(data, company_id)
            disclosures.schedule(company_id)
            keywords.schedule()
            model_store.schedule_countries([data["country_registration"]])
            flash("Company details saved.", "success")
            return redirect(url_for("dashboard"))
        except (ValueError, UniqueViolation) as exc:
            flash(str(exc), "error")
            return redirect(url_for("edit_company", company_id=company_id))

    @app.post("/companies/<int:company_id>/remove")
    def remove_company(company_id: int):
        if not db.set_active(company_id, False):
            abort(404)
        flash("Company removed from the active watchlist. Its search history was kept.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/companies/<int:company_id>/disclosures")
    def discover_disclosures(company_id):
        if not db.get_company(company_id):
            abort(404)
        disclosures.schedule(company_id, force=True)
        flash("Disclosure discovery queued. Saved news searches and scores remain unchanged.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/companies/<int:company_id>/restore")
    def restore_company(company_id: int):
        if not db.set_active(company_id, True):
            abort(404)
        keywords.schedule()
        disclosures.schedule(company_id)
        flash("Company restored to the active watchlist.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/companies/import")
    def import_companies():
        uploaded = request.files.get("workbook")
        try:
            if uploaded and uploaded.filename:
                if not uploaded.filename.lower().endswith(".xlsx"):
                    raise ValueError("Choose an .xlsx workbook")
                source = uploaded.stream
            else:
                source = ROOT / "companies.xlsx"
            added, skipped = stage_workbook(source)
            for draft in db.list_intake(("queued",)):
                intake_worker.enqueue(draft["id"])
            flash(f"Workbook intake: {added} drafts staged for research and review, {skipped} identical rows already staged. No watchlist records were saved.", "success")
        except (ValueError, OSError, UniqueViolation) as exc:
            flash(f"Workbook import failed: {exc}", "error")
        return redirect(url_for("dashboard"))

    def begin_search(scope: str, subjects: list[dict], *, custom_range=False):
        reviewer = headline_review.readiness()
        if not reviewer["ready"]:
            flash(reviewer["message"], "warning")
        try:
            window = {key: datetime.fromisoformat(request.form[key]) if request.form.get(key) else None
                      for key in ("window_start", "window_end")}
            if request.form.get("start_date") and (not custom_range or not request.form.get("end_date")):
                raise ValueError("Choose both start and end dates for a single-company search.")
            if request.form.get("end_date"):
                if any(window.values()) or request.form.get("cutoff_date"):
                    raise ValueError("Choose an inclusive end date or another date range, not both.")
                now = datetime.now(WEEK_TIMEZONE)
                selected = date.fromisoformat(request.form["end_date"])
                if selected > now.date():
                    raise ValueError("Search end date cannot be in the future.")
                end_day = datetime.combine(selected, datetime.min.time(), WEEK_TIMEZONE)
                start = (datetime.combine(date.fromisoformat(request.form["start_date"]), datetime.min.time(), WEEK_TIMEZONE)
                         if request.form.get("start_date") else end_day - timedelta(days=6))
                if start > end_day:
                    raise ValueError("Search start date must be on or before the end date.")
                window = {"window_start": start,
                          "window_end": min(end_day + timedelta(days=1) - timedelta(microseconds=1), now)}
            if request.form.get("cutoff_date"):
                if any(window.values()):
                    raise ValueError("Choose a cutoff date or explicit timestamps, not both.")
                cutoff = datetime.combine(date.fromisoformat(request.form["cutoff_date"]), datetime.min.time(), WEEK_TIMEZONE)
                window = {"window_start": cutoff - timedelta(days=7), "window_end": cutoff - timedelta(microseconds=1)}
            if (not custom_range and all(window.values())
                    and all(value.tzinfo is not None for value in window.values())
                    and window["window_end"] - window["window_start"] > timedelta(days=7)):
                raise ValueError("Watchlist searches cover up to seven days. Use a single-company search for a longer range.")
            run_id = db.create_run(scope, subjects, **window)
        except ValueError as exc:
            abort(400, str(exc))
        worker.enqueue(run_id)
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/search/all")
    def search_all():
        filters = {(branch, value) for branch in ("country", "sector")
                   if (value := request.form.get(branch, "").strip())}
        subjects = [db.company_snapshot(item) for item in db.list_companies(True)
                    if filters.issubset(topic_links(item))]
        if not subjects:
            flash("No active watchlist company matches this selection.", "warning")
            return redirect(url_for("dashboard"))
        return begin_search("selected" if filters else "all", subjects)

    @app.post("/search/selected")
    def search_selected():
        try:
            company_id = int(request.form.get("company_id", ""))
        except ValueError:
            abort(400, "Choose a company")
        company = db.get_company(company_id)
        if not company or not company["active"]:
            abort(404)
        return begin_search("selected", [db.company_snapshot(company)], custom_range=True)

    @app.post("/search/one-off")
    def search_one_off():
        abort(410, "Add the company to the watchlist, then use the individual company search.")

    @app.get("/runs/<int:run_id>", strict_slashes=False)
    def run_detail(run_id: int):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        all_results = classify_results(run, db.get_results(run_id))
        view = request.args.get("view", "risk" if (run.get("search_settings") or {}).get("adverse_only") else "events")
        if view not in ("events", "developments", "risk", "review", "older", "background", "scored", "exposure", "governance", "factors", "all"):
            abort(400, "Unknown lead view")
        ordinary = [item for item in all_results if (item.get("score") or 0) <= 0]
        results = [item for item in (all_results if view == "all" else ordinary) if view == "all" or item["relevance"] == view
                   or (view == "events" and event_analysis.candidate(item))
                   or (view == "scored" and item.get("score_included"))
                   or (view == "exposure" and item.get("exposure_paths"))
                   or (view == "developments" and item["credit_developments"])
                   or (view == "factors" and item.get("risk_factor_review", {}).get("matches"))
                   or (view == "governance" and item["governance"])]
        results.sort(key=lambda item: (item.get("published_at") is None, item.get("published_at"), item["id"]))
        visible_ids = {item["id"] for item in results}
        return render_template(
            "run.html", run=run, results=results, errors=db.get_errors(run_id), view=view,
            events=event_analysis.run_events(run, all_results),
            company_labels={s["name"]: company_label(s) for s in run["subjects"]},
            country_leads=[item for item in ordinary if any(p.startswith("Country ·") for p in item.get("exposure_paths", []))],
            counts={"events": sum(event_analysis.candidate(item) for item in ordinary), **{key: sum(item["relevance"] == key for item in ordinary)
                    for key in ("risk", "review", "older", "background")},
                    "developments": sum(bool(item["credit_developments"]) for item in ordinary),
                    "governance": sum(bool(item["governance"]) for item in ordinary),
                    "factors": sum(bool(item.get("risk_factor_review", {}).get("matches")) for item in ordinary),
                    "exposure": sum(bool(item.get("exposure_paths")) for item in ordinary),
                    "scored": sum(bool(item.get("score_included")) for item in ordinary)},
            rosters=governance_roster.observations(run_id) if view == "governance" else [],
            exposure_checks=db.exposure_coverage(run_id) if view == "exposure" else [],
            score_labels=LABELS, verification_jobs=verification_store.job_summary(run_id),
            reviewer=headline_review.readiness(),
            search_progress=worker.search_progress(run), all_count=len(all_results),
            selected_count=sum(bool(item["report_selected"]) for item in all_results),
            hidden_selected=[item["id"] for item in all_results
                             if item["report_selected"] and item["id"] not in visible_ids],
        )

    @app.post("/runs/<int:run_id>/retry-events")
    def retry_events(run_id):
        with db.connect() as conn:
            conn.execute("UPDATE search_runs SET event_analysis_status='pending' WHERE id=%s AND event_analysis_status='failed'", (run_id,))
        event_analysis._wake.set()
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/api/runs/<int:run_id>/status")
    def run_status(run_id: int):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        payload = {key: run[key] for key in ("status", "total_companies", "completed_companies", "result_count", "error_count")}
        return jsonify(**payload, event_analysis_status=run.get("event_analysis_status", "pending"), discovery=worker.search_progress(run), verification=verification_store.job_summary(run_id),
                       reviewer=headline_review.readiness())

    @app.get("/runs/<int:run_id>/company-report")
    def download_company_report(run_id):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        if run["status"] in ("queued", "running"):
            abort(409, "The search is still running")
        company = request.args.get("company", "")
        subject = next((s for s in run["subjects"] if str(s.get("id") or "one-off") == company), None)
        fmt = request.args.get("format")
        if subject is None or fmt not in ("pdf", "xlsx"):
            abort(404)
        report = load_report(run, subject)
        if fmt == "pdf":
            content, mime = company_pdf(report), "application/pdf"
        else:
            content, mime = company_xlsx(report), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return send_file(content, mimetype=mime, as_attachment=True,
                         download_name=f"ra-company-{company}-run-{run_id}.{fmt}")

    @app.get("/runs/<int:run_id>/download.<format>")
    def download_run(run_id: int, format: str):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        if run["status"] in ("queued", "running"):
            abort(409, "The search is still running")
        results, errors = db.get_results(run_id), db.get_errors(run_id)
        if format == "xlsx":
            content, mime = make_xlsx(run, results, errors), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif format == "pdf":
            content, mime = make_pdf(run, results, errors), "application/pdf"
        else:
            abort(404)
        return send_file(content, mimetype=mime, as_attachment=True, download_name=f"ra-search-{run_id}.{format}")

    @app.post("/runs/<int:run_id>/review")
    def review_run(run_id: int):
        if not db.get_run(run_id):
            abort(404)
        try:
            selected_ids = [int(value) for value in request.form.getlist("selected_ids")]
            edits = {}
            for result in db.get_results(run_id):
                title_key, publisher_key = f"title_en_{result['id']}", f"publisher_en_{result['id']}"
                if title_key in request.form and publisher_key in request.form:
                    edits[result["id"]] = (
                        request.form[title_key].strip(), request.form[publisher_key].strip()
                    )
            scores = {int(k[6:]): int(value) for k, value in request.form.items() if k.startswith("score_") and value}
            count = db.set_report_selection(run_id, selected_ids, edits, scores, save_all=request.form.get("action") == "all")
            flash(f"Saved {count} leads. Weekly scores now reflect the selected, eligible event evidence.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/runs/<int:run_id>/report.<format>")
    def download_selected_report(run_id: int, format: str):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        if run["status"] in ("queued", "running"):
            abort(409, "The search is still running")
        results = [item for item in db.get_results(run_id) if item["report_selected"]]
        if not results:
            abort(409, "Select and save at least one article before building a report")
        errors = db.get_errors(run_id)
        if format == "xlsx":
            content, mime = make_xlsx(run, results, errors, selected=True), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif format == "pdf":
            content, mime = make_pdf(run, results, errors, selected=True), "application/pdf"
        else:
            abort(404)
        return send_file(content, mimetype=mime, as_attachment=True, download_name=f"ra-selected-report-{run_id}.{format}")

    @app.post("/runs/<int:run_id>/save-company")
    def save_one_off(run_id: int):
        run = db.get_run(run_id)
        if not run or run["scope"] != "one_off":
            abort(404)
        data = run["subjects"][0]
        intake_id = db.stage_company(data, source="one_off")
        if intake_id:
            intake_worker.enqueue(intake_id)
            flash("The one-time subject is staged for identity review.", "success")
            return redirect(url_for("review_intake", intake_id=intake_id))
        flash("This exact draft is already in the intake history.", "warning")
        return redirect(url_for("dashboard"))

    @app.get("/intake/<int:intake_id>", strict_slashes=False)
    def review_intake(intake_id: int):
        draft = db.get_intake(intake_id)
        if not draft:
            abort(404)
        if draft["status"] == "saved" and draft["company_id"]:
            return redirect(url_for("edit_company", company_id=draft["company_id"]))
        if draft["status"] == "dismissed":
            return redirect(url_for("dashboard"))
        matched = db.get_company(draft["matched_company_id"]) if draft["matched_company_id"] else None
        registry = preliminary.MANUAL_REGISTRIES.get(preliminary.country_code(draft["draft_data"]["country_registration"]))
        return render_template("intake.html", draft=draft, company=draft["draft_data"],
                               matched=matched, registry=registry)

    @app.get("/api/intake/<int:intake_id>/status")
    def intake_status(intake_id: int):
        draft = db.get_intake(intake_id)
        if not draft:
            abort(404)
        return jsonify({"status": draft["status"]})

    @app.post("/intake/<int:intake_id>/confirm")
    def confirm_intake(intake_id: int):
        try:
            if request.form.get("identity_checked") != "yes":
                raise ValueError("Double-check the legal name, country, registration number and tax ID, then tick the review box")
            data = form_company(request.form)
            company_id, updated = db.confirm_intake(intake_id, data)
            disclosures.schedule(company_id)
            keywords.schedule()
            model_store.schedule_countries([data["country_registration"]])
            flash(f"{'Updated' if updated else 'Added'} {data['name']} after review. It is now available for searches.", "success")
            return redirect(url_for("edit_company", company_id=company_id))
        except (ValueError, UniqueViolation) as exc:
            flash(str(exc), "error")
            return redirect(url_for("review_intake", intake_id=intake_id))

    @app.post("/intake/<int:intake_id>/dismiss")
    def dismiss_intake(intake_id: int):
        if not db.dismiss_intake(intake_id):
            abort(409, "Wait for research to finish before dismissing")
        flash("Draft dismissed; no company was saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/intake/<int:intake_id>/research")
    def retry_intake(intake_id: int):
        if not db.retry_intake(intake_id):
            abort(409, "This draft cannot be researched again")
        intake_worker.enqueue(intake_id)
        return redirect(url_for("review_intake", intake_id=intake_id))

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_schema()
    keywords.reload_rules()
    risk_factors.reload()
    keywords.schedule()
    score_store.refresh_scores()
    intake_worker.start()
    from . import rss_recovery
    rss_recovery.start()
    worker.start_worker()
    verification_store.start()
    event_analysis.start()
    disclosures.schedule()
    model_store.schedule_countries([company["country_registration"] for company in db.list_companies(True)])
    app = create_app()
    port = int(os.environ.get("RA_PORT", "8765"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
