// The form-field list, moved out of the right-hand column.
//
// It used to share 400px with the chat, which meant you could see the whole of
// neither: a 97-field form scrolled inside a third of a column while the
// conversation about it scrolled inside another third. Splitting them by *time*
// instead of by space gives each one the room it needs — the chat is the column,
// the list is this dialog.
//
// Native <dialog> + showModal(): Esc, the focus trap and focus restore come from
// the platform, and the top layer puts this above .viewer-toolbar's z-index:5
// without touching z-index at all. See css/styles.css, "fields modal".
//
// The one thing showModal() costs is that the page behind goes inert — and the
// panel has a button ("Place it") whose whole instruction is "now drag a box on
// the document". So this module watches state.placingFieldId, closes itself the
// moment that button is pressed, and offers the list back once the box lands.

import { state, onChange, draftFields, unplacedFields } from "./state.js";

const RESUME_HOLD_MS = 9000;

let dialog = null;
let tabs = [];
let panelsByKey = new Map();
let openButtons = [];
let badgeEl = null;

let resumeEl = null;
let resumeTimer = null;

// Distinguishes "we took this away" from "they closed it". Only the first earns
// an offer to come back.
let closedForPlacing = false;
let lastPlacingFieldId = null;

/**
 * @param {object}            opts
 * @param {HTMLDialogElement} opts.dialogEl  #fields-modal
 * @param {HTMLElement[]}     opts.buttons   everything that opens it
 * @param {HTMLElement|null}  opts.badge     the count pill inside the topbar button
 */
export function initFieldsModal({ dialogEl, buttons = [], badge = null }) {
  dialog = dialogEl;
  openButtons = buttons.filter(Boolean);
  badgeEl = badge;

  initSideTabs();

  for (const btn of openButtons) btn.addEventListener("click", () => open());
  for (const btn of dialog.querySelectorAll("[data-action=close-fields]")) {
    btn.addEventListener("click", () => close());
  }
  dialog.addEventListener("click", onBackdropClick);
  // Fires for Esc, for close(), and for a form[method=dialog] submit alike, so
  // every exit route cleans up in one place.
  dialog.addEventListener("close", hideResume);

  onChange(onStateChange);
  onStateChange();
}

// ---------------------------------------------------------------- open/close

export function open(tabKey = null) {
  if (!dialog || dialog.open) return;
  if (tabKey) showSideTab(tabKey);
  hideResume();
  // Reopening by hand ends the placing round-trip, whatever happens to
  // placingFieldId afterwards — otherwise the offer to come back arrives while
  // you are already looking at the list.
  closedForPlacing = false;
  // showModal() moves focus to the first focusable descendant — the "Form
  // fields" tab. Deliberate: focusing a field input instead would raise the
  // on-screen keyboard on every open on a phone.
  dialog.showModal();
}

export function close() {
  if (dialog?.open) dialog.close();
}

export function isOpen() {
  return Boolean(dialog?.open);
}

/** A click on the scrim targets the <dialog> itself, because the backdrop is its
 * pseudo-element. The rect test is the part usually left out: a click on the
 * dialog's own padding, or the tail end of a <select> interaction, also targets
 * the dialog and must not close it. */
function onBackdropClick(e) {
  if (e.target !== dialog) return;
  const r = dialog.getBoundingClientRect();
  const inside =
    e.clientX >= r.left && e.clientX <= r.right &&
    e.clientY >= r.top && e.clientY <= r.bottom;
  if (!inside) close();
}

// ------------------------------------------------------------------ the tabs
// The tabs live in this dialog now. guide-panel.js needs to switch to the fields
// tab before it jumps to a row, so the switcher is exported from here — this
// module imports only state.js, so there is no cycle.

function initSideTabs() {
  tabs = Array.from(document.querySelectorAll("[data-side-tab]"));
  panelsByKey = new Map(
    Array.from(document.querySelectorAll("[data-side-panel]"))
      .map((el) => [el.dataset.sidePanel, el])
  );
  for (const tab of tabs) tab.addEventListener("click", () => activate(tab));
}

export function showSideTab(key) {
  const tab = tabs.find((t) => t.dataset.sideTab === key);
  if (tab) activate(tab);
}

function activate(tab) {
  for (const t of tabs) {
    const on = t === tab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
  }
  for (const [key, panel] of panelsByKey) {
    panel.classList.toggle("hidden", key !== tab.dataset.sideTab);
  }
}

// ----------------------------------------------------------------- the store

function onStateChange() {
  syncBadge();

  const placing = state.placingFieldId;

  // "Place it" was pressed. The instruction it just printed is "drag a box on
  // the document", and showModal() has made the document inert — so the dialog
  // gets out of the way rather than asking for a gesture the page will refuse.
  if (placing && placing !== lastPlacingFieldId && isOpen()) {
    closedForPlacing = true;
    close();
  }

  // The drag landed (viewer.js clears placingFieldId on a good drop), or placing
  // was abandoned from the viewer toolbar. Offer the list back — an offer, not a
  // snap-back, because reopening on top of the box you just drew hides the one
  // thing you were trying to see.
  if (!placing && lastPlacingFieldId && closedForPlacing) {
    closedForPlacing = false;
    showResume(lastPlacingFieldId);
  }

  lastPlacingFieldId = placing;
}

/** The list is behind a button now, so nothing else on screen would say that
 * four values are waiting in it. Counts exactly what the panel is the only place
 * to fix: unconfirmed agent drafts, and fields with no box. */
function syncBadge() {
  const drafts = draftFields().length;
  const unplaced = unplacedFields().length;
  const n = drafts + unplaced;

  if (badgeEl) {
    badgeEl.textContent = n ? String(n) : "";
    badgeEl.classList.toggle("hidden", !n);
  }

  const parts = [];
  if (drafts) parts.push(`${drafts} to confirm`);
  if (unplaced) parts.push(`${unplaced} with no box`);
  const title = parts.length ? `Form fields — ${parts.join(", ")}` : "Form fields";
  for (const btn of openButtons) {
    btn.title = title;
    btn.setAttribute("aria-label", title);
  }
}

// ------------------------------------------------------------- resume prompt

function showResume(fieldId) {
  const label = state.fieldsById.get(fieldId)?.label || "that field";

  if (!resumeEl) {
    resumeEl = document.createElement("div");
    resumeEl.className = "resume-fields";
    resumeEl.setAttribute("role", "status");

    const text = document.createElement("span");
    text.className = "resume-text";
    text.setAttribute("dir", "auto");

    const back = document.createElement("button");
    back.type = "button";
    back.className = "small primary";
    back.textContent = "Back to the list";
    back.addEventListener("click", () => { hideResume(); open("fields"); });

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "small ghost";
    dismiss.textContent = "Not now";
    dismiss.addEventListener("click", hideResume);

    resumeEl.append(text, back, dismiss);
    document.body.appendChild(resumeEl);
  }

  resumeEl.querySelector(".resume-text").textContent = `Placed ${label}.`;
  resumeEl.classList.remove("hidden");
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(hideResume, RESUME_HOLD_MS);
}

function hideResume() {
  clearTimeout(resumeTimer);
  resumeTimer = null;
  resumeEl?.classList.add("hidden");
}
