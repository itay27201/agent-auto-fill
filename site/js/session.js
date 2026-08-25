import { loadConfig, isConfigured, promptForConfig } from "./config.js";
import { createApi } from "./api.js";
import { createWsClient } from "./ws-client.js";
import { state, setSession, notify } from "./state.js";
import { initViewer } from "./viewer.js";
import { initFieldsPanel } from "./fields-panel.js";
import { initChat, wsHandlers } from "./chat.js";
import { initRender } from "./render.js";

const POLL_START_MS = 1500;
const POLL_MAX_MS = 5000;

const sid = new URLSearchParams(window.location.search).get("sid");
const titleEl = document.getElementById("doc-title");
const progressEl = document.getElementById("progress");
const appEl = document.getElementById("app");
const errorEl = document.getElementById("session-error");

if (!sid) {
  showFatal("No session id in the URL. Start from the upload page.");
} else {
  boot();
}

async function boot() {
  let cfg = await loadConfig();
  if (!isConfigured(cfg)) {
    cfg = await promptForConfig(document.body);
  }
  const api = createApi(cfg.apiUrl);

  try {
    const session = await pollUntilReady(api, sid);
    if (session.status === "failed") {
      showFatal(`Processing failed: ${session.error || "unknown error"}`);
      return;
    }
    startApp(api, cfg, session);
  } catch (err) {
    showFatal(err.message || "Could not load this session.");
  }
}

async function pollUntilReady(api, sessionId) {
  let delay = POLL_START_MS;
  for (;;) {
    const session = await api.getSession(sessionId);
    titleEl.textContent = session.filename || "Document";
    progressEl.textContent = describeProgress(session);
    if (session.status === "ready" || session.status === "failed") return session;
    await sleep(delay);
    delay = Math.min(delay * 1.3, POLL_MAX_MS);
  }
}

function describeProgress(session) {
  if (session.status === "awaiting_upload") return "Waiting for upload...";
  if (session.status === "processing") return `Processing (${session.progress || "working"})...`;
  return session.status;
}

function startApp(api, cfg, session) {
  setSession(session);
  appEl.classList.remove("hidden");
  progressEl.textContent = `${session.field_count ?? state.fields.length} fields`;

  initViewer(document.getElementById("viewer-pane"));
  initFieldsPanel(document.getElementById("fields-panel"), api, onNeedsRefetch);
  initRender(document.getElementById("render-panel"), api);

  const ws = createWsClient(cfg.wsUrl, wsHandlers());
  initChat(
    {
      log: document.getElementById("chat-log"),
      scopeChip: document.getElementById("chat-scope"),
      input: document.getElementById("chat-input"),
      sendBtn: document.getElementById("chat-send"),
    },
    ws
  );

  notify();

  let refetching = false;
  async function onNeedsRefetch(reason) {
    if (refetching) return;
    refetching = true;
    try {
      const fresh = await api.getSession(sid);
      setSession(fresh);
      if (reason) flashProgress(reason);
    } finally {
      refetching = false;
    }
  }
}

function flashProgress(message, holdMs = 4000) {
  const prev = progressEl.textContent;
  progressEl.textContent = message;
  setTimeout(() => {
    if (progressEl.textContent === message) progressEl.textContent = prev;
  }, holdMs);
}

function showFatal(message) {
  errorEl.textContent = message;
  errorEl.classList.remove("hidden");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
