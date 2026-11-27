const form = document.getElementById("run-form");
const promptEl = document.getElementById("prompt");
const crewEl = document.getElementById("crew");
const modelEl = document.getElementById("model");
const submitBtn = document.getElementById("submit");
const resultSection = document.getElementById("result-section");
const resultEl = document.getElementById("result");
const errorSection = document.getElementById("error-section");
const errorEl = document.getElementById("error");

const explainForm = document.getElementById("explain-form");
const repoUrlEl = document.getElementById("repo-url");
const modeEl = document.getElementById("mode");
const granularityEl = document.getElementById("granularity");
const maxWorkersEl = document.getElementById("max-workers");
const maxFilesEl = document.getElementById("max-files");
const focusPathsEl = document.getElementById("focus-paths");
const refineEl = document.getElementById("refine");
const liveEl = document.getElementById("live");
const explainSubmitBtn = document.getElementById("explain-submit");
const explainCancelBtn = document.getElementById("explain-cancel");
const explainStatusSection = document.getElementById("explain-status-section");
const explainStatusEl = document.getElementById("explain-status");
const explainLiveSection = document.getElementById("explain-live-section");
const explainLiveTextEl = document.getElementById("explain-live-text");
const explainChangesSection = document.getElementById("explain-changes-section");
const explainChangesTextEl = document.getElementById("explain-changes-text");
const explainResultSection = document.getElementById("explain-result-section");
const explainTextEl = document.getElementById("explain-text");
const explainDownloadsEl = document.getElementById("explain-downloads");
const explainErrorSection = document.getElementById("explain-error-section");
const explainErrorEl = document.getElementById("explain-error");
let explainTimer = null;
let currentJobId = null;

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

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds % 60)}s`;
}

function showExplainError(message) {
  explainErrorSection.hidden = false;
  explainErrorEl.textContent = message;
  explainStatusSection.hidden = true;
  explainLiveSection.hidden = true;
  explainChangesSection.hidden = true;
  explainResultSection.hidden = true;
  currentJobId = null;
}

function setExplainBusy(busy) {
  explainSubmitBtn.disabled = busy;
  explainSubmitBtn.textContent = busy ? "Explaining..." : "Explain repo";
  explainCancelBtn.disabled = !busy;
}

function stopPolling() {
  if (explainTimer !== null) {
    clearTimeout(explainTimer);
    explainTimer = null;
  }
}

function fileHref(jobId, path) {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `/api/explain/${jobId}/files/${encoded}`;
}

async function fetchText(path) {
  try {
    const response = await fetch(`${path}?t=${Date.now()}`);
    if (!response.ok) return null;
    return await response.text();
  } catch (err) {
    return null;
  }
}

function renderDownloads(jobId, files) {
  explainDownloadsEl.replaceChildren();
  for (const path of files) {
    if (path === "job.json" || path.startsWith("repo/")) continue;
    const link = document.createElement("a");
    link.href = fileHref(jobId, path);
    link.textContent = path;
    link.setAttribute("download", path.split("/").pop());
    explainDownloadsEl.appendChild(link);
  }
}

async function pollExplain(jobId) {
  try {
    const data = await getJSON(`/api/explain/${jobId}`);
    const progress = data.progress || {};
    const counts = progress.total
      ? ` - ${progress.done}/${progress.total} chunks, ${progress.workers} workers`
      : "";
    const timing = data.elapsed_seconds
      ? ` - ${formatDuration(data.elapsed_seconds)} elapsed`
        + (data.eta_seconds ? `, ~${formatDuration(data.eta_seconds)} remaining` : "")
      : "";
    explainStatusEl.textContent = `${data.status}${counts}${timing}${data.partial ? " (partial)" : ""}`;

    if (liveEl.checked && data.live_available) {
      const live = await fetchText(fileHref(jobId, "live.md"));
      if (live !== null) {
        explainLiveTextEl.textContent = live;
        explainLiveSection.hidden = false;
      }
    }

    if (["completed", "failed", "partial", "cancelled"].includes(data.status)) {
      stopPolling();
      setExplainBusy(false);
      currentJobId = null;
      explainLiveSection.hidden = true;
      if (data.error) {
        showExplainError(data.error);
      } else {
        explainErrorSection.hidden = true;
        explainTextEl.textContent = data.text || "";
        renderDownloads(jobId, data.files || []);
        explainResultSection.hidden = false;
        if ((data.files || []).includes("changes.md")) {
          const changes = await fetchText(fileHref(jobId, "changes.md"));
          if (changes !== null) {
            explainChangesTextEl.textContent = changes;
            explainChangesSection.hidden = false;
          }
        }
      }
      return;
    }

    explainTimer = setTimeout(() => pollExplain(jobId), 2000);
  } catch (err) {
    stopPolling();
    setExplainBusy(false);
    showExplainError(err.message);
  }
}

explainForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  stopPolling();
  explainErrorSection.hidden = true;
  explainResultSection.hidden = true;
  explainLiveSection.hidden = true;
  explainChangesSection.hidden = true;
  explainStatusSection.hidden = false;
  explainStatusEl.textContent = "Starting...";
  setExplainBusy(true);

  const focusPaths = focusPathsEl.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const body = {
    repo_url: repoUrlEl.value.trim(),
    mode: modeEl.value,
    granularity: granularityEl.value,
    refine: refineEl.checked,
    live: liveEl.checked,
    max_workers: Number(maxWorkersEl.value || 4),
    max_files: Number(maxFilesEl.value || 150),
  };
  if (focusPaths.length) body.focus_paths = focusPaths;

  try {
    const response = await fetch("/api/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(formatError(data.detail ?? data ?? response.statusText));
    }
    currentJobId = data.job_id;
    explainStatusEl.textContent = `queued - job ${data.job_id}`;
    pollExplain(data.job_id);
  } catch (err) {
    setExplainBusy(false);
    showExplainError(err.message);
  }
});

explainCancelBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  explainCancelBtn.disabled = true;
  try {
    const response = await fetch(`/api/explain/${currentJobId}/cancel`, { method: "POST" });
    if (!response.ok) throw new Error(await response.text());
    explainStatusEl.textContent = "cancelling...";
  } catch (err) {
    setExplainBusy(false);
    showExplainError(err.message);
  }
});
