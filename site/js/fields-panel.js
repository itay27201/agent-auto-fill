// Manual-fill panel: one input per field, grouped by section, kept in sync
// with the agent's writes over the WebSocket. PATCHes are conditional on
// `version` so a lost race against the agent (or another tab) comes back
// as a per-field conflict instead of silently clobbering a value.

import {
  state,
  onChange,
  applyFieldUpdate,
  toggleSelected,
  setSectionSelected,
  fieldsBySection,
} from "./state.js";

let container;
let api;
let onNeedsRefetch;

const TYPE_TAGS = {
  date: "Date",
  number: "Number",
  signature: "Signature",
  checkbox: "Yes / No",
  select: "Choice",
  multiselect: "Multi-choice",
  textarea: "Long text",
};

export function initFieldsPanel(el, apiClient, refetchFn) {
  container = el;
  api = apiClient;
  onNeedsRefetch = refetchFn;
  onChange(render);
}

function render() {
  if (!container) return;
  const focusInfo = captureFocus();
  container.innerHTML = "";

  for (const [section, fields] of fieldsBySection()) {
    if (section) container.appendChild(sectionHeading(section, fields));
    for (const f of fields) container.appendChild(fieldRow(f));
  }

  restoreFocus(focusInfo);
}

function captureFocus() {
  const active = container.querySelector(":focus");
  if (!active) return null;
  return { fieldId: active.closest(".field-row")?.dataset.fieldId, selStart: active.selectionStart };
}

function restoreFocus(info) {
  if (!info?.fieldId) return;
  const el = container.querySelector(`.field-row[data-field-id="${cssEscape(info.fieldId)}"] input, .field-row[data-field-id="${cssEscape(info.fieldId)}"] textarea`);
  if (el) {
    el.focus();
    if (info.selStart != null && el.setSelectionRange) {
      try { el.setSelectionRange(info.selStart, info.selStart); } catch { /* not a text input */ }
    }
  }
}

function sectionHeading(section, fields) {
  const allSelected = fields.every((f) => state.selectedFieldIds.has(f.field_id));
  const el = document.createElement("div");
  el.className = "section-heading";
  const label = document.createElement("span");
  label.textContent = section;
  label.setAttribute("dir", "auto");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "small";
  btn.textContent = allSelected ? "Deselect section" : "Discuss this section";
  btn.addEventListener("click", () => setSectionSelected(section, !allSelected));
  el.append(label, btn);
  return el;
}

function fieldRow(f) {
  const v = state.values[f.field_id] || {};
  const isDraft = v.source === "agent" && !v.confirmed;
  const selected = state.selectedFieldIds.has(f.field_id);

  const row = document.createElement("div");
  row.className = "field-row" + (isDraft ? " draft" : "") + (selected ? " selected" : "");
  row.dataset.fieldId = f.field_id;
  row.dataset.type = f.type || "text";

  const head = document.createElement("div");
  head.className = "row-head";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "select-toggle";
  toggle.title = "Select for chat";
  toggle.textContent = selected ? "\u2713" : "";
  toggle.addEventListener("click", () => toggleSelected(f.field_id));

  const label = document.createElement("label");
  label.className = "field-label";
  label.setAttribute("dir", "auto");
  label.textContent = f.label + (f.required ? " " : "");
  if (f.required) {
    const star = document.createElement("span");
    star.className = "req";
    star.textContent = "*";
    label.appendChild(star);
  }

  head.append(toggle, label);

  const typeTag = TYPE_TAGS[f.type];
  if (typeTag) {
    const tag = document.createElement("span");
    tag.className = "field-type-tag";
    tag.textContent = typeTag;
    head.appendChild(tag);
  }

  row.appendChild(head);
  row.appendChild(inputFor(f, v));

  if (isDraft) {
    const banner = document.createElement("div");
    banner.className = "draft-banner";
    const text = document.createElement("span");
    text.textContent = "Drafted by the assistant \u2014 confirm to keep it.";
    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = "small";
    confirmBtn.textContent = "Confirm";
    confirmBtn.addEventListener("click", () => confirmField(f.field_id, v.version));
    banner.append(text, confirmBtn);
    row.appendChild(banner);
  }

  return row;
}

function inputFor(f, v) {
  const wrap = document.createElement("div");

  if (f.type === "checkbox") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(v.value);
    input.addEventListener("change", () => saveField(f, input.checked));
    wrap.appendChild(input);
    return wrap;
  }

  if (f.type === "select") {
    const select = document.createElement("select");
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "\u2014";
    select.appendChild(blank);
    for (const opt of f.options || []) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      if (v.value === opt) o.selected = true;
      select.appendChild(o);
    }
    select.addEventListener("change", () => saveField(f, select.value || null));
    wrap.appendChild(select);
    return wrap;
  }

  if (f.type === "multiselect") {
    wrap.className = "multiselect-options";
    const current = Array.isArray(v.value) ? v.value : [];
    for (const opt of f.options || []) {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = current.includes(opt);
      cb.addEventListener("change", () => {
        const next = new Set(current);
        cb.checked ? next.add(opt) : next.delete(opt);
        saveField(f, Array.from(next));
      });
      label.append(cb, document.createTextNode(" " + opt));
      wrap.appendChild(label);
    }
    return wrap;
  }

  if (f.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = v.value ?? "";
    textarea.setAttribute("dir", "auto");
    textarea.addEventListener("change", () => saveField(f, textarea.value));
    wrap.appendChild(textarea);
    return wrap;
  }

  // text, number, date, signature: a plain text input. Format hints (date
  // shape, id length) live in `validation`/`help` and are enforced
  // server-side, so no native <input type=date|number> quirks to fight.
  const input = document.createElement("input");
  input.type = "text";
  input.value = v.value ?? "";
  input.setAttribute("dir", "auto");
  if (f.help) input.placeholder = f.help;
  input.addEventListener("change", () => saveField(f, input.value));
  wrap.appendChild(input);
  return wrap;
}

async function saveField(f, value) {
  const current = state.values[f.field_id] || {};
  const expectedVersion = current.version;
  applyFieldUpdate(f.field_id, { value, source: "user", confirmed: true });
  try {
    const res = await api.setFields(state.sid, [
      { field_id: f.field_id, value, expected_version: expectedVersion },
    ]);
    const result = res.results?.[0];
    if (!result?.ok) handleRejected(f.field_id, result);
    else applyFieldUpdate(f.field_id, { version: (expectedVersion ?? 0) + 1 });
  } catch {
    /* transient network failure — the field keeps its optimistic local value */
  }
}

async function confirmField(fieldId, expectedVersion) {
  try {
    const res = await api.setFields(state.sid, [
      { field_id: fieldId, confirm: true, expected_version: expectedVersion },
    ]);
    const result = res.results?.[0];
    if (!result?.ok) return handleRejected(fieldId, result);
    applyFieldUpdate(fieldId, { confirmed: true, version: (expectedVersion ?? 0) + 1 });
  } catch {
    /* transient network failure — the draft banner just stays up for retry */
  }
}

function handleRejected(fieldId, result) {
  if (result?.error === "version_conflict") {
    onNeedsRefetch?.(`"${fieldId}" changed elsewhere \u2014 refreshed its value.`);
  }
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}
