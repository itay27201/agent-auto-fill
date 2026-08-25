// The guide pane on session.html: official guidance for the form in front of
// the person filling it.
//
// The agent can already answer from this, but a person filling a government
// form usually wants to read the requirements themselves before they start —
// what to attach, whether they qualify at all. Making them ask an assistant
// for something the agency published is friction, not help.
//
// Only shown when the session has a guide, which means the form came from the
// catalog or an upload hash-matched a published one.

import { state, onChange } from "./state.js";
import { renderMarkdown, escapeHtml } from "./md.js";

// Order the reader cares about, not the storage order: whether this applies to
// them, then what they need in hand, before the rest.
const READING_ORDER = [
  "Overview",
  "Eligibility",
  "Required attachments",
  "Key rules",
  "Common mistakes",
  "Where to submit",
];

export function initGuidePanel(root, tabButton) {
  let rendered = false;

  onChange(() => {
    const guide = state.session?.guide;
    const has = Boolean(guide) && hasContent(guide);
    tabButton?.classList.toggle("hidden", !has);
    if (has && !rendered) {
      render(root, guide);
      rendered = true;
    }
  });
}

function hasContent(guide) {
  const sections = guide.sections || {};
  return READING_ORDER.some((n) => (sections[n] || "").trim())
    || Object.keys(guide.field_notes || {}).length > 0;
}

function render(root, guide) {
  const sections = guide.sections || {};
  const meta = guide.meta || {};
  root.innerHTML = "";

  const head = document.createElement("div");
  head.className = "guide-head";
  head.setAttribute("dir", "auto");
  head.innerHTML = `
    <strong>${escapeHtml(meta.name || "Official guidance")}</strong>
    ${meta.agency ? `<span>${escapeHtml(meta.agency)}</span>` : ""}
    <p class="hint-inline">Written for this form. The assistant answers from it too.</p>`;
  root.appendChild(head);

  for (const name of READING_ORDER) {
    const body = (sections[name] || "").trim();
    if (!body) continue;
    root.appendChild(section(name, renderMarkdown(body)));
  }

  const notes = guide.field_notes || {};
  const ids = Object.keys(notes);
  if (ids.length) {
    const list = ids
      .map((fid) => {
        const label = state.fieldsById.get(fid)?.label || fid;
        return `<div class="guide-note" data-field="${escapeHtml(fid)}">
          <button type="button" class="guide-note-label">${escapeHtml(label)}</button>
          <div class="guide-note-body">${renderMarkdown(notes[fid])}</div>
        </div>`;
      })
      .join("");
    const el = section("Notes on individual fields", list);
    // Clicking a note jumps to that field's row, which is the reason to read
    // the note in the first place.
    el.addEventListener("click", (ev) => {
      const wrap = ev.target.closest(".guide-note");
      if (!wrap || !ev.target.closest(".guide-note-label")) return;
      const row = document.querySelector(`.field-row[data-field-id="${cssEscape(wrap.dataset.field)}"]`);
      row?.scrollIntoView({ behavior: "smooth", block: "center" });
      row?.classList.add("flash");
      setTimeout(() => row?.classList.remove("flash"), 1200);
    });
    root.appendChild(el);
  }
}

// Same guard fields-panel.js uses: field_ids come from the document, so they
// can contain characters a selector would choke on.
function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
}

function section(title, innerHtml) {
  const el = document.createElement("details");
  el.className = "guide-section";
  el.open = title === "Overview" || title === "Required attachments";
  el.setAttribute("dir", "auto");
  el.innerHTML = `<summary>${escapeHtml(title)}</summary><div class="guide-body">${innerHtml}</div>`;
  return el;
}
