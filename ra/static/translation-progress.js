(() => {
  const status = document.getElementById("translation-progress");
  if (!status) return;

  async function update() {
    try {
      const response = await fetch(status.dataset.url, {cache: "no-store"});
      if (!response.ok) throw new Error("Status unavailable");
      const data = await response.json();
      const state = data.running ? "Running" : data.missing ? "Finished with missing translations" : "Complete";
      const parts = [`${state}. This page: ${data.saved}/${data.total} translations saved; ${data.missing} missing.`];
      if (data.running) {
        parts.push(`${data.message} (${data.elapsed_seconds}s in this step).`);
        if (data.model_activity) parts.push(`Shared local model: ${data.model_activity}`);
        if (data.retry_requested) parts.push("Another pass is queued; no need to click Retry again.");
      } else if (data.missing) {
        parts.push("Remaining translations were not completed or approved. Retry or enter reviewed wording manually.");
      }
      if (data.error) parts.push(data.error);
      status.textContent = parts.join(" ");
    } catch {
      status.textContent = "Cannot read translation progress. Retrying shortly; this does not mean translation has stopped.";
    } finally {
      window.setTimeout(update, 5000);
    }
  }
  update();
})();
