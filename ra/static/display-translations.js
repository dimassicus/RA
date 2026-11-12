// Refresh pending display copies without interrupting edits to the current page.
(() => {
  const notice = document.querySelector('[data-translation-keys]');
  const keys = JSON.parse(notice.dataset.translationKeys);
  const fields = [...document.querySelectorAll('form input, form select, form textarea')];
  const value = field => ['checkbox', 'radio'].includes(field.type) ? field.checked : field.value;
  const initial = fields.map(value);
  let offset = 0;
  async function update() {
    try {
      if (document.hidden || fields.some((field, index) => value(field) !== initial[index])) return;
      const data = new URLSearchParams({csrf_token: document.querySelector('[name=csrf_token]').value});
      for (const key of keys.slice(offset, offset + 128)) data.append('key', key);
      offset = offset + 128 < keys.length ? offset + 128 : 0;
      const response = await fetch(notice.dataset.url, {method: 'POST', body: data});
      if (response.ok && (await response.json()).ready &&
          fields.every((field, index) => value(field) === initial[index])) window.location.reload();
    } catch {
      // Keep the current page and retry after a transient connection failure.
    } finally {
      setTimeout(update, 10000);
    }
  }
  setTimeout(update, 10000);
})();
