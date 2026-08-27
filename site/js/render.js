// One click: validate, render, download.
//
// The old Check -> Render -> Download rail asked a person to run a compliance
// check they had no reason to care about before they could ask for their
// document, then handed them a link that any state change silently retracted.
//
// The click goes straight to POST /render and never calls /validate. Strict
// rendering runs the same validate_all and refuses *before* it reads the
// document (api_render.py, above the get_object), so a refusal costs what the
// separate round trip cost and a success saves it.

import { state, onChange } from "./state.js";

const COPY = {
  working: "Preparing your document...",
  busy: "Preparing...",
  started: "Your download has started.",
  draftStarted: "Downloaded as a draft — the problems below are still in it.",
  dropped: (n) =>
    `${n} value${n === 1 ? "" : "s"} had nowhere to go on the page and ` +
    `${n === 1 ? "was" : "were"} left out.`,
  toFix: (n) => `${n} thing${n === 1 ? "" : "s"} to fix before this can be filed`,
  confirmFirst: (n) =>
    `${n} value${n === 1 ? "" : "s"} the assistant drafted still need${n === 1 ? "s" : ""} ` +
    `your confirmation — use the drafts bar at the top of this column.`,
  failed: "Could not produce the document.",
  expired: "That link expired. Download again.",
  primary: (docType) => `Download filled ${docType === "docx" ? "Word file" : "PDF"}`,
  noBox: "no box on the page — place it in the document first",
};

// Comfortably under the presigned URL's 900s (config.DOWNLOAD_URL_TTL), with
// room for a slow click.
const LINK_TTL_MS = 13 * 60 * 1000;

let container, api;
let busy = false;
let linkTimer = null;
// What the visible link's document was rendered from. Null when no link is up.
let renderedFrom = null;

export function initRender(el, apiClient) {
  container = el;
  api = apiClient;

  primaryBtn().textContent = COPY.primary(state.session?.doc_type);
  primaryBtn().addEventListener("click", () => download({ strict: true }));
  draftBtn().addEventListener("click", () => download({ strict: false }));

  // Only the AcroForm renderer reads `flatten`. The overlay path produces a
  // flat document either way and _fill_docx ignores it entirely, so on those
  // two the checkbox is a promise nothing keeps.
  if (state.session?.doc_type === "pdf_acroform") {
    container.querySelector("[data-role=flatten-label]").classList.remove("hidden");
  }

  onChange(onStateChange);
}

const primaryBtn = () => container.querySelector("[data-action=download]");
const draftBtn = () => container.querySelector("[data-action=download-draft]");
const fallbackLink = () => container.querySelector("[data-role=download]");
const problemList = () => container.querySelector(".awaiting-list");

// ------------------------------------------------------------------ the click

async function download({ strict }) {
  if (busy) return;
  busy = true;
  setBusy(true);
  // A strict attempt is re-asking the question, so the previous answer goes. A
  // draft attempt is acting on the answer already on screen, so it stays.
  if (strict) clearProblems();
  setSummary(COPY.working);

  try {
    const flatten = container.querySelector("[data-role=flatten]").checked;
    const result = await api.render(state.sid, { strict, flatten });
    deliver(result, strict);
  } catch (err) {
    if (err.status === 422 && strict) {
      showProblems(err.body || {});
    } else {
      clearProblems();
      setSummary(err.body?.message || err.message || COPY.failed, "error");
    }
  } finally {
    busy = false;
    setBusy(false);
  }
}

function deliver(result, strict) {
  const name = suggestedName(result.key);
  startDownload(result.download_url, name);
  showFallback(result.download_url, name);
  renderedFrom = fingerprint();

  if (strict) {
    clearProblems();
    setSummary(COPY.started, "ok");
    return;
  }
  // A draft render skipped every gate, including the one that stops a value
  // with no box from being dropped. Say what is missing from the file that just
  // landed in their downloads folder rather than "Ready."
  const dropped = (result.unplaced || []).length;
  setSummary(dropped ? `${COPY.draftStarted} ${COPY.dropped(dropped)}` : COPY.draftStarted, "warn");
}

// -------------------------------------------------------------- the download

/** Hand the file to the browser without leaving the page.
 *
 * `download` is inert here — the URL is cross-origin, so the attribute is
 * ignored and the filename is whatever api_render signed into the
 * Content-Disposition. It is set anyway for the day this is served same-origin.
 * `attachment` in that signature is also the only reason this is a download and
 * not a navigation away from the session into a PDF viewer.
 */
function startDownload(url, name) {
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** The visible half, and the only guaranteed half: a click() synthesized after
 * an await is a gesture the browser did not watch a person make, and Safari and
 * iOS suppress those. Shipping the programmatic trigger alone strands people
 * with a rendered document and no way to reach it. */
function showFallback(url, name) {
  const link = fallbackLink();
  link.href = url;
  link.setAttribute("download", name);
  link.classList.remove("hidden");
  clearTimeout(linkTimer);
  // A dead link that still looks live is worse than none: the failure arrives
  // as an S3 XML error page with no way back. Re-rendering is cheap and always
  // current, so the link is retired rather than refreshed.
  linkTimer = setTimeout(() => {
    hideFallback();
    setSummary(COPY.expired);
  }, LINK_TTL_MS);
}

function hideFallback() {
  clearTimeout(linkTimer);
  linkTimer = null;
  renderedFrom = null;
  const link = fallbackLink();
  link.classList.add("hidden");
  link.removeAttribute("href");
}

// -------------------------------------------------------------- the problems

function showProblems(body) {
  const list = problemList();
  list.innerHTML = "";

  // All three of api_render's 422s carry the whole validate_all result, so one
  // shape draws all of them. Read defensively anyway: SiteStack deploys ahead
  // of the SAM backend on a pipeline run, so for a minute this code talks to a
  // handler that still sends only `awaiting_confirmation`.
  const missing = body.missing_required || [];
  const errors = body.errors || [];
  const unplaced = body.unplaced || [];
  const awaiting = body.awaiting_confirmation || [];

  for (const p of missing) list.appendChild(issueRow(p.label || p.field_id, p.error || "required"));
  for (const p of errors) list.appendChild(issueRow(p.label || p.field_id, p.error || "invalid"));
  for (const p of unplaced) list.appendChild(issueRow(p.label || p.field_id, COPY.noBox));

  // Confirming drafts belongs to the drafts rail, which is already on screen
  // whenever there is one to confirm and does the whole batch in one PATCH. The
  // per-field Confirm buttons that used to live here were the same action
  // spelled differently, and they deleted themselves: confirming a row notified
  // state, and the clear below wiped the rows still being worked through.
  const fixable = missing.length + errors.length + unplaced.length;
  const parts = [];
  if (fixable) parts.push(COPY.toFix(fixable));
  if (awaiting.length) parts.push(COPY.confirmFirst(awaiting.length));
  setSummary(parts.join(" · ") || body.message || COPY.failed, "error");

  draftBtn().classList.remove("hidden");
}

function issueRow(label, message) {
  const row = document.createElement("div");
  row.className = "item issue-row";
  const text = document.createElement("span");
  text.setAttribute("dir", "auto");
  text.textContent = `${label}: ${message}`;
  row.appendChild(text);
  return row;
}

function clearProblems() {
  problemList().innerHTML = "";
  draftBtn().classList.add("hidden");
}

// ------------------------------------------------------------------ staleness

/** What survives a state change, and what does not.
 *
 * A server problem list stops describing the form the moment a value changes,
 * and a stale "nothing to fix" is how somebody files an incomplete form — so
 * that always goes, along with the draft escape hatch that only made sense
 * beside it.
 *
 * The finished download is a different thing. The old panel hid it on every
 * notify(), which includes selecting a box and switching a tab — neither of
 * which changes a single byte of the rendered document — and that is why the
 * link was usually gone before anyone reached it. It is retired only when the
 * document it points at is genuinely out of date.
 */
function onStateChange() {
  clearProblems();
  const stale = renderedFrom !== null && fingerprint() !== renderedFrom;
  if (stale) hideFallback();
  // The summary is either about the problems just cleared or about a download
  // that is still good. Only the first kind goes.
  if (stale || renderedFrom === null) setSummary("");
}

/** Everything the export would contain: values, whether each is confirmed, and
 * the boxes they get stamped into — a moved box changes the overlay renderer's
 * output as surely as a changed value does. Deliberately not all of `state`:
 * selection and placing-mode notify too and change nothing about the file. */
function fingerprint() {
  return state.fields
    .map((f) => {
      const v = state.values[f.field_id] || {};
      // Joined on separators no field id, value or bbox can contain:
      // anything they could would let two different forms agree.
      return [
        f.field_id,
        JSON.stringify(v.value ?? null),
        v.confirmed ? 1 : 0,
        (f.bbox || []).join(","),
      ].join("\u0000");
    })
    .join("\u0001");
}

// ------------------------------------------------------------------ chrome

function setBusy(on) {
  const btn = primaryBtn();
  btn.disabled = on;
  // textContent replaces text nodes only, so the ::before icon mask survives.
  btn.textContent = on ? COPY.busy : COPY.primary(state.session?.doc_type);
  draftBtn().disabled = on;
}

function setSummary(text, tone = "muted") {
  const el = container.querySelector(".summary");
  el.textContent = text;
  el.dataset.tone = tone;
}

/** Only the `download` attribute reads this, and a cross-origin URL ignores it.
 * The name that actually lands is the one api_render signs into the URL. */
function suggestedName(key) {
  const ext = (key || "").split(".").pop() || "pdf";
  const stem = (state.session?.filename || "form").replace(/\.[^.]+$/, "");
  return `${stem}-filled.${ext}`;
}
