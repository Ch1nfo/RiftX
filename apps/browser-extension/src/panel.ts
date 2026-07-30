import {
  harEntryToCapture,
  RiftXConnectorClient,
  shouldCapture,
  type HttpCapture,
} from "./connector.js";

const captures = new Map<string, HttpCapture>();
const selected = new Set<string>();
let currentRunId = "";
let streamAbort: AbortController | null = null;

const apiUrl = element<HTMLInputElement>("api-url");
const runSelect = element<HTMLSelectElement>("run-select");
const objective = element<HTMLInputElement>("objective");
const engagement = element<HTMLInputElement>("engagement");
const captureList = element<HTMLDivElement>("captures");
const status = element<HTMLPreElement>("status");

apiUrl.value = localStorage.getItem("riftx-api-url") || "http://127.0.0.1:8787";
apiUrl.addEventListener("change", () => localStorage.setItem("riftx-api-url", apiUrl.value));

element<HTMLButtonElement>("refresh-runs").addEventListener("click", refreshRuns);
element<HTMLButtonElement>("send-selected").addEventListener("click", sendSelected);
element<HTMLButtonElement>("cancel-run").addEventListener("click", cancelRun);
element<HTMLButtonElement>("open-webui").addEventListener("click", openWebUI);

chrome.devtools.network.onRequestFinished.addListener((entry) => {
  if (!shouldCapture(entry)) return;
  void harEntryToCapture(entry).then((capture) => {
    captures.set(capture.capture_id, capture);
    selected.add(capture.capture_id);
    renderCaptures();
  });
});

async function refreshRuns(): Promise<void> {
  try {
    const runs = await client().listRuns();
    runSelect.innerHTML = '<option value="">Create a new Run</option>';
    for (const run of runs) {
      const option = document.createElement("option");
      option.value = run.id;
      option.textContent = `${run.id.slice(0, 8)} — ${run.objective.description}`;
      runSelect.append(option);
    }
    log(`Loaded ${runs.length} Run(s).`);
  } catch (error) {
    logError(error);
  }
}

async function sendSelected(): Promise<void> {
  const items = [...selected].map((id) => captures.get(id)).filter(Boolean) as HttpCapture[];
  if (!items.length) return log("Select at least one XHR/Fetch request.");
  try {
    let targetRunId = runSelect.value;
    for (const capture of items) {
      const receipt = await client().submit(
        capture,
        targetRunId
          ? { runId: targetRunId }
          : {
              newRun: {
                objective: objective.value || "Analyze captured browser request",
                engagementName: engagement.value || "Browser connector capture",
              },
            },
      );
      currentRunId = receipt.submission.run_id;
      targetRunId = currentRunId;
      selected.delete(capture.capture_id);
      log(
        `Imported ${capture.method} ${capture.url}\n` +
          `Request Artifact: ${receipt.submission.request_artifact_id}`,
      );
    }
    renderCaptures();
    await refreshRuns();
    startStream(currentRunId);
  } catch (error) {
    logError(error);
  }
}

function startStream(runId: string): void {
  streamAbort?.abort();
  streamAbort = new AbortController();
  void client()
    .streamEvents(
      runId,
      (event) => log(`[${event.id}] ${event.type}\n${JSON.stringify(event.data, null, 2)}`),
      streamAbort.signal,
    )
    .catch((error) => {
      if (!streamAbort?.signal.aborted) logError(error);
    });
}

async function cancelRun(): Promise<void> {
  const runId = runSelect.value || currentRunId;
  if (!runId) return log("Choose a Run first.");
  try {
    await client().cancel(runId);
    log(`Cancel requested for ${runId}.`);
  } catch (error) {
    logError(error);
  }
}

async function openWebUI(): Promise<void> {
  const runId = runSelect.value || currentRunId;
  if (!runId) return log("Choose a Run first.");
  try {
    await chrome.tabs.create({ url: await client().webuiUrl(runId) });
  } catch (error) {
    logError(error);
  }
}

function renderCaptures(): void {
  captureList.replaceChildren();
  for (const capture of [...captures.values()].reverse()) {
    const label = document.createElement("label");
    label.className = "capture";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(capture.capture_id);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? selected.add(capture.capture_id) : selected.delete(capture.capture_id);
    });
    const text = document.createElement("span");
    text.textContent = `${capture.method} ${capture.response_status} ${capture.url}`;
    label.append(checkbox, text);
    captureList.append(label);
  }
}

function client(): RiftXConnectorClient {
  return new RiftXConnectorClient(apiUrl.value);
}

function log(message: string): void {
  status.textContent = `${new Date().toLocaleTimeString()} ${message}\n\n${status.textContent}`;
}

function logError(error: unknown): void {
  log(error instanceof Error ? error.message : String(error));
}

function element<T extends HTMLElement>(id: string): T {
  const value = document.getElementById(id);
  if (!value) throw new Error(`Missing #${id}`);
  return value as T;
}

void refreshRuns();
