// The document viewer: one <img> per rasterized page (already produced by
// ingest_extract.py's _rasterize, for all doc types including DOCX-derived
// PDFs) with field boxes positioned by percentage from the normalized
// [x0,y0,x1,y1] bbox — no PDF.js needed.
//
// Each box also draws its current value, so the document itself shows what has
// been filled rather than only an outline — the only way to check placement
// before the final render, which is strict and downloads rather than displays.
// It's a toggle because the overlay covers the form's own printed text.

import { state, onChange, toggleSelected } from "./state.js";

const SHOW_VALUES_KEY = "fa.showValues";

let pane;          // .viewer-pane — owns the toolbar, never cleared
let container;     // .pages — rebuilt on every render
let toolbar;
let toggleBtn;
let showValues = loadShowValues();
let measureQueued = false;

export function initViewer(el) {
  pane = el;
  pane.innerHTML = "";
  toolbar = buildToolbar();
  container = document.createElement("div");
  container.className = "pages";
  pane.append(toolbar, container);
  // A box's size is a percentage of the page image, which is itself
  // responsive — so "does this value still fit" can only be answered after
  // layout settles, and again after every resize.
  new ResizeObserver(queueMeasure).observe(container);
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

// ------------------------------------------------------------------ toolbar

function buildToolbar() {
  const bar = document.createElement("div");
  bar.className = "viewer-toolbar hidden";
  toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "small";
  toggleBtn.addEventListener("click", () => setShowValues(!showValues));
  bar.appendChild(toggleBtn);
  syncToggle();
  return bar;
}

function syncToggle() {
  toggleBtn.textContent = showValues ? "Hide values" : "Show values";
  toggleBtn.classList.toggle("active", showValues);
  toggleBtn.setAttribute("aria-pressed", String(showValues));
}

function setShowValues(on) {
  showValues = on;
  try {
    localStorage.setItem(SHOW_VALUES_KEY, on ? "1" : "0");
  } catch {
    /* private browsing — the preference just doesn't persist */
  }
  syncToggle();
  render();
}

function loadShowValues() {
  try {
    return localStorage.getItem(SHOW_VALUES_KEY) !== "0";
  } catch {
    return true;
  }
}

// ------------------------------------------------------------------- render

function render() {
  if (!container) return;
  toolbar.classList.toggle("hidden", !state.pageUrls.length);
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
    // Until the image lands, every box is 0px wide and would measure as
    // overflowing. Re-check once it has real dimensions.
    img.addEventListener("load", queueMeasure);
    pageEl.appendChild(img);

    for (const f of fieldsByPage.get(pageNo) || []) {
      pageEl.appendChild(fieldBox(f));
    }
    container.appendChild(pageEl);
  });

  queueMeasure();
}

function fieldBox(f) {
  const [x0, y0, x1, y1] = f.bbox && f.bbox.length === 4 ? f.bbox : [0, 0, 0, 0];
  const box = document.createElement("div");
  box.className = "field-box";
  box.dataset.fieldId = f.field_id;
  box.dataset.type = f.type || "text";
  box.dataset.label = f.label || "";
  box.title = box.dataset.label;
  box.style.left = `${x0 * 100}%`;
  box.style.top = `${y0 * 100}%`;
  box.style.width = `${Math.max(0, x1 - x0) * 100}%`;
  box.style.height = `${Math.max(0, y1 - y0) * 100}%`;

  const v = state.values[f.field_id] || {};
  const hasValue = v.value !== null && v.value !== undefined && v.value !== "";
  if (hasValue) box.classList.add("has-value");
  if (v.source === "agent" && !v.confirmed) box.classList.add("draft");
  if (state.selectedFieldIds.has(f.field_id)) box.classList.add("selected");

  if (showValues) {
    const text = displayValue(f, v.value);
    if (text) box.appendChild(valueOverlay(text));
  }

  box.addEventListener("click", () => toggleSelected(f.field_id));
  return box;
}

/** What the renderer would stamp here — see api_render.py's _draw_field. */
function displayValue(f, value) {
  if (value === null || value === undefined || value === "") return "";
  // The renderer draws an X; the glyph differs, the placement is the point.
  if (f.type === "checkbox") return value ? "\u2713" : "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function valueOverlay(text) {
  const wrap = document.createElement("span");
  wrap.className = "field-value";
  // dir=auto resolves to the same side the renderer picks: Hebrew hugs the
  // right edge (drawRightString), a leading digit hugs the left (drawString),
  // because api_render.py's _is_rtl keys off the same thing — Hebrew letters.
  wrap.setAttribute("dir", "auto");
  const ink = document.createElement("span");
  ink.className = "ink";
  ink.textContent = text;
  wrap.appendChild(ink);
  return wrap;
}

// ------------------------------------------------------------------ overflow
// A value wider than its box will run over its neighbours in the output too,
// so flag it here rather than letting it surface only in the downloaded file.

function queueMeasure() {
  if (measureQueued) return;
  measureQueued = true;
  requestAnimationFrame(() => {
    measureQueued = false;
    measureOverflow();
  });
}

function measureOverflow() {
  if (!container) return;
  for (const box of container.querySelectorAll(".field-box")) {
    const ink = box.querySelector(".field-value > .ink");
    const width = box.clientWidth;
    // 0.94 accounts for .field-value's 3% inline padding on either side.
    const overflows = Boolean(ink) && width > 1 && ink.getBoundingClientRect().width > width * 0.94;
    box.classList.toggle("overflows", overflows);
    box.title = overflows
      ? `${box.dataset.label} — this value is wider than the field`.trim()
      : box.dataset.label;
  }
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}
