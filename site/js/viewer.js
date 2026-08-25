// The document viewer: one <img> per rasterized page (already produced by
// ingest_extract.py's _rasterize, for all doc types including DOCX-derived
// PDFs) with field boxes positioned by percentage from the normalized
// [x0,y0,x1,y1] bbox — no PDF.js needed.

import { state, onChange, toggleSelected } from "./state.js";

let container;

export function initViewer(el) {
  container = el;
  onChange(render);
}

export function highlightField(fieldId) {
  const box = container?.querySelector(`.field-box[data-field-id="${cssEscape(fieldId)}"]`);
  if (!box) return;
  box.scrollIntoView({ behavior: "smooth", block: "center" });
  box.classList.remove("pulse");
  // Force reflow so the animation restarts if the field was just highlighted.
  void box.offsetWidth;
  box.classList.add("pulse");
}

function render() {
  if (!container) return;
  if (!state.pageUrls.length) {
    container.innerHTML = "";
    return;
  }
  // Full re-render on every state change is simple and fast enough here —
  // forms in this product are tens of fields, not thousands.
  container.innerHTML = "";

  const fieldsByPage = new Map();
  for (const f of state.fields) {
    const key = f.page || 1;
    if (!fieldsByPage.has(key)) fieldsByPage.set(key, []);
    fieldsByPage.get(key).push(f);
  }

  state.pageUrls.forEach((url, i) => {
    const pageNo = i + 1;
    const pageEl = document.createElement("div");
    pageEl.className = "page";

    const img = document.createElement("img");
    img.src = url;
    img.alt = `Page ${pageNo}`;
    img.draggable = false;
    pageEl.appendChild(img);

    for (const f of fieldsByPage.get(pageNo) || []) {
      pageEl.appendChild(fieldBox(f));
    }
    container.appendChild(pageEl);
  });
}

function fieldBox(f) {
  const [x0, y0, x1, y1] = f.bbox && f.bbox.length === 4 ? f.bbox : [0, 0, 0, 0];
  const box = document.createElement("div");
  box.className = "field-box";
  box.dataset.fieldId = f.field_id;
  box.title = f.label;
  box.style.left = `${x0 * 100}%`;
  box.style.top = `${y0 * 100}%`;
  box.style.width = `${Math.max(0, x1 - x0) * 100}%`;
  box.style.height = `${Math.max(0, y1 - y0) * 100}%`;

  const v = state.values[f.field_id] || {};
  const hasValue = v.value !== null && v.value !== undefined && v.value !== "";
  if (hasValue) box.classList.add("has-value");
  if (v.source === "agent" && !v.confirmed) box.classList.add("draft");
  if (state.selectedFieldIds.has(f.field_id)) box.classList.add("selected");

  box.addEventListener("click", () => toggleSelected(f.field_id));
  return box;
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}
