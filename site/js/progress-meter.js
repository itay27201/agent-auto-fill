// The topbar progress meter.
//
// The slot used to hold one string, written once at startup ("97 fields") and
// never updated — so the only number the page ever showed about your progress
// was the size of the job, not how much of it was done. It also doubled as a
// scratchpad for transient messages, and that is the constraint this module is
// built around: flashProgress() sets textContent, so the text has to be a leaf
// element of its own. Writing textContent on the wrapper would delete the bar.

import { state, onChange, draftFields } from "./state.js";

let meterEl = null;
let doneEl = null;
let draftEl = null;
let textEl = null;

// What the slot says when nothing transient is being flashed over it. Held
// separately because two overlapping flashes used to have the second capture the
// first's message as the thing to restore — so the transient text became
// permanent and the real progress never came back.
let baseline = "";
let flashTimer = null;

export function initProgressMeter(root) {
  if (!root) return;
  meterEl = root.querySelector("[data-role=meter]");
  doneEl = root.querySelector("[data-role=meter-done]");
  draftEl = root.querySelector("[data-role=meter-draft]");
  textEl = root.querySelector("[data-role=progress-text]");
  onChange(render);
  render();
}

/** Set the baseline text directly. Used while ingest is still running, when
 * there are no fields to count and the only honest thing to say is what the
 * pipeline is doing. */
export function setProgressText(text) {
  baseline = text;
  if (!flashTimer && textEl) textEl.textContent = text;
}

/** A transient message over the text, and only over the text — the two bar
 * segments keep tracking the real numbers underneath, which is why "Placed
 * {label}" can flash at the same moment the meter moves. */
export function flashProgress(message, holdMs = 4000) {
  if (!textEl) return;
  textEl.textContent = message;
  textEl.title = message;          // the slot ellipsises past ~34ch
  textEl.classList.add("flashing");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    flashTimer = null;
    textEl.classList.remove("flashing");
    textEl.title = "";
    textEl.textContent = baseline;
  }, holdMs);
}

function render() {
  if (!meterEl) return;

  const total = state.fields.length;
  // Nothing to measure yet — ingest is still running and the text slot is
  // carrying the pipeline status instead.
  meterEl.classList.toggle("hidden", !total);
  if (!total) return;

  const drafts = draftFields().length;

  // Three disjoint buckets, so the two segments can never sum past 100%:
  //   done   — has a value that is yours (typed, or an agent draft you confirmed)
  //   draft  — the agent wrote it and nobody has signed off; strict rendering
  //            refuses to export while any of these remain, so they get their
  //            own colour rather than being counted as progress
  //   rest   — empty
  let done = 0;
  for (const f of state.fields) {
    const v = state.values[f.field_id] || {};
    if (v.source === "agent" && !v.confirmed) continue;
    if (hasValue(v.value)) done += 1;
  }

  const donePct = (done / total) * 100;
  doneEl.style.width = `${donePct}%`;
  draftEl.style.width = `${(drafts / total) * 100}%`;

  const text = drafts
    ? `${done} of ${total} filled · ${drafts} to confirm`
    : `${done} of ${total} filled`;

  // role="progressbar" carries the number; deliberately no aria-live on the
  // text. A forty-field turn changes this count forty times, and the write is
  // already announced once, in a sentence, by viewer.js's .agent-activity
  // (role=status, aria-live=polite) — a second live region would read the same
  // event twice and interrupt the first.
  meterEl.setAttribute("aria-valuenow", String(Math.round(donePct)));
  meterEl.setAttribute("aria-valuetext", text);

  setProgressText(text);
}

/** Mirrors the test in viewer.js, so the meter and the boxes agree about what
 * counts as answered — including `false`, which only ever reaches the store
 * because somebody explicitly unticked a checkbox. */
function hasValue(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}
