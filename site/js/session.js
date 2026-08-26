import { waitForConfig, isConfigured, showUnavailableNotice } from "./config.js";
import { createApi } from "./api.js";
import { createWsClient } from "./ws-client.js";
import { state, setSession, notify } from "./state.js";
import { initViewer } from "./viewer.js";
import { initFieldsPanel } from "./fields-panel.js";
import { initChat, wsHandlers } from "./chat.js";
import { initRender } from "./render.js";
import { initGuidePanel } from "./guide-panel.js";

const POLL_START_MS = 1500;
const POLL_MAX_MS = 5000;
// Ingest is slow but not unbounded. Past this we stop polling and say so,
// rather than leaving someone watching a spinner that will never resolve.
const POLL_TIMEOUT_MS = 5 * 60 * 1000;
// A single failed poll is usually the session row not being readable yet, or
// a blip. Only give up once they stack up.
const MAX_CONSECUTIVE_ERRORS = 4;

const sid = new URLSearchParams(window.location.search).get("sid");
const titleEl = document.getElementById("doc-title");
const progressEl = document.getElementById("progress");
const appEl = document.getElementById("app");
const errorEl = document.getElementById("session-error");
const loadingEl = document.getElementById("session-loading");
const loadingDecorEl = document.getElementById("loading-decor");
const loadingTitleEl = document.getElementById("loading-title");
const loadingNoteEl = document.getElementById("loading-note");
const loadingBackEl = document.getElementById("loading-back");

if (!sid) {
  showFatal("No session id in the URL. Start from the upload page.");
} else {
  boot();
}

async function boot() {
  const cfg = await waitForConfig();
  if (!isConfigured(cfg)) {
    hideLoading();
    showUnavailableNotice(document.body);
    return;
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
  let errors = 0;
  const deadline = Date.now() + POLL_TIMEOUT_MS;

  setLoadingStatus("Getting your document ready...");
  for (;;) {
    let session;
    try {
      session = await api.getSession(sessionId);
      errors = 0;
    } catch (err) {
      if (++errors >= MAX_CONSECUTIVE_ERRORS) throw err;
      await sleep(delay);
      continue;
    }

    titleEl.textContent = session.filename || "Document";
    if (session.filename) loadingTitleEl.textContent = `Reading ${session.filename}`;
    const progress = describeProgress(session);
    progressEl.textContent = progress;
    setLoadingStatus(progress);

    if (session.status === "ready" || session.status === "failed") return session;
    if (Date.now() > deadline) {
      throw new Error("This document is taking longer than expected. Try uploading it again.");
    }
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
  hideLoading();
  appEl.classList.remove("hidden");
  progressEl.textContent = `${session.field_count ?? state.fields.length} fields`;

  initViewer(document.getElementById("viewer-pane"), api, onBoxPlaced);
  initFieldsPanel(document.getElementById("fields-panel"), api, onNeedsRefetch);
  initRender(document.getElementById("render-panel"), api);
  // Only appears when this form has a guide — i.e. it came from the catalog,
  // or the upload hash-matched a form somebody already documented.
  initGuidePanel(
    document.getElementById("guide-panel"),
    document.querySelector('[data-side-tab="guide"]')
  );
  initSideTabs();

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

  /** A box was dragged. The move is already applied locally and saved; this
   * only reports whether the save actually landed, because an optimistic
   * update that silently failed would leave the page and the document
   * disagreeing about where a value is going to be stamped. */
  function onBoxPlaced(fieldId, error) {
    const label = state.fieldsById.get(fieldId)?.label || fieldId;
    progressEl.textContent = error
      ? `Could not save the box for ${label}: ${error.message}`
      : `Placed ${label}`;
  }

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

function initSideTabs() {
  const tabs = Array.from(document.querySelectorAll("[data-side-tab]"));
  const panels = new Map(
    Array.from(document.querySelectorAll("[data-side-panel]")).map((el) => [el.dataset.sidePanel, el])
  );
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      for (const t of tabs) t.classList.toggle("active", t === tab);
      for (const [key, panel] of panels) panel.classList.toggle("hidden", key !== tab.dataset.sideTab);
    });
  }
}

function flashProgress(message, holdMs = 4000) {
  const prev = progressEl.textContent;
  progressEl.textContent = message;
  setTimeout(() => {
    if (progressEl.textContent === message) progressEl.textContent = prev;
  }, holdMs);
}

function setLoadingStatus(text) {
  if (loadingEl.classList.contains("hidden")) return;
  errorEl.textContent = text;
}

function hideLoading() {
  loadingEl.classList.add("hidden");
  loadingDecorEl.classList.add("hidden");
}

function showFatal(message) {
  appEl.classList.add("hidden");
  loadingEl.classList.remove("hidden");
  loadingDecorEl.classList.remove("hidden");
  loadingTitleEl.textContent = "Something went wrong";
  loadingNoteEl.classList.add("hidden");
  loadingBackEl.classList.remove("hidden");
  errorEl.textContent = message;
  errorEl.classList.add("error");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
