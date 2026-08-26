// Small shared store for session.html. No framework: modules read `state`
// directly and call `onChange(fn)` to re-render when it's mutated.

export const state = {
  sid: null,
  session: null,       // raw GET /sessions/{id} response
  fields: [],           // FormField dicts
  fieldsById: new Map(),
  values: {},            // field_id -> {value, source, confirmed, version, ...}
  pageUrls: [],
  selectedFieldIds: new Set(),
  // Box-placing mode. `placing` turns every box into something draggable;
  // `placingFieldId` is the unplaced field waiting to be drawn onto the page.
  placing: false,
  placingFieldId: null,
};

const listeners = new Set();

export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) fn(state);
}

export function setSession(session) {
  state.session = session;
  state.sid = session.session_id;
  if (session.fields) {
    state.fields = session.fields;
    state.fieldsById = new Map(session.fields.map((f) => [f.field_id, f]));
  }
  if (session.values) state.values = session.values;
  if (session.page_urls) state.pageUrls = session.page_urls;
  notify();
}

export function applyFieldUpdate(fieldId, patch) {
  const prev = state.values[fieldId] || {};
  state.values[fieldId] = { ...prev, ...patch };
  notify();
}

/** Many patches, one notify. The agent writes a field at a time over the
 * WebSocket, and every listener here rebuilds its whole DOM — so a forty-field
 * turn applied one at a time is eighty rebuilds, and the animation that shows
 * the writing lands on elements that are replaced before it finishes.
 * `patches` is field_id -> patch. */
export function applyFieldUpdates(patches) {
  let touched = false;
  for (const [fieldId, patch] of Object.entries(patches)) {
    const prev = state.values[fieldId] || {};
    state.values[fieldId] = { ...prev, ...patch };
    touched = true;
  }
  if (touched) notify();
}

/** A box someone moved. Patches the schema entry, not the value — geometry and
 * content are separate stores on the backend for the same reason. */
export function applyBoxUpdate(fieldId, bbox, page) {
  const f = state.fieldsById.get(fieldId);
  if (!f) return;
  f.bbox = bbox;
  if (page) f.page = page;
  f.bbox_confidence = "ok";
  f.bbox_source = "user";
  f.bbox_note = "";
  notify();
}

/** Fields ingest could not place. They have no box on the page, so they are
 * unreachable from the document view — the panel is the only way to find them,
 * and the renderer refuses to export a value sitting in one. */
export function isUnplaced(f) {
  return f?.bbox_confidence === "low";
}

export function unplacedFields() {
  return state.fields.filter(isUnplaced);
}

/** Values the agent wrote that nobody has signed off on yet. The renderer
 * refuses to export while any of these remain, so this is the list standing
 * between a filled form and a filed one. */
export function draftFields() {
  return state.fields.filter((f) => {
    const v = state.values[f.field_id] || {};
    return v.source === "agent" && !v.confirmed;
  });
}

export function setPlacing(fieldId) {
  state.placingFieldId = fieldId || null;
  notify();
}

export function toggleSelected(fieldId) {
  if (state.selectedFieldIds.has(fieldId)) state.selectedFieldIds.delete(fieldId);
  else state.selectedFieldIds.add(fieldId);
  notify();
}

export function setSectionSelected(section, selected) {
  for (const f of state.fields) {
    if ((f.section || "") !== section) continue;
    if (selected) state.selectedFieldIds.add(f.field_id);
    else state.selectedFieldIds.delete(f.field_id);
  }
  notify();
}

export function clearSelection() {
  state.selectedFieldIds.clear();
  notify();
}

export function fieldsBySection() {
  const bySection = new Map();
  for (const f of state.fields) {
    const key = f.section || "";
    if (!bySection.has(key)) bySection.set(key, []);
    bySection.get(key).push(f);
  }
  return bySection;
}
