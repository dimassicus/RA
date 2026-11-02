"""Local HTML interface for the RA watchlist and search pilot."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from psycopg.errors import UniqueViolation

from . import db, intake_worker, worker
from .importer import stage_workbook
from .reports import make_pdf, make_xlsx


ROOT = Path(__file__).resolve().parent.parent
MAX_FIELD = {"name": 300, "short_name": 300, "country_registration": 150,
             "registration_number": 150, "legal_address": 1500, "comment": 5000}


def form_company(form) -> dict:
    data = {}
    for field in db.FIELDS:
        value = str(form.get(field, "")).strip()
        if len(value) > MAX_FIELD.get(field, 1500):
            raise ValueError(f"{field.replace('_', ' ').title()} is too long")
        data[field] = value
    if not data["name"] or not data["country_registration"]:
        raise ValueError("Company name and country of registration are required")
    return data


def create_app() -> Flask:
    app = Flask(__name__)
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
            if not expected or not hmac.compare_digest(supplied, expected):
                abort(400, "Invalid form token. Refresh the page and try again.")

    @app.get("/")
    def dashboard():
        return render_template(
            "index.html", companies=db.list_companies(True),
            archived=db.list_companies(False), runs=db.list_runs(), intake=db.list_intake(),
        )

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
            identity_evidence=db.get_identity_evidence(company_id, company["registration_number"], company["country_registration"]),
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

    @app.post("/companies/<int:company_id>/restore")
    def restore_company(company_id: int):
        if not db.set_active(company_id, True):
            abort(404)
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
            intake_worker.start()
            flash(f"Workbook intake: {added} drafts staged for research and review, {skipped} identical rows already staged. No watchlist records were saved.", "success")
        except (ValueError, OSError, UniqueViolation) as exc:
            flash(f"Workbook import failed: {exc}", "error")
        return redirect(url_for("dashboard"))

    def begin_search(scope: str, subjects: list[dict]):
        run_id = db.create_run(scope, subjects)
        worker.enqueue(run_id)
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/search/all")
    def search_all():
        subjects = [db.company_snapshot(item) for item in db.list_companies(True)]
        if not subjects:
            flash("Add a company to the watchlist first.", "warning")
            return redirect(url_for("dashboard"))
        return begin_search("all", subjects)

    @app.post("/search/selected")
    def search_selected():
        try:
            company_id = int(request.form.get("company_id", ""))
        except ValueError:
            abort(400, "Choose a company")
        company = db.get_company(company_id)
        if not company or not company["active"]:
            abort(404)
        return begin_search("selected", [db.company_snapshot(company)])

    @app.post("/search/one-off")
    def search_one_off():
        try:
            subject = form_company(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        return begin_search("one_off", [subject])

    @app.get("/runs/<int:run_id>", strict_slashes=False)
    def run_detail(run_id: int):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        return render_template(
            "run.html", run=run, results=db.get_results(run_id), errors=db.get_errors(run_id),
        )

    @app.get("/api/runs/<int:run_id>/status")
    def run_status(run_id: int):
        run = db.get_run(run_id)
        if not run:
            abort(404)
        return jsonify({key: run[key] for key in
                        ("status", "total_companies", "completed_companies", "result_count", "error_count")})

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
            db.set_report_selection(run_id, selected_ids, edits)
            flash(f"Saved {len(set(selected_ids))} leads and any English corrections for the selected report.", "success")
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
        matched = db.get_company(draft["matched_company_id"]) if draft["matched_company_id"] else None
        return render_template("intake.html", draft=draft, company=draft["draft_data"], matched=matched)

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
                raise ValueError("Double-check the legal name, country and identifier, then tick the review box")
            data = form_company(request.form)
            company_id, updated = db.confirm_intake(intake_id, data)
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
    db.init_schema()
    intake_worker.start()
    worker.start_worker()
    app = create_app()
    port = int(os.environ.get("RA_PORT", "8765"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
