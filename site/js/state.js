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
