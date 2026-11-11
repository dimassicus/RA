"""Exercise rendered polling code without a server, database or model calls."""
from datetime import datetime, timezone
import json
import re
import shutil
import subprocess

from flask import render_template
import pytest

from ra.app import create_app
from ra.scoring import LABELS


def run_page(status="completed", theme_status="inconclusive"):
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    checks = dict(total=2, completed=1, queued=1, running=0, failed=0, attention=1,
                  percent=50, translation_percent=50, reviewed=1, translation_total=2,
                  review_failed=1, missing=0, review_attention=0, review_pending=0,
                  review_exhausted=1, revision="before")
    run = dict(id=15, scope="selected", status=status, total_companies=1,
               completed_companies=1, result_count=2, error_count=0, created_at=now,
               subjects=[dict(name="Acme", id=1)])
    article = dict(id=1, published_at=now, subject_name="Acme", title="Company news",
                   url="https://example.test/news", language="en", title_en="Company news",
                   publisher="News", score=0, score_status="needs_review", score_included=False,
                   score_reason="Review needed", verification={"status": "needs_review"},
                   risk_factor_review={"status": theme_status, "matches": [], "coverage": "headline only"},
                   keyword_hits=[], source_kind="news", report_selected=False)
    with create_app().test_request_context("/runs/15?view=all"):
        html = render_template("run.html", run=run, verification_jobs=checks,
                               reviewer={"message": "Ready"}, search_progress=None,
                               score_labels=LABELS, results=[article], errors=[], view="all", counts={}, all_count=2,
                               selected_count=0, hidden_selected=[], rosters=[], exposure_checks=[])
    return html, checks


@pytest.mark.parametrize("status", ["inconclusive", "incomplete"])
def test_run_page_explains_failed_theme_and_exhausted_headline_checks(status):
    html, _ = run_page(theme_status=status)
    assert f"Theme assessment: {status} · headline only" in html
    assert "Some themes could not be assessed" in html
    assert "Automatic headline retries stopped: 1 unresolved reviews exhausted their retries." in html


@pytest.mark.parametrize("edit", [None, "text", "checkbox", "select"])
@pytest.mark.parametrize("initial_status", ["running", "completed"])
def test_polling_refreshes_results_but_preserves_unsaved_controls(edit, initial_status):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the browser polling regression")
    html, checks = run_page(status=initial_status)
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    payload = dict(status="completed", completed_companies=1, discovery=None,
                   reviewer={"message": "Ready"},
                   verification={**checks, "completed": 2, "queued": 0, "percent": 100, "revision": "after"})
    harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {script, payload, edit} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const controls = [{type: 'text', value: 'Original'}, {type: 'checkbox', checked: false},
                  {type: 'select', value: 'First'}];
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {hidden: true, textContent: '', value: 0});
  return elements.get(id);
};
let reloads = 0;
const context = vm.createContext({
  document: {hidden: false, querySelectorAll: () => controls, getElementById: element},
  window: {location: {reload: () => reloads++}},
  fetch: async () => ({ok: true, json: async () => payload}), setTimeout: () => {}, Date
});
vm.runInContext(script, context);
if (edit === 'text') controls[0].value = 'Corrected headline';
if (edit === 'checkbox') controls[1].checked = true; // Bulk buttons change this without an event.
if (edit === 'select') controls[2].value = 'Second';
(async () => {
  await context.updateChecks();
  assert.equal(reloads, edit ? 0 : 1);
  if (edit) {
    assert.equal(element('results-refresh').hidden, false);
    assert.equal(element('publisher-bar').value, 100);
    assert.match(element('translation-retries').textContent, /retries stopped/);
    assert.doesNotMatch(element('check-live').textContent, /interrupted/);
    controls[0].value = 'Original'; controls[1].checked = false; controls[2].value = 'First';
    await context.updateChecks();
    assert.equal(reloads, 1);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", harness], input=json.dumps(dict(script=script, payload=payload, edit=edit)),
                   text=True, capture_output=True, check=True)
