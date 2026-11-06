const saveAll = document.getElementById('save-all');
const list = saveAll.closest('section');
const statusText = document.getElementById('save-all-status');
saveAll.hidden = false;

saveAll.addEventListener('click', async () => {
  const data = new URLSearchParams();
  for (const row of list.querySelectorAll('tbody tr')) {
    const fields = row.querySelector('input[name=english]').form.elements;
    const id = fields.id.value;
    const previousScope = [...fields.scope.options].find(option => option.defaultSelected).value;
    let changed = fields.english.value !== fields.previous.value ||
      fields.scope.value !== previousScope || fields.enabled.checked !== fields.enabled.defaultChecked;
    for (const area of row.querySelectorAll('textarea')) {
      const translation = area.form.elements;
      if (area.value === translation.original.value) continue;
      const code = translation.language.value;
      data.append(id + '.language', code);
      data.set(id + '.' + code + '.original', translation.original.value);
      data.set(id + '.' + code + '.translation', area.value);
      changed = true;
    }
    if (!changed) continue;
    data.set('csrf_token', fields.csrf_token.value);
    data.append('id', id);
    data.set(id + '.previous', fields.previous.value);
    data.set(id + '.previous_scope', previousScope);
    data.set(id + '.previous_enabled', fields.enabled.defaultChecked);
    data.set(id + '.english', fields.english.value);
    data.set(id + '.scope', fields.scope.value);
    data.set(id + '.enabled', fields.enabled.checked);
  }
  if (!data.has('id')) {
    statusText.textContent = 'No changes to save.';
    return;
  }

  statusText.textContent = 'Saving edits and checking translations…';
  const controls = list.querySelectorAll('input, select, textarea, button');
  controls.forEach(control => { control.disabled = true; });
  try {
    const response = await fetch(saveAll.dataset.url, {method: 'POST', body: data});
    const result = await response.json();
    if (!response.ok) {
      statusText.textContent = result.error || 'Could not save edits.';
      return;
    }
    window.location.reload();
  } catch {
    statusText.textContent = 'Could not confirm the save. Your edits are still on this page.';
  } finally {
    controls.forEach(control => { control.disabled = false; });
  }
});
