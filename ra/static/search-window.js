document.querySelectorAll('[data-search-end]').forEach(input => {
  const russian = document.documentElement?.lang === 'ru';
  const output = input.form.querySelector('[data-search-window]');
  const startInput = input.form.querySelector('[data-search-start]');
  const format = date => date.toLocaleDateString(russian ? 'ru-RU' : 'en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'
  });
  const update = () => {
    if (startInput) {
      startInput.required = input.required = Boolean(startInput.value || input.value);
      input.min = startInput.value;
      if (startInput.required && (!startInput.value || !input.value)) {
        output.textContent = russian ? 'Укажите обе даты: начало и конец периода.' : 'Choose both start and end dates.';
        return;
      }
      if (!startInput.validity.valid || !input.validity.valid) {
        output.textContent = russian ? 'Дата начала не должна быть позже даты окончания. Обе даты должны быть не позднее сегодняшнего дня.' :
          'Choose a start date on or before the end date, with neither date in the future.';
        return;
      }
    }
    if (!input.value || !input.validity.valid) {
      if (input.value) {
        output.textContent = russian ? 'Укажите сегодняшнюю или более раннюю дату.' : 'Choose today or an earlier date.';
      } else {
        output.textContent = russian ? 'Без дат: последние семь дней до момента запуска.' : 'Blank: rolling seven days ending at launch time.';
      }
      return;
    }
    const end = new Date(input.value + 'T00:00:00Z');
    const start = startInput ? new Date(startInput.value + 'T00:00:00Z') : new Date(end);
    if (!startInput) start.setUTCDate(start.getUTCDate() - 6);
    output.textContent = russian
      ? `Период поиска: ${format(start)} – ${format(end)} включительно (московское время).`
      : `Search dates: ${format(start)} – ${format(end)}, inclusive (Moscow time).`;
    if (input.value === input.max) {
      output.textContent += russian ? ' За сегодня учтены данные только до момента запуска.' : ' Today: only through launch time; the day is incomplete.';
    }
  };
  input.addEventListener('input', update);
  if (startInput) startInput.addEventListener('input', update);
  update();
});
