// Chat panel: talks to agent_chat.py over the WebSocket the session page
// already opened. Renders streamed text, tool activity, and reacts to
// field_updated/highlight events pushed mid-turn.

import { state, onChange, applyFieldUpdates, clearSelection, draftFields } from "./state.js";
import { highlightField, markWritten, announceWrites } from "./viewer.js";
import { createActivityLog } from "./activity.js";
import { createVoiceInput } from "./voice.js";
import { createSpeaker } from "./speak.js";

// The box grows to fit a dictated paragraph, which arrives all at once rather
// than a line at a time the way typing does — see autosize(). The ceiling lives
// in `.chat-input textarea { max-height }` alone, because it has to shrink with
// the viewport: a cap tall enough for a desktop pane overflows a phone's.

let log, scopeChip, input, sendBtn, wsClient;
let streamingBubble = null;
let activity = null;
let speaker = null;

// Writes the agent has sent but that have not been applied yet — see flushWrites.
let pendingWrites = null;

export function initChat(els, ws, sttEndpoint) {
  ({ log, scopeChip, input, sendBtn } = els);
  wsClient = ws;
  activity = createActivityLog(log);

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("input", autosize);

  // Both take `activity.note` rather than building their own log: a second
  // createActivityLog over the same element keeps its own open row, and would
  // settle one it does not own — leaving the agent's tool row spinning forever.
  speaker = createSpeaker({ button: els.speakBtn, onNote: activity.note });
  createVoiceInput({
    button: els.micBtn,
    status: els.voiceStatus,
    sttUrl: sttEndpoint,
    onTranscript: insertTranscript,
    onNote: activity.note,
    // Anything still being read aloud would otherwise be recorded off the
    // speakers and sent to Gemini as if the person had said it.
    onRecordStart: () => speaker.cancel(),
  });

  onChange(renderScopeChip);
  renderScopeChip();
}

export function wsHandlers() {
  return {
    onOpen: () => activity.note("Connected."),
    onClose: () => activity.note("Disconnected — reconnecting..."),
    onSendFailed: (msg) => activity.note(msg),
    turn_start: () => {
      streamingBubble = null;
      speaker.cancel();
      setBusy(true);
    },
    text: (msg) => {
      appendAssistantDelta(msg.delta || "");
      speaker.feed(msg.delta || "");
    },
    tool_start: (msg) => activity.tool(msg.name),
    tool_end: (msg) => activity.toolDone(msg.name, msg.ok !== false),
    field_updated: (msg) => {
      queueWrite(msg.field_id, {
        value: msg.value,
        source: msg.source,
        confirmed: Boolean(msg.confirmed),
        // Sent by tools.py so a later confirm can carry `expected_version`.
        // Without it the conditional write silently stops being conditional.
        version: msg.version,
      });
    },
    // An explicit highlight is the agent pointing at something and asking you to
    // look, so it centres. A write scrolls as little as it can — see flushWrites.
    highlight: (msg) => highlightField(msg.field_id, { block: "center" }),
    turn_end: () => {
      streamingBubble = null;
      // Whatever is left in the buffer never got a sentence ending.
      speaker.flush();
      activity.settle();
      setBusy(false);
    },
    warning: (msg) => activity.note(msg.message, "warn"),
    error: (msg) => {
      activity.note(msg.message || "Something went wrong.", "error");
      setBusy(false);
    },
  };
}

// ------------------------------------------------------------- agent writes
// A turn that fills a section sends one field_updated per field, and every
// listener on the store rebuilds its whole DOM. Applied one at a time, a
// forty-field turn is eighty rebuilds, and each one replaces the element the
// previous write's animation was still playing on. Hold them for a frame
// instead: the writes that arrive together are shown together.

function queueWrite(fieldId, patch) {
  if (!fieldId) return;
  if (!pendingWrites) {
    pendingWrites = new Map();
    requestAnimationFrame(flushWrites);
  }
  pendingWrites.set(fieldId, { ...(pendingWrites.get(fieldId) || {}), ...patch });
}

function flushWrites() {
  const batch = pendingWrites;
  pendingWrites = null;
  if (!batch?.size) return;

  const ids = Array.from(batch.keys());
  // Marked before the store notifies, because the viewer reads the marks while
  // it rebuilds and only the boxes in this batch should animate.
  markWritten(ids);
  applyFieldUpdates(Object.fromEntries(batch));

  // Scroll to where the agent is working, but only as far as it takes: `nearest`
  // does nothing when the box is already on screen, which is what keeps a long
  // batch from dragging the page around under someone trying to read it.
  highlightField(ids[0], { block: "nearest" });
  announceWrites(ids.length, draftFields().length);
}

function send() {
  const text = input.value.trim();
  if (!text) return;
  // Asking something new means the previous answer is no longer what you want
  // read to you.
  speaker.cancel();
  appendMessage("user", text);
  const scope = Array.from(state.selectedFieldIds);
  const ok = wsClient.send(state.sid, text, scope);
  if (ok) {
    input.value = "";
    autosize();
  }
}

/** Appends rather than replaces — someone may have typed half a sentence and
 * dictated the rest, and losing what they typed would be worse than a clumsy
 * join. */
function insertTranscript(text) {
  const existing = input.value.trimEnd();
  input.value = existing ? `${existing} ${text}` : text;
  autosize();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
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
  if (!streamingBubble) {
    // Tools are done for now — the agent is talking.
    activity.settle();
    streamingBubble = appendMessage("assistant", "");
  }
  streamingBubble.textContent += delta;
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
