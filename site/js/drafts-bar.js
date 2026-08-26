// The rail between a filled form and a filed one.
//
// Strict rendering refuses to export while any agent draft is unconfirmed, so
// every draft is a blocker — but confirming them was one click per field, in two
// unconnected lists (the per-row banner in fields-panel.js and the check results
// in render.js). A forty-field turn was forty clicks to get back to where the
// export button would even enable.
//
// PATCH /sessions/{id}/fields already takes a list and answers per item, so
// confirming all of them is one request, not N.

import { state, onChange, applyFieldUpdates, draftFields } from "./state.js";
import { highlightField } from "./viewer.js";

const COPY = {
  none: "No drafts waiting.",
  waiting: (n) => `${n} value${n === 1 ? "" : "s"} waiting for you`,
  confirmAll: "Confirm all",
  confirming: "Confirming...",
  review: "Review one by one",
  reviewing: (i, n) => `Reviewing ${i} of ${n}`,
  next: "Confirm and next",
  stop: "Stop reviewing",
  overflow: (n) => `${n} value${n === 1 ? "" : "s"} too wide for the box — will be cut off in print`,
  someFailed: (n) => `${n} could not be confirmed — refreshing.`,
};

let root, api, onNeedsRefetch;
let busy = false;
let reviewIndex = null;   // null when not stepping through drafts

export function initDraftsBar(el, apiClient, refetchFn) {
  root = el;
  api = apiClient;
  onNeedsRefetch = refetchFn;
  onChange(render);
  render();
}

/** Overflowing fields are found by measuring laid-out boxes, which only the
 * viewer can do — so it reports them here rather than this module guessing. */
let overflowing = [];
export function setOverflowing(fieldIds) {
  const next = Array.from(fieldIds);
  const changed = next.length !== overflowing.length
    || next.some((id, i) => id !== overflowing[i]);
  overflowing = next;
  if (changed) render();
}

function render() {
  if (!root) return;
  const drafts = draftFields();

  // Someone confirmed the field being reviewed (here or in the panel), so the
  // list shrank under the cursor. Stepping past the end ends the review.
  if (reviewIndex !== null && reviewIndex >= drafts.length) reviewIndex = null;

  root.classList.toggle("hidden", !drafts.length && !overflowing.length);
  root.innerHTML = "";
  if (!drafts.length && !overflowing.length) return;

  if (drafts.length) root.appendChild(draftsRow(drafts));
  if (overflowing.length) root.appendChild(overflowRow());
}

function draftsRow(drafts) {
  const row = document.createElement("div");
  row.className = "drafts-row";

  const count = document.createElement("span");
  count.className = "drafts-count";
  count.textContent = reviewIndex === null
    ? COPY.waiting(drafts.length)
    : COPY.reviewing(reviewIndex + 1, drafts.length);
  row.appendChild(count);

  if (reviewIndex === null) {
    row.append(
      button(COPY.review, "small ghost", () => startReview(drafts)),
      button(busy ? COPY.confirming : COPY.confirmAll, "small primary",
             () => confirmAll(drafts), busy)
    );
  } else {
    const current = drafts[reviewIndex];
    const label = document.createElement("span");
    label.className = "drafts-current";
    label.setAttribute("dir", "auto");
    label.textContent = current.label || current.field_id;
    row.append(
      label,
      button(COPY.stop, "small ghost", stopReview),
      button(COPY.next, "small primary", () => confirmCurrent(drafts), busy)
    );
  }
  return row;
}

function overflowRow() {
  const row = document.createElement("div");
  row.className = "drafts-row overflow-row";
  const text = document.createElement("span");
  text.className = "drafts-count";
  text.textContent = COPY.overflow(overflowing.length);
  // Jumps to the first one rather than listing them: the fix is per-field and
  // starts with seeing which box is too small for what is in it.
  row.append(text, button("Show me", "small ghost",
                          () => highlightField(overflowing[0], { block: "center" })));
  return row;
}

function button(text, className, onClick, disabled = false) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = text;
  b.disabled = disabled;
  b.addEventListener("click", onClick);
  return b;
}

// ------------------------------------------------------------------ actions

function startReview(drafts) {
  reviewIndex = 0;
  render();
  focusDraft(drafts[0]);
}

function stopReview() {
  reviewIndex = null;
  render();
}

function focusDraft(field) {
  if (field) highlightField(field.field_id, { block: "center" });
}

async function confirmCurrent(drafts) {
  // Read before confirming: applying the write re-renders, and render() ends the
  // review when the index falls off the shortened list — so by the time this
  // resumes, reviewIndex may already be null.
  const idx = reviewIndex;
  const current = drafts[idx];
  if (!current) return stopReview();
  if (!await confirmMany([current])) return;

  // The confirmed field has left the list, so the same index is already the
  // next draft. It only moves when someone confirmed out of order elsewhere.
  const remaining = draftFields();
  if (!remaining.length) return stopReview();
  reviewIndex = Math.min(idx, remaining.length - 1);
  render();
  focusDraft(remaining[reviewIndex]);
}

async function confirmAll(drafts) {
  await confirmMany(drafts);
}

/** Resolves true when the batch was actually attempted. */
async function confirmMany(fields) {
  if (busy || !fields.length) return false;
  busy = true;
  render();

  const updates = fields.map((f) => ({
    field_id: f.field_id,
    confirm: true,
    // Undefined when we never learned the version — the server then writes
    // unconditionally, which is the old behaviour and not something to pretend
    // away here. tools.py sends it with every agent write so this is populated.
    expected_version: (state.values[f.field_id] || {}).version,
  }));

  try {
    const res = await api.setFields(state.sid, updates);
    const results = res.results || [];

    // Apply the ones that landed in a single pass, so the panel and the document
    // rebuild once for the whole batch instead of once per field.
    const patches = {};
    let rejected = 0;
    results.forEach((result, i) => {
      const fieldId = updates[i].field_id;
      if (result?.ok) patches[fieldId] = { confirmed: true, version: result.version };
      else rejected += 1;
    });
    applyFieldUpdates(patches);

    // One refetch for the whole batch. Per-field it would be forty round trips
    // to learn the same thing, and each one re-renders everything.
    if (rejected) onNeedsRefetch?.(COPY.someFailed(rejected));
  } catch {
    /* transient network failure — the drafts stay listed and the bar retries */
  } finally {
    busy = false;
    render();
  }
  return true;
}
