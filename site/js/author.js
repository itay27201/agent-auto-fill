// Drives catalog.html: upload a blank form, name it, then work with the
// authoring agent to write its guide.
//
// This talks to the *other* agent. The `author` action routes to
// author_chat.py, which has tools for writing guide sections and none for
// touching form values — the filling agent on session.html is the reverse.
//
// The transcript is held here rather than server-side. Defining a document is
// one sitting; the durable artifact is guide.md, which the agent flushes to S3
// after every write, so a closed tab loses the conversation and none of the work.

import { waitForConfig, isConfigured, showUnavailableNotice } from "./config.js";
import { createApi, uploadToS3 } from "./api.js";
import { createWsClient } from "./ws-client.js";
import { initUpload } from "./upload.js";
import { setMarkdown, escapeHtml } from "./md.js";

const POLL_START_MS = 1500;
const POLL_MAX_MS = 5000;

const el = (id) => document.getElementById(id);
const steps = { form: el("step-form"), meta: el("step-meta"), guide: el("step-guide") };

const cfg = await waitForConfig();
const ready = isConfigured(cfg);
if (!ready) {
  // Nothing on this page works without the backend URLs, so stop here rather
  // than wiring up handlers that would fail on first use.
  showUnavailableNotice(document.querySelector(".author-shell"));
  document.getElementById("step-form").classList.add("hidden");
}

const api = createApi(cfg.apiUrl);

let sessionId = null;   // the ingest session the blank form went through
let entry = null;       // the catalog entry, once created
let markdown = "";      // the guide as text
let history = [];       // [{role, text}] — replayed to the agent each turn
let ws = null;
let streamingBubble = null;
let editing = false;

// --------------------------------------------------------------- step 1

const uploader = ready
  ? initUpload(
      { dropZone: el("drop-zone"), fileInput: el("file-input"), statusLine: el("status-line") },
      cfg,
      { onSession: onFormUploaded }
    )
  : { setStatus: () => {} };

async function onFormUploaded(sid, file) {
  sessionId = sid;
  uploader.setStatus("Reading the form — this happens once, for everyone.");
  try {
    const session = await pollUntilReady(sid);
    if (session.status === "failed") {
      uploader.setStatus(`Could not read that document: ${session.error || "unknown error"}`, true);
      return;
    }
    uploader.setStatus(`Read ${session.field_count} fields.`);
    // Seed the name from the filename — it is usually close, and editing a
    // wrong guess is less work than typing from nothing.
    el("meta-name").value = (file?.name || "").replace(/\.(pdf|docx)$/i, "").replace(/[_-]+/g, " ");
    show("meta");
  } catch (err) {
    uploader.setStatus(err.message || "Could not read that document.", true);
  }
}

async function pollUntilReady(sid) {
  let delay = POLL_START_MS;
  for (;;) {
    const session = await api.getSession(sid);
    if (session.status === "ready" || session.status === "failed") return session;
    uploader.setStatus(`Reading the form (${session.progress || "working"})...`);
    await sleep(delay);
    delay = Math.min(delay * 1.3, POLL_MAX_MS);
  }
}

// --------------------------------------------------------------- step 2

el("create-entry").addEventListener("click", async () => {
  const name = el("meta-name").value.trim();
  const status = el("meta-status");
  if (!name) {
    status.textContent = "Give the form a name — it is what people pick from the list.";
    status.classList.add("error");
    return;
  }
  status.classList.remove("error");
  status.textContent = "Creating the entry...";
  try {
    const res = await api.createCatalogEntry(sessionId, {
      name,
      agency: el("meta-agency").value.trim(),
      description: el("meta-description").value.trim(),
      language: el("meta-language").value.trim(),
    });
    entry = res.entry;
    status.textContent = "";
    startGuideStep();
  } catch (err) {
    status.textContent = err.message || "Could not create the entry.";
    status.classList.add("error");
  }
});

// --------------------------------------------------------------- step 3

function startGuideStep() {
  show("guide");
  markdown = entry.guide_markdown || "";
  renderGuide();
  renderSources([]);
  connectAgent();
  appendSystem(
    `Ready. This assistant writes the guide for "${entry.name}" — it cannot fill anyone's form, ` +
    `and it will not write anything it cannot trace to a document you upload or to something you tell it.`
  );
}

function connectAgent() {
  ws = createWsClient(cfg.wsUrl, {
    onOpen: () => {},
    onClose: () => appendSystem("Disconnected — reconnecting..."),
    onSendFailed: (msg) => appendSystem(msg),
    turn_start: () => {
      streamingBubble = null;
      setBusy(true);
    },
    text: (msg) => appendAssistantDelta(msg.delta || ""),
    tool_start: (msg) => appendTool(describeTool(msg.name)),
    guide_updated: (msg) => {
      markdown = msg.markdown || markdown;
      renderGuide();
    },
    turn_end: (msg) => {
      streamingBubble = null;
      setBusy(false);
      if (msg.markdown) markdown = msg.markdown;
      renderGuide(msg);
    },
    warning: (msg) => appendSystem(msg.message, "warn"),
    error: (msg) => {
      appendSystem(msg.message || "Something went wrong.", "error");
      setBusy(false);
    },
  });
}

const TOOL_LABELS = {
  list_sources: "checking what you uploaded",
  read_source: "reading a reference document",
  get_field_list: "looking at the form's fields",
  read_guide: "re-reading the guide",
  write_section: "writing a section",
  write_field_note: "writing a note on a field",
};
const describeTool = (name) => TOOL_LABELS[name] || `using ${name}...`;

el("chat-send").addEventListener("click", sendMessage);
el("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

function sendMessage() {
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text || !ws) return;
  appendMessage("user", text);
  const ok = ws.sendRaw({
    action: "author",
    catalog_id: entry.catalog_id,
    message: text,
    history,
  });
  if (ok) {
    history.push({ role: "user", text });
    input.value = "";
  }
}

function setBusy(busy) {
  el("chat-send").disabled = busy;
}

// ------------------------------------------------------------- sources

el("add-source").addEventListener("click", () => el("source-input").click());
el("source-input").addEventListener("change", async () => {
  const file = el("source-input").files?.[0];
  if (!file) return;
  const status = el("source-status");
  status.classList.remove("error");
  status.textContent = `Uploading ${file.name}...`;
  try {
    const contentType = file.type || "application/octet-stream";
    const { upload_url } = await api.catalogSourceUrl(entry.catalog_id, file.name, contentType);
    await uploadToS3(upload_url, file, contentType);
    status.textContent = "";
    const fresh = await api.getCatalogEntry(entry.catalog_id);
    renderSources(fresh.sources || []);
    appendSystem(`Added ${file.name}. Ask the assistant to read it.`);
  } catch (err) {
    status.textContent = err.message || "Upload failed.";
    status.classList.add("error");
  }
  el("source-input").value = "";
});

function renderSources(sources) {
  const list = el("source-list");
  list.innerHTML = "";
  if (!sources.length) {
    list.innerHTML = '<span class="hint-inline">Nothing uploaded yet.</span>';
    return;
  }
  for (const s of sources) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = s.source_id;
    list.appendChild(chip);
  }
}

// --------------------------------------------------------------- guide

el("toggle-edit").addEventListener("click", () => {
  editing = !editing;
  el("guide-render").classList.toggle("hidden", editing);
  el("guide-source").classList.toggle("hidden", !editing);
  el("save-source").classList.toggle("hidden", !editing);
  el("toggle-edit").textContent = editing ? "Done editing" : "Edit as text";
  if (editing) el("guide-source").value = markdown;
});

el("save-source").addEventListener("click", async () => {
  const status = el("publish-status");
  status.classList.remove("error");
  status.textContent = "Saving...";
  try {
    const res = await api.updateCatalogEntry(entry.catalog_id, {
      guide_markdown: el("guide-source").value,
    });
    entry = res.entry;
    markdown = el("guide-source").value;
    renderGuide();
    status.textContent = "Saved.";
  } catch (err) {
    status.textContent = err.message || "Could not save.";
    status.classList.add("error");
  }
});

function renderGuide(summary) {
  const target = el("guide-render");
  if (!markdown.trim()) {
    target.innerHTML = '<p class="hint-inline">Nothing written yet. Tell the assistant what this form is, and upload whatever the agency publishes alongside it.</p>';
  } else {
    // Strip the frontmatter block: it is bookkeeping, not something to read.
    setMarkdown(target, markdown.replace(/^---\n[\s\S]*?\n---\n/, ""));
  }
  if (summary) {
    const done = (summary.field_count || 0) === 0 ? 0 : summary.field_notes || 0;
    el("guide-progress").textContent =
      `${done} of ${summary.field_count} fields noted` +
      (summary.empty_sections?.length ? ` · ${summary.empty_sections.length} sections empty` : "");
  }
}

el("publish").addEventListener("click", async () => {
  const status = el("publish-status");
  status.classList.remove("error");
  status.textContent = "Publishing...";
  try {
    await api.updateCatalogEntry(entry.catalog_id, { status: "published" });
    status.textContent = "Published. It is now in the list on the home page.";
    el("publish-note").textContent = "Anyone can now fill this form without uploading it.";
  } catch (err) {
    status.textContent = err.message || "Could not publish.";
    status.classList.add("error");
  }
});

// ------------------------------------------------------------ chat views

function show(step) {
  for (const [name, node] of Object.entries(steps)) node.classList.toggle("hidden", name !== step);
  el("step-label").textContent = `Step ${{ form: 1, meta: 2, guide: 3 }[step]} of 3`;
}

function appendMessage(role, text) {
  const node = document.createElement("div");
  node.className = `chat-msg ${role}`;
  node.setAttribute("dir", "auto");
  node.textContent = text;
  el("chat-log").appendChild(node);
  scrollChat();
  return node;
}

function appendAssistantDelta(delta) {
  if (!delta) return;
  if (!streamingBubble) {
    streamingBubble = appendMessage("assistant", "");
    history.push({ role: "assistant", text: "" });
  }
  streamingBubble.textContent += delta;
  history[history.length - 1].text = streamingBubble.textContent;
  scrollChat();
}

function appendTool(text) {
  const node = document.createElement("div");
  node.className = "chat-msg tool";
  node.textContent = text;
  el("chat-log").appendChild(node);
  scrollChat();
}

function appendSystem(text, kind = "info") {
  const node = document.createElement("div");
  node.className = kind === "error" ? "chat-msg system-error" : "chat-msg tool";
  node.innerHTML = escapeHtml(text);
  el("chat-log").appendChild(node);
  scrollChat();
}

function scrollChat() {
  const log = el("chat-log");
  log.scrollTop = log.scrollHeight;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
