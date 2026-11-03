// Intake desk: upload documents and follow their extraction live over Server-Sent Events.
//
// The browser's EventSource cannot send the X-API-Key header, so the stream is read with
// fetch() + ReadableStream and parsed here. The server always starts a stream with the job's
// current state, which makes reconnecting after a dropped connection safe.

const KEY_STORAGE = "dex-api-key";
const TERMINAL = new Set(["completed", "failed"]);
const STAGE_TEXT = {
  queued: "Waiting in the queue",
  processing: "Picked up by a worker",
  reading: "Reading the document",
  transcribing: "Transcribing with the vision model",
  extracting: "Extracting fields with the LLM",
  retrying: "Temporary failure — will retry",
  completed: "Fields extracted and validated",
  failed: "Processing failed",
};
const TYPE_TEXT = { invoice: "Invoice", contract: "Contract", resume: "Resume" };

const $ = (id) => document.getElementById(id);
const els = {
  keyForm: $("key-form"),
  keyInput: $("api-key"),
  keyStatus: $("key-status"),
  uploadForm: $("upload-form"),
  dropzone: $("dropzone"),
  fileInput: $("file-input"),
  fileName: $("file-name"),
  uploadButton: $("upload-button"),
  uploadError: $("upload-error"),
  jobList: $("job-list"),
  refreshJobs: $("refresh-jobs"),
  slipEmpty: $("slip-empty"),
  slip: $("slip"),
  slipType: $("slip-type"),
  slipFilename: $("slip-filename"),
  slipId: $("slip-id"),
  slipStatus: $("slip-status"),
  timeline: $("timeline"),
  connection: $("connection"),
  outcome: $("outcome"),
  outcomeTitle: $("outcome-title"),
  outcomeError: $("outcome-error"),
  result: $("result"),
  copyResult: $("copy-result"),
  downloadFile: $("download-file"),
};

const state = {
  apiKey: readStoredKey(),
  job: null, // the JobRead currently shown on the slip
  stream: null, // AbortController of the active event stream
  lastStage: null,
};

// --- API -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-API-Key": state.apiKey, ...options.headers },
  });
  if (!response.ok) {
    let code = "http_error";
    let message = `Request failed (${response.status}).`;
    try {
      ({ code, message } = (await response.json()).error);
    } catch {
      // not our error envelope; keep the generic message
    }
    throw new ApiError(response.status, code, message);
  }
  return response;
}

// --- Server-Sent Events over fetch -----------------------------------------------------

async function* readEvents(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value.replace(/\r\n?/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const event = parseEventBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) yield event;
    }
  }
}

function parseEventBlock(block) {
  const fields = {};
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // blank or keep-alive comment
    const colon = line.indexOf(":");
    const name = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (name === "data") data.push(value);
    else fields[name] = value;
  }
  if (data.length === 0) return null;
  return { type: fields.event ?? "message", id: fields.id, data: JSON.parse(data.join("\n")) };
}

async function followJob(jobId) {
  state.stream?.abort();
  const controller = new AbortController();
  state.stream = controller;
  let delay = 1000;

  while (!controller.signal.aborted) {
    try {
      const response = await api(`/jobs/${jobId}/events`, {
        headers: { Accept: "text/event-stream" },
        signal: controller.signal,
      });
      showConnection(null);
      for await (const { data } of readEvents(response)) {
        delay = 1000;
        applyEvent(data);
        if (TERMINAL.has(data.stage)) {
          refreshJobs();
          return;
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status < 500) {
        showConnection(error.message);
        return;
      }
    }
    // The stream ended or dropped before the final event: reconnect with backoff.
    showConnection(`Connection lost — reconnecting in ${delay / 1000}s…`);
    await sleep(delay, controller.signal);
    delay = Math.min(delay * 2, 15000);
  }
}

function sleep(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

// --- Routing slip ----------------------------------------------------------------------

async function openJob(jobId, knownJob = null) {
  state.stream?.abort();
  state.lastStage = null;
  els.timeline.replaceChildren();
  els.outcome.hidden = true;
  showConnection(null);
  els.slipEmpty.hidden = true;
  els.slip.hidden = false;
  highlightListItem(jobId);

  try {
    renderSlipHeader(knownJob ?? (await (await api(`/jobs/${jobId}`)).json()));
  } catch (error) {
    showConnection(error.message);
    return;
  }
  followJob(jobId);
}

function renderSlipHeader(job) {
  state.job = job;
  els.slipType.textContent = `${TYPE_TEXT[job.document_type] ?? job.document_type} · routing slip`;
  els.slipFilename.textContent = job.original_filename;
  els.slipId.textContent = job.id;
  setBadge(els.slipStatus, job.status);
}

function applyEvent(event) {
  setBadge(els.slipStatus, event.status);
  // A reconnect starts with a snapshot of the current state; skip it if already shown.
  if (event.stage === state.lastStage && event.stage !== "retrying") {
    if (event.job) showOutcome(event.job);
    return;
  }
  state.lastStage = event.stage;

  for (const item of els.timeline.querySelectorAll("li.current")) {
    item.classList.replace("current", "done");
  }
  const item = document.createElement("li");
  item.classList.add(event.stage, TERMINAL.has(event.stage) ? event.stage : "current");

  const stage = document.createElement("span");
  stage.className = "stage";
  stage.textContent = event.attempt > 1 ? `${event.stage} · attempt ${event.attempt}` : event.stage;

  const time = document.createElement("time");
  time.dateTime = event.at;
  time.textContent = new Date(event.at).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const description = document.createElement("span");
  description.className = "description";
  description.textContent = event.message ?? STAGE_TEXT[event.stage] ?? "";

  item.append(stage, time, description);
  els.timeline.append(item);

  if (event.job) showOutcome(event.job);
}

function showOutcome(job) {
  state.job = job;
  els.outcome.hidden = false;
  const failed = job.status === "failed";
  els.outcomeTitle.textContent = failed ? "No data extracted" : "Extracted fields";
  els.outcomeError.hidden = !failed;
  els.outcomeError.textContent = job.error_message ?? "";
  els.result.hidden = failed;
  els.copyResult.hidden = failed;
  if (!failed) els.result.innerHTML = highlightJson(job.result);
}

function showConnection(message) {
  els.connection.hidden = !message;
  els.connection.textContent = message ?? "";
}

function setBadge(element, status) {
  element.dataset.status = status;
  element.textContent = status;
}

function highlightJson(value) {
  const escaped = JSON.stringify(value, null, 2)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
  return escaped.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b|\b(true|false|null)\b/g,
    (match, string, colon, number, literal) => {
      if (string) return colon ? `<span class="k">${string}</span>${colon}` : `<span class="s">${string}</span>`;
      if (number) return `<span class="n">${number}</span>`;
      return `<span class="l">${literal}</span>`;
    },
  );
}

// --- Recent jobs -----------------------------------------------------------------------

async function refreshJobs() {
  if (!state.apiKey) return;
  try {
    const { items } = await (await api("/jobs?limit=10")).json();
    setKeyStatus("ok");
    renderJobList(items);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) setKeyStatus("invalid");
    renderJobList([], error.message);
  }
}

function renderJobList(jobs, emptyMessage = "No jobs yet — submit your first document.") {
  if (jobs.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = emptyMessage;
    els.jobList.replaceChildren(empty);
    return;
  }
  els.jobList.replaceChildren(
    ...jobs.map((job) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.jobId = job.id;

      const name = document.createElement("span");
      name.className = "job-name";
      name.textContent = job.original_filename;

      const meta = document.createElement("span");
      meta.className = "job-meta";
      meta.textContent = `${TYPE_TEXT[job.document_type] ?? job.document_type} · ${timeAgo(job.created_at)}`;

      const badge = document.createElement("span");
      badge.className = "status-badge";
      setBadge(badge, job.status);

      button.append(name, badge, meta);
      button.addEventListener("click", () => openJob(job.id));
      item.append(button);
      return item;
    }),
  );
  if (state.job) highlightListItem(state.job.id);
}

function highlightListItem(jobId) {
  for (const button of els.jobList.querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.jobId === jobId));
  }
}

function timeAgo(iso) {
  const seconds = Math.round((new Date(iso) - Date.now()) / 1000);
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return format.format(seconds, "second");
}

// --- API key ---------------------------------------------------------------------------

function readStoredKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

function setKeyStatus(stateName) {
  els.keyStatus.dataset.state = stateName;
  els.keyStatus.textContent = { missing: "not set", ok: "connected", invalid: "invalid key" }[stateName];
}

els.keyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.apiKey = els.keyInput.value.trim();
  try {
    sessionStorage.setItem(KEY_STORAGE, state.apiKey);
  } catch {
    // storage unavailable (e.g. private mode): the key lives in memory for this tab only
  }
  refreshJobs();
});

// --- Upload ----------------------------------------------------------------------------

function showSelectedFile() {
  const file = els.fileInput.files[0];
  els.fileName.hidden = !file;
  els.fileName.textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(0)} KB` : "";
}

els.fileInput.addEventListener("change", showSelectedFile);
for (const type of ["dragenter", "dragover"]) {
  els.dropzone.addEventListener(type, () => els.dropzone.classList.add("dragging"));
}
for (const type of ["dragleave", "drop"]) {
  els.dropzone.addEventListener(type, () => els.dropzone.classList.remove("dragging"));
}
els.dropzone.addEventListener("drop", () => setTimeout(showSelectedFile));

els.uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.uploadError.hidden = true;
  if (!state.apiKey) {
    els.uploadError.textContent = "Set your API key first.";
    els.uploadError.hidden = false;
    els.keyInput.focus();
    return;
  }
  els.uploadButton.disabled = true;
  els.uploadButton.textContent = "Uploading…";
  try {
    const response = await api("/jobs", { method: "POST", body: new FormData(els.uploadForm) });
    const job = await response.json();
    els.uploadForm.reset();
    showSelectedFile();
    await refreshJobs();
    openJob(job.id, job);
  } catch (error) {
    els.uploadError.textContent = error.message;
    els.uploadError.hidden = false;
  } finally {
    els.uploadButton.disabled = false;
    els.uploadButton.textContent = "Submit for extraction";
  }
});

// --- Outcome actions -------------------------------------------------------------------

els.copyResult.addEventListener("click", async () => {
  if (!state.job?.result) return;
  await navigator.clipboard.writeText(JSON.stringify(state.job.result, null, 2));
  els.copyResult.textContent = "Copied";
  setTimeout(() => (els.copyResult.textContent = "Copy JSON"), 1500);
});

els.downloadFile.addEventListener("click", async () => {
  if (!state.job) return;
  try {
    // A plain link cannot send the API key header, so fetch the file and save the blob.
    const blob = await (await api(`/jobs/${state.job.id}/file`)).blob();
    const url = URL.createObjectURL(blob);
    const link = Object.assign(document.createElement("a"), {
      href: url,
      download: state.job.original_filename,
    });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); // revoking at once can cancel the download
  } catch (error) {
    showConnection(error.message);
  }
});

els.refreshJobs.addEventListener("click", refreshJobs);

// --- Start -----------------------------------------------------------------------------

if (state.apiKey) {
  els.keyInput.value = state.apiKey;
  refreshJobs();
}
