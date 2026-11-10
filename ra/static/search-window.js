document.querySelectorAll('[data-search-end]').forEach(input => {
  const output = input.form.querySelector('[data-search-window]');
  const format = date => date.toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'
  });
  const update = () => {
    if (!input.value || !input.validity.valid) {
      output.textContent = input.value ? 'Choose today or an earlier date.' :
        'Blank: rolling seven days ending at launch time.';
      return;
    }
    const end = new Date(input.value + 'T00:00:00Z');
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - 6);
    output.textContent = `Search dates: ${format(start)} – ${format(end)}, inclusive (Moscow time).` +
      (input.value === input.max ? ' Today: only through launch time; the day is incomplete.' : '');
  };
  input.addEventListener('input', update);
  update();
});
