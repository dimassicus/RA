(() => {
  const status = document.getElementById("translation-progress");
  if (!status) return;
  const russian = document.documentElement?.lang === "ru";

  async function update() {
    try {
      const response = await fetch(status.dataset.url, {cache: "no-store"});
      if (!response.ok) throw new Error("Status unavailable");
      const data = await response.json();
      const states = russian
        ? ["Идёт перевод", "Часть переводов не готова", "Перевод завершён"]
        : ["Running", "Finished with missing translations", "Complete"];
      const state = states[data.running ? 0 : data.missing ? 1 : 2];
      const parts = [russian
        ? `${state}. Переводов в этом списке: ${data.total}. Сохранено: ${data.saved}. Не хватает: ${data.missing}.`
        : `${state}. This page: ${data.saved}/${data.total} translations saved; ${data.missing} missing.`];
      if (data.running) {
        parts.push(russian ? `${data.message} (на этом этапе: ${data.elapsed_seconds} с).` :
          `${data.message} (${data.elapsed_seconds}s in this step).`);
        if (data.model_activity) parts.push((russian ? "Локальная модель: " : "Shared local model: ") + data.model_activity);
        if (data.retry_requested) parts.push(russian ? "Повторный перевод уже в очереди." :
          "Another pass is queued; no need to click Retry again.");
      } else if (data.missing) {
        parts.push(russian ? "Оставшиеся переводы ещё не готовы или не прошли проверку. Повторите перевод или введите проверенный текст вручную." :
          "Remaining translations were not completed or approved. Retry or enter reviewed wording manually.");
      }
      if (data.error) parts.push(data.error);
      status.textContent = parts.join(" ");
    } catch {
      status.textContent = russian ? "Не удалось получить статус перевода. Страница скоро повторит запрос. Сам перевод может продолжаться." :
        "Cannot read translation progress. Retrying shortly; this does not mean translation has stopped.";
    } finally {
      window.setTimeout(update, 5000);
    }
  }
  update();
})();
