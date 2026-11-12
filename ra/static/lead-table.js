for (const section of document.querySelectorAll('.lead-review-table')) {
  const leadTable = section.querySelector('table');
  const leadBody = leadTable.tBodies[0];
  const leadRows = Array.from(leadBody.rows);
  const columnHeaders = Array.from(leadTable.querySelectorAll('th[data-column]'));

  const cueFilter = section.querySelector('[data-cue-filter]');
  const rowKeywords = new Map(leadRows.map(row => [row, JSON.parse(row.dataset.keywords)]));
  for (const [field, title] of [['direction', 'Sentiment'], ['term', 'Keyword']]) {
    const group = document.createElement('optgroup');
    group.label = title;
    const values = new Set([...rowKeywords.values()].flat().map(hit => hit[field]));
    for (const value of [...values].sort((a, b) => a.localeCompare(b))) {
      group.append(new Option(value, field + ':' + value));
    }
    cueFilter.append(group);
  }
  cueFilter.addEventListener('change', filterLeads);

  function compareValues(key, a, b, direction) {
    if (key === 'company') return direction * a.localeCompare(b);
    if (a === 'pending' || b === 'pending') {
      return Number(a === 'pending') - Number(b === 'pending');
    }
    return direction * (Number(a) - Number(b));
  }

  function filterLeads() {
    const filters = columnHeaders.map(header => [header.dataset.column, header.querySelector('select').value]);
    const [field, keyword] = cueFilter.value.split(':');
    let visible = 0;
    for (const row of leadRows) {
      row.hidden = (cueFilter.value && !rowKeywords.get(row).some(hit => hit[field] === keyword)) ||
        filters.some(([key, value]) => value && row.dataset[key] !== value);
      if (!row.hidden) visible++;
    }
    section.querySelector('[data-lead-count]').textContent = `Showing ${visible} of ${leadRows.length} leads in this view.`;
  }

  for (const header of columnHeaders) {
    const key = header.dataset.column;
    const select = header.querySelector('select');
    const values = Array.from(new Set(leadRows.map(row => row.dataset[key])));
    for (const value of values.sort((a, b) => compareValues(key, a, b, 1))) {
      let label = value;
      if (key === 'score' && value === 'pending') label = 'Scoring pending';
      if (key === 'score' && Number(value) > 0) label = '+' + value;
      select.add(new Option(label, value));
    }
    select.addEventListener('change', filterLeads);
    header.querySelector('button').addEventListener('click', () => {
      const ascending = header.getAttribute('aria-sort') !== 'ascending';
      for (const other of columnHeaders) {
        other.removeAttribute('aria-sort');
        other.querySelector('button span').textContent = '↕';
      }
      header.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
      header.querySelector('button span').textContent = ascending ? '↑' : '↓';
      const sorted = leadRows.slice().sort((a, b) =>
        compareValues(key, a.dataset[key], b.dataset[key], ascending ? 1 : -1));
      leadBody.append(...sorted);
    });
  }

  filterLeads();

  const eventCheck = section.closest('.event-choice')?.querySelector('[data-event-accept]');
  const leadChecks = leadRows.map(row => row.querySelector('.lead-check'));
  function syncEventCheck() {
    if (!eventCheck) return;
    const selected = leadChecks.filter(check => check.checked).length;
    eventCheck.checked = selected === leadChecks.length;
    eventCheck.indeterminate = selected > 0 && selected < leadChecks.length;
  }
  if (eventCheck) {
    syncEventCheck();
    eventCheck.parentElement.hidden = false;
    eventCheck.addEventListener('change', () => {
      for (const check of leadChecks) check.checked = eventCheck.checked;
    });
    leadBody.addEventListener('change', syncEventCheck);
  }

  for (const button of section.querySelectorAll('[data-select-visible], [data-clear-visible]')) {
    button.addEventListener('click', () => {
      for (const row of leadRows) {
        if (!row.hidden) row.querySelector('.lead-check').checked = button.hasAttribute('data-select-visible');
      }
      syncEventCheck();
    });
  }
}
