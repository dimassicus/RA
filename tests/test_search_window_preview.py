"""Date preview and browser validation without starting searches."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_date_preview_supports_custom_ranges_and_preserves_watchlist_week():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the date preview regression')
    script = Path('ra/static/search-window.js').read_text()
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = require('node:fs').readFileSync(0, 'utf8');
function field() {
  return {value:'', min:'', max:'2026-11-10', required:false, events:{},
    get validity() { return {valid: this.value ? this.value <= this.max && (!this.min || this.value >= this.min) : !this.required}; },
    addEventListener(event, callback) {this.events[event] = callback;}};
}
function form(custom) {
  const end = field(), start = custom ? field() : null, output = {textContent:''};
  end.form = {querySelector: selector => selector === '[data-search-start]' ? start : output};
  return {end, start, output};
}
const weekly = form(false), selected = form(true), once = form(true);
vm.runInNewContext(code, {document:{querySelectorAll:() => [weekly.end, selected.end, once.end]}, Date});
assert.match(selected.output.textContent, /rolling seven days/);
weekly.end.value='2026-11-10'; weekly.end.events.input();
assert.match(weekly.output.textContent, /4 November 2026 – 10 November 2026/);
for (const {start,end,output} of [selected,once]) {
  start.value='2024-01-01'; start.events.input();
  assert.equal(end.required,true); assert.equal(start.required,true);
  assert.equal(output.textContent,'Choose both start and end dates.');
  end.value='2026-11-10'; end.events.input();
  assert.match(output.textContent,/1 January 2024 – 10 November 2026/);
  assert.match(output.textContent,/day is incomplete/);
  start.value='2026-11-04'; end.value='2026-11-04'; start.events.input();
  assert.match(output.textContent,/4 November 2026 – 4 November 2026/);
  start.value='2026-11-05'; start.events.input();
  assert.equal(end.validity.valid,false); assert.match(output.textContent,/on or before/);
  start.value=''; end.value='2026-11-04'; start.events.input();
  assert.match(output.textContent,/Choose both/);
  start.value=''; end.value=''; end.events.input();
  assert.equal(start.required,false); assert.equal(end.required,false);
  assert.match(output.textContent,/rolling seven days/);
}
'''
    subprocess.run([node, '-e', harness], input=script, text=True, capture_output=True, check=True)
