// Chat panel: talks to agent_chat.py over the WebSocket the session page
// already opened. Renders streamed text, tool activity, and reacts to
// field_updated/highlight events pushed mid-turn.

import { state, onChange, applyFieldUpdate, clearSelection } from "./state.js";
import { highlightField } from "./viewer.js";

let log, scopeChip, input, sendBtn, wsClient;
let streamingBubble = null;

export function initChat(els, ws) {
  ({ log, scopeChip, input, sendBtn } = els);
  wsClient = ws;

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  onChange(renderScopeChip);
  renderScopeChip();
}

export function wsHandlers() {
  return {
    onOpen: () => appendSystem("Connected."),
    onClose: () => appendSystem("Disconnected — reconnecting..."),
    onSendFailed: (msg) => appendSystem(msg),
    turn_start: () => {
      streamingBubble = null;
      setBusy(true);
    },
    text: (msg) => appendAssistantDelta(msg.delta || ""),
    tool_start: (msg) => appendTool(`using ${msg.name}...`),
    field_updated: (msg) => {
      applyFieldUpdate(msg.field_id, {
        value: msg.value,
        source: msg.source,
        confirmed: Boolean(msg.confirmed),
      });
    },
    highlight: (msg) => highlightField(msg.field_id),
    turn_end: () => {
      streamingBubble = null;
      setBusy(false);
    },
    warning: (msg) => appendSystem(msg.message, "warn"),
    error: (msg) => {
      appendSystem(msg.message || "Something went wrong.", "error");
      setBusy(false);
    },
  };
}

function send() {
  const text = input.value.trim();
  if (!text) return;
  appendMessage("user", text);
  const scope = Array.from(state.selectedFieldIds);
  const ok = wsClient.send(state.sid, text, scope);
  if (ok) input.value = "";
}

function setBusy(busy) {
  sendBtn.disabled = busy;
}

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `chat-msg ${role}`;
  el.setAttribute("dir", "auto");
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function appendAssistantDelta(delta) {
  if (!delta) return;
  if (!streamingBubble) streamingBubble = appendMessage("assistant", "");
  streamingBubble.textContent += delta;
  log.scrollTop = log.scrollHeight;
}

function appendTool(text) {
  const el = document.createElement("div");
  el.className = "chat-msg tool";
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function appendSystem(text, kind = "info") {
  const el = document.createElement("div");
  el.className = kind === "error" ? "chat-msg system-error" : "chat-msg tool";
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function renderScopeChip() {
  const n = state.selectedFieldIds.size;
  if (!n) {
    scopeChip.classList.add("hidden");
    return;
  }
  scopeChip.classList.remove("hidden");
  scopeChip.innerHTML = "";
  const label = document.createElement("span");
  label.textContent = `Scoped to ${n} field${n === 1 ? "" : "s"}`;
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "small ghost";
  clear.textContent = "Clear";
  clear.addEventListener("click", clearSelection);
  scopeChip.append(label, clear);
}
