const form = document.getElementById("run-form");
const promptEl = document.getElementById("prompt");
const crewEl = document.getElementById("crew");
const modelEl = document.getElementById("model");
const submitBtn = document.getElementById("submit");
const resultSection = document.getElementById("result-section");
const resultEl = document.getElementById("result");
const errorSection = document.getElementById("error-section");
const errorEl = document.getElementById("error");

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

function showError(message) {
  errorSection.hidden = false;
  errorEl.textContent = message;
  resultSection.hidden = true;
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
  submitBtn.disabled = true;
  submitBtn.textContent = "Running...";

  try {
    const response = await fetch("/api/crew/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: promptEl.value,
        crew: crewEl.value,
        model: modelEl.value || undefined,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
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
