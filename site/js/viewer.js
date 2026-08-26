// The document viewer: one <img> per rasterized page (already produced by
// ingest_extract.py's _rasterize, for all doc types including DOCX-derived
// PDFs) with field boxes positioned by percentage from the normalized
// [x0,y0,x1,y1] bbox — no PDF.js needed.
//
// Each box also draws its current value, so the document itself shows what has
// been filled rather than only an outline — the only way to check placement
// before the final render, which is strict and downloads rather than displays.
// It's a toggle because the overlay covers the form's own printed text.
//
// Boxes are also editable here. Ingest reads them out of the PDF's own ruled
// geometry and refuses any that fail a sanity check, but a form it reads wrongly
// used to be unfixable by anyone: no API route, no UI, no agent tool could move
// a bbox, and the bad schema went into a registry with no TTL. "Place boxes"
// turns every box into something you can drag, and gives the ones ingest
// declined somewhere to be drawn.

import {
  state,
  onChange,
  notify,
  toggleSelected,
  applyBoxUpdate,
  setPlacing,
  unplacedFields,
  isUnplaced,
} from "./state.js";

const SHOW_VALUES_KEY = "fa.showValues";
const MIN_SIZE = 0.004;   // below this a drag was a click, not a box

let pane;          // .viewer-pane — owns the toolbar, never cleared
let container;     // .pages — rebuilt on every render
let toolbar;
let toggleBtn;
let placeBtn;
let api;
let announcer;     // the floating "the assistant filled N" line over the pages
let onPlaced;      // told what moved, so the panel and status can react
let onOverflow;    // told which fields no longer fit their box
let writtenTotal = 0;
let announceTimer = null;
let showValues = loadShowValues();
let measureQueued = false;
let drag = null;   // the gesture in flight; suppresses re-render mid-drag
let justWritten = new Set();   // fields to animate on the next render — see markWritten

export function initViewer(el, apiClient, placedCallback, overflowCallback) {
  pane = el;
  api = apiClient;
  onPlaced = placedCallback || (() => {});
  onOverflow = overflowCallback || (() => {});
  pane.innerHTML = "";
  toolbar = buildToolbar();
  container = document.createElement("div");
  container.className = "pages";
  // Floats over the pages rather than sitting in the side panel: what it counts
  // is happening on the document, and a blind user gets the same sentence read
  // out — until now, twelve fields could be filled with no notification at all.
  announcer = document.createElement("div");
  announcer.className = "agent-activity hidden";
  announcer.setAttribute("role", "status");
  announcer.setAttribute("aria-live", "polite");
  pane.append(toolbar, container, announcer);
  // A box's size is a percentage of the page image, which is itself
  // responsive — so "does this value still fit" can only be answered after
  // layout settles, and again after every resize.
  new ResizeObserver(queueMeasure).observe(container);
  onChange(render);
}

/** Scroll a field into view and pulse it. `block` is the caller's intent: an
 * agent pointing at something centres it, while a write asks for `nearest`,
 * which does not scroll at all when the box is already visible. */
export function highlightField(fieldId, { block = "center" } = {}) {
  const box = container?.querySelector(`.field-box[data-field-id="${cssEscape(fieldId)}"]`);
  if (!box) return;
  box.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block });
  box.classList.remove("pulse");
  // Force reflow so the animation restarts if the field was just highlighted.
  void box.offsetWidth;
  box.classList.add("pulse");
}

/** Name the fields whose values are about to change, so the next render can
 * animate those boxes and no others. The store notifies straight after, and
 * render() consumes the set as it rebuilds — a box that was already on screen
 * unchanged must not flash just because something else was written. */
export function markWritten(fieldIds) {
  for (const id of fieldIds) justWritten.add(id);
}

const ANNOUNCE_HOLD_MS = 3500;

/** Say how much the agent has written this session. Called after the batch has
 * been applied, so the draft count it quotes is the real one. */
export function announceWrites(count, draftCount) {
  if (!announcer || !count) return;
  writtenTotal += count;
  const filled = `The assistant filled ${writtenTotal} field${writtenTotal === 1 ? "" : "s"}`;
  announcer.textContent = draftCount
    ? `${filled} · ${draftCount} waiting for you`
    : filled;
  announcer.classList.remove("hidden");
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => announcer.classList.add("hidden"), ANNOUNCE_HOLD_MS);
}

function reducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

// ------------------------------------------------------------------ toolbar

function buildToolbar() {
  const bar = document.createElement("div");
  bar.className = "viewer-toolbar hidden";

  toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "small";
  toggleBtn.addEventListener("click", () => setShowValues(!showValues));

  placeBtn = document.createElement("button");
  placeBtn.type = "button";
  placeBtn.className = "small";
  placeBtn.addEventListener("click", () => setPlacingMode(!state.placing));

  bar.append(toggleBtn, placeBtn);
  syncToggle();
  return bar;
}

function syncToggle() {
  toggleBtn.textContent = showValues ? "Hide values" : "Show values";
  toggleBtn.classList.toggle("active", showValues);
  toggleBtn.setAttribute("aria-pressed", String(showValues));

  const unplaced = unplacedFields().length;
  placeBtn.textContent = state.placing
    ? "Done placing"
    : unplaced
      ? `Place boxes (${unplaced})`
      : "Place boxes";
  placeBtn.classList.toggle("active", state.placing);
  placeBtn.classList.toggle("needs-attention", !state.placing && unplaced > 0);
  placeBtn.setAttribute("aria-pressed", String(state.placing));
}

function setPlacingMode(on) {
  state.placing = on;
  state.placingFieldId = on ? state.placingFieldId : null;
  // notify, not render: the fields panel draws the "place it" banner and has to
  // move in step with the viewer, or the two disagree about what mode we're in.
  notify();
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
  // A re-render mid-gesture would replace the element under the pointer with a
  // fresh one and drop the drag. The final position lands via applyBoxUpdate.
  if (drag) return;
  toolbar.classList.toggle("hidden", !state.pageUrls.length);
  syncToggle();
  if (!state.pageUrls.length) {
    container.innerHTML = "";
    return;
  }
  // Full re-render on every state change is simple and fast enough here —
  // forms in this product are tens of fields, not thousands.
  container.innerHTML = "";

  container.classList.toggle("placing", state.placing);

  const fieldsByPage = new Map();
  for (const f of state.fields) {
    // A field ingest could not place has no box, so drawing one would put it at
    // the page's top-left corner on top of whatever is printed there. It lives
    // in the panel until someone draws it in placing mode.
    if (isUnplaced(f)) continue;
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
      // A choice printed as a row of tick squares is marked in the square it
      // chose, which is not the square the field is anchored on. Drawn as
      // separate elements because the field carries one bbox and this needs one
      // per chosen option — the same reason api_render.py has _draw_choice.
      for (const mark of choiceMarks(f)) pageEl.appendChild(mark);
    }
    pageEl.addEventListener("pointerdown", (e) => startDraw(e, pageEl, pageNo));
    container.appendChild(pageEl);
  });

  // Consumed, not accumulated: these boxes have just been built with the
  // animation on them, and a field written while its box was filtered out
  // (unplaced, or on no page at all) must not animate the next time anything
  // else re-renders.
  justWritten = new Set();

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
  if (justWritten.has(f.field_id)) box.classList.add("just-written");

  if (showValues) {
    const text = displayValue(f, v.value);
    if (text) box.appendChild(valueOverlay(text));
  }

  if (state.placing) {
    box.classList.add("movable");
    if (f.bbox_source === "user") box.classList.add("hand-placed");
    for (const corner of ["nw", "ne", "sw", "se"]) {
      const handle = document.createElement("span");
      handle.className = `handle ${corner}`;
      handle.dataset.corner = corner;
      box.appendChild(handle);
    }
    box.addEventListener("pointerdown", (e) => startMove(e, box, f));
  } else {
    box.addEventListener("click", () => toggleSelected(f.field_id));
  }
  return box;
}

/** What the renderer would stamp here — see api_render.py's _draw_field. */
function displayValue(f, value) {
  if (value === null || value === undefined || value === "") return "";
  // This field's mark goes in one of its printed choice squares, drawn by
  // choiceMarks. Showing the chosen option's text here as well would put it in
  // the square the field anchored on, which is the very thing the renderer
  // stopped doing.
  if (optionBoxes(f).length) return "";
  // The renderer draws an X; the glyph differs, the placement is the point.
  if (f.type === "checkbox" || (f.backend || {}).mark === "checkbox") {
    return isTicked(value) ? "\u2713" : "";
  }
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function optionBoxes(f) {
  return (f.backend && f.backend.option_boxes) || [];
}

// Mirrors api_render.py's _NEGATIVE. A tick square should hold a boolean, but a
// field typed wrong upstream reaches here holding whatever the agent wrote.
const NEGATIVE = new Set(["\u05dc\u05d0", "no", "false", "0", "off", "none", "n"]);

/** Mirrors api_render.py's _is_ticked, so the preview cannot promise a mark the
 *  export will not draw. */
function isTicked(value) {
  if (typeof value === "boolean") return value;
  if (value === null || value === undefined) return false;
  const text = (Array.isArray(value) ? value.join(", ") : String(value)).trim();
  if (!text) return false;
  return !NEGATIVE.has(squash(text).toLowerCase());
}

function squash(text) {
  return String(text || "").replace(/[^\p{L}\p{N}]/gu, "");
}

/** A checkmark in each printed square this field's value chose \u2014 the same squares
 *  api_render.py's _draw_choice stamps, matched the same way. */
function choiceMarks(f) {
  const options = optionBoxes(f);
  if (!showValues || !options.length) return [];

  const value = (state.values[f.field_id] || {}).value;
  const list = Array.isArray(value) ? value : [value];
  const chosen = new Set(
    list.filter((v) => v !== null && v !== undefined && v !== "" && typeof v !== "boolean")
      .map((v) => String(v).trim())
  );
  // A lone square answers true or false rather than naming its own choice.
  if (value === true && options.length === 1) chosen.add(String(options[0].value));
  if (!chosen.size) return [];

  const out = [];
  for (const option of options) {
    if (!chosen.has(String(option.value).trim())) continue;
    const bbox = option.bbox && option.bbox.length === 4 ? option.bbox : [0, 0, 0, 0];
    const [x0, y0, x1, y1] = bbox;
    const mark = document.createElement("div");
    mark.className = "choice-mark";
    mark.title = `${f.label}: ${option.value}`;
    mark.style.left = `${x0 * 100}%`;
    mark.style.top = `${y0 * 100}%`;
    mark.style.width = `${Math.max(0, x1 - x0) * 100}%`;
    mark.style.height = `${Math.max(0, y1 - y0) * 100}%`;
    mark.textContent = "\u2713";
    out.push(mark);
  }
  return out;
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

// ------------------------------------------------------------------- placing
// Everything is already positioned as a percentage of the page element, so a
// gesture is just pointer maths against that element's rect — no zoom factor to
// track, and the result is the normalized bbox the schema already speaks.

/** Pointer position as a 0..1 fraction of the page. */
function pagePoint(e, pageEl) {
  const r = pageEl.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
  };
}

function startMove(e, box, field) {
  if (!state.placing || e.button !== 0) return;
  // Otherwise the page's own handler would start drawing a second box under
  // the one being moved.
  e.stopPropagation();
  e.preventDefault();

  const pageEl = box.closest(".page");
  const corner = e.target.dataset?.corner || null;
  const [x0, y0, x1, y1] = field.bbox;
  drag = {
    box,
    pageEl,
    field,
    corner,
    start: pagePoint(e, pageEl),
    origin: [x0, y0, x1, y1],
    moved: false,
  };
  box.setPointerCapture(e.pointerId);
  box.classList.add("dragging");
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragEnd, { once: true });
}

function startDraw(e, pageEl, pageNo) {
  const fieldId = state.placingFieldId;
  if (!state.placing || !fieldId || e.button !== 0) return;
  const field = state.fieldsById.get(fieldId);
  if (!field) return;
  e.preventDefault();

  const at = pagePoint(e, pageEl);
  const box = document.createElement("div");
  box.className = "field-box movable drawing";
  box.dataset.fieldId = fieldId;
  box.dataset.label = field.label || "";
  pageEl.appendChild(box);

  drag = {
    box,
    pageEl,
    field,
    corner: "se",
    start: at,
    origin: [at.x, at.y, at.x, at.y],
    page: pageNo,
    drawing: true,
    moved: false,
  };
  paint([at.x, at.y, at.x, at.y]);
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragEnd, { once: true });
}

function onDragMove(e) {
  if (!drag) return;
  const at = pagePoint(e, drag.pageEl);
  const dx = at.x - drag.start.x;
  const dy = at.y - drag.start.y;
  if (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001) drag.moved = true;

  let [x0, y0, x1, y1] = drag.origin;
  if (drag.corner) {
    // Resize the grabbed corner only. `normalize` puts them back in order when
    // a corner is dragged past its opposite, so a box can be drawn in any
    // direction and inverted coordinates never reach the schema.
    if (drag.corner.includes("n")) y0 = at.y;
    if (drag.corner.includes("s")) y1 = at.y;
    if (drag.corner.includes("w")) x0 = at.x;
    if (drag.corner.includes("e")) x1 = at.x;
  } else {
    // Move: keep the size, clamp so the whole box stays on the page.
    const w = x1 - x0;
    const h = y1 - y0;
    x0 = Math.min(Math.max(x0 + dx, 0), 1 - w);
    y0 = Math.min(Math.max(y0 + dy, 0), 1 - h);
    x1 = x0 + w;
    y1 = y0 + h;
  }
  paint(normalize([x0, y0, x1, y1]));
}

async function onDragEnd() {
  window.removeEventListener("pointermove", onDragMove);
  const gesture = drag;
  drag = null;
  if (!gesture) return;

  gesture.box.classList.remove("dragging");
  const bbox = normalize(readPainted(gesture.box));
  const tooSmall = bbox[2] - bbox[0] < MIN_SIZE || bbox[3] - bbox[1] < MIN_SIZE;

  if (!gesture.moved || tooSmall) {
    // A click, not a drag. Nothing to save; re-render puts the DOM back,
    // including removing a zero-size box a stray click started drawing.
    render();
    return;
  }

  const page = gesture.page || gesture.field.page || 1;
  applyBoxUpdate(gesture.field.field_id, bbox, page);
  if (state.placingFieldId === gesture.field.field_id) setPlacing(null);

  try {
    await api.setSchema(state.sid, [
      { field_id: gesture.field.field_id, bbox, page },
    ]);
    onPlaced(gesture.field.field_id, null);
  } catch (err) {
    // The optimistic update above already moved it locally. Say so rather than
    // silently leaving the page and the server disagreeing about where a box is.
    onPlaced(gesture.field.field_id, err);
  }
}

function paint(bbox) {
  const [x0, y0, x1, y1] = bbox;
  drag.box.style.left = `${x0 * 100}%`;
  drag.box.style.top = `${y0 * 100}%`;
  drag.box.style.width = `${(x1 - x0) * 100}%`;
  drag.box.style.height = `${(y1 - y0) * 100}%`;
}

function readPainted(box) {
  const pct = (v) => parseFloat(v) / 100 || 0;
  const x0 = pct(box.style.left);
  const y0 = pct(box.style.top);
  return [x0, y0, x0 + pct(box.style.width), y0 + pct(box.style.height)];
}

/** Corners in order and inside the page — the same repair geometry.clamp makes
 * server-side, done here so the box never *looks* inverted mid-drag. */
function normalize([x0, y0, x1, y1]) {
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const xs = [clamp01(x0), clamp01(x1)].sort((a, b) => a - b);
  const ys = [clamp01(y0), clamp01(y1)].sort((a, b) => a - b);
  return [round(xs[0]), round(ys[0]), round(xs[1]), round(ys[1])];
}

const round = (v) => Math.round(v * 1e5) / 1e5;

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
  const tooWide = [];
  for (const box of container.querySelectorAll(".field-box")) {
    const ink = box.querySelector(".field-value > .ink");
    const width = box.clientWidth;
    // 0.94 accounts for .field-value's 3% inline padding on either side.
    const overflows = Boolean(ink) && width > 1 && ink.getBoundingClientRect().width > width * 0.94;
    box.classList.toggle("overflows", overflows);
    box.title = overflows
      ? `${box.dataset.label} — this value is wider than the field`.trim()
      : box.dataset.label;
    if (overflows) tooWide.push(box.dataset.fieldId);
  }
  // A title attribute is invisible on touch and to anyone not hovering, and
  // this is the failure that silently truncates the exported file. Hand the
  // list up so it can be said somewhere a person will actually read it.
  onOverflow(tooWide);
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}
