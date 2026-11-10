document.querySelectorAll('[data-search-end]').forEach(input => {
  const output = input.form.querySelector('[data-search-window]');
  const startInput = input.form.querySelector('[data-search-start]');
  const format = date => date.toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'
  });
  const update = () => {
    if (startInput) {
      startInput.required = input.required = Boolean(startInput.value || input.value);
      input.min = startInput.value;
      if (startInput.required && (!startInput.value || !input.value)) {
        output.textContent = 'Choose both start and end dates.';
        return;
      }
      if (!startInput.validity.valid || !input.validity.valid) {
        output.textContent = 'Choose a start date on or before the end date, with neither date in the future.';
        return;
      }
    }
    if (!input.value || !input.validity.valid) {
      output.textContent = input.value ? 'Choose today or an earlier date.' :
        'Blank: rolling seven days ending at launch time.';
      return;
    }
    const end = new Date(input.value + 'T00:00:00Z');
    const start = startInput ? new Date(startInput.value + 'T00:00:00Z') : new Date(end);
    if (!startInput) start.setUTCDate(start.getUTCDate() - 6);
    output.textContent = `Search dates: ${format(start)} – ${format(end)}, inclusive (Moscow time).` +
      (input.value === input.max ? ' Today: only through launch time; the day is incomplete.' : '');
  };
  input.addEventListener('input', update);
  if (startInput) startInput.addEventListener('input', update);
  update();
});
