// The form picker: the closed list of documents the agency issues.
//
// Clicking a card is the whole point of the catalog. There is no upload and no
// ingest — the backend copies a schema that already exists and the session is
// ready before the next page finishes loading.

import { createApi } from "./api.js";
import { escapeHtml } from "./md.js";

export function initCatalogList(root, cfg) {
  const api = createApi(cfg.apiUrl);
  const search = root.querySelector("[data-role=search]");
  const grid = root.querySelector("[data-role=grid]");
  const status = root.querySelector("[data-role=status]");

  let entries = [];
  let starting = null;

  search.addEventListener("input", render);
  load();

  async function load() {
    status.textContent = "Loading the list of forms...";
    try {
      const { entries: found } = await api.listCatalog();
      entries = found || [];
      status.textContent = "";
      render();
    } catch (err) {
      status.textContent = err.message || "Could not load the catalog.";
      status.classList.add("error");
    }
  }

  function matching() {
    const q = search.value.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) =>
      [e.name, e.agency, e.description, e.catalog_id]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(q))
    );
  }

  function render() {
    const rows = matching();
    grid.innerHTML = "";

    if (!entries.length) {
      grid.appendChild(emptyState(
        "No forms have been added yet",
        "Once someone defines a document, it shows up here and anyone can fill it in without uploading anything.",
        true
      ));
      return;
    }
    if (!rows.length) {
      grid.appendChild(emptyState("No form matches that",
        "Try a different word, or upload the document yourself in the other tab.", false));
      return;
    }

    for (const entry of rows) grid.appendChild(card(entry));
  }

  function card(entry) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "form-card";
    el.disabled = Boolean(starting);
    el.setAttribute("dir", "auto");
    el.innerHTML = `
      <span class="form-card-head">
        <span class="form-card-name">${escapeHtml(entry.name || entry.catalog_id)}</span>
        ${entry.agency ? `<span class="form-card-agency">${escapeHtml(entry.agency)}</span>` : ""}
      </span>
      ${entry.description ? `<span class="form-card-desc">${escapeHtml(entry.description)}</span>` : ""}
      <span class="form-card-meta">
        <span class="chip">${Number(entry.field_count) || 0} fields</span>
        ${entry.has_guide ? '<span class="chip chip-ok">has a guide</span>' : ""}
        <span class="form-card-go">Fill this in &rarr;</span>
      </span>`;
    el.addEventListener("click", () => start(entry, el));
    return el;
  }

  async function start(entry, el) {
    if (starting) return;
    starting = entry.catalog_id;
    el.classList.add("busy");
    status.classList.remove("error");
    status.textContent = `Opening ${entry.name || entry.catalog_id}...`;
    try {
      const { session_id } = await api.startFromCatalog(entry.catalog_id);
      window.location.href = `./session.html?sid=${encodeURIComponent(session_id)}`;
    } catch (err) {
      starting = null;
      el.classList.remove("busy");
      status.textContent = err.message || "Could not open that form.";
      status.classList.add("error");
    }
  }

  function emptyState(title, body, offerDefine) {
    const el = document.createElement("div");
    el.className = "empty-state";
    el.innerHTML = `
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(body)}</p>
      ${offerDefine ? '<a class="link-emphasis" href="./catalog.html">Define a document &rarr;</a>' : ""}`;
    return el;
  }
}
