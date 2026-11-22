const form = document.getElementById("run-form");
const promptEl = document.getElementById("prompt");
const crewEl = document.getElementById("crew");
const modelEl = document.getElementById("model");
const submitBtn = document.getElementById("submit");
const resultSection = document.getElementById("result-section");
const resultEl = document.getElementById("result");
const errorSection = document.getElementById("error-section");
const errorEl = document.getElementById("error");

function formatError(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") return item.msg || JSON.stringify(item);
        return String(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return "Unknown error";
}

function showError(message) {
  errorSection.hidden = false;
  errorEl.textContent = message;
  resultSection.hidden = true;
}

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body ? (body.detail ?? body) : res.statusText;
    throw new Error(`${path}: ${res.status} ${formatError(detail)}`);
  }
  return res.json();
}

async function loadOptions() {
  try {
    const [crews, models] = await Promise.all([
      getJSON("/api/crews"),
      getJSON("/api/models"),
    ]);

    crewEl.replaceChildren();
    (crews.crews || []).forEach((name) => crewEl.add(new Option(name, name)));
    if (!crewEl.options.length) crewEl.add(new Option("research_write", "research_write"));

    modelEl.replaceChildren();
    (models.data || []).forEach((m) => modelEl.add(new Option(m.name || m.id, m.id)));
  } catch (err) {
    showError(`Could not load options: ${err.message}`);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorSection.hidden = true;
  resultSection.hidden = true;

  const prompt = promptEl.value;

  submitBtn.disabled = true;
  submitBtn.textContent = "Running...";

  try {
    const response = await fetch("/api/crew/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: prompt,
        crew: crewEl.value,
        model: modelEl.value || undefined,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(formatError(data.detail ?? data ?? response.statusText));
    }
    resultSection.hidden = false;
    resultEl.textContent = data.result;
  } catch (err) {
    showError(err.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Run crew";
  }
});

loadOptions();
