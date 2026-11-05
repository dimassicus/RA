const leadTable = document.getElementById('lead-table');
const leadBody = leadTable.tBodies[0];
const leadRows = Array.from(leadBody.rows);
const columnHeaders = Array.from(leadTable.querySelectorAll('th[data-column]'));

function compareValues(key, a, b, direction) {
  if (key === 'company') return direction * a.localeCompare(b);
  if (a === 'pending' || b === 'pending') {
    return Number(a === 'pending') - Number(b === 'pending');
  }
  return direction * (Number(a) - Number(b));
}

function filterLeads() {
  const filters = columnHeaders.map(header => [header.dataset.column, header.querySelector('select').value]);
  let visible = 0;
  for (const row of leadRows) {
    row.hidden = filters.some(([key, value]) => value && row.dataset[key] !== value);
    if (!row.hidden) visible++;
  }
  document.getElementById('lead-count').textContent = `Showing ${visible} of ${leadRows.length} leads in this view.`;
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
