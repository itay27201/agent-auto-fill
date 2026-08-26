// Validate -> confirm remaining drafts -> render -> download. Strict
// rendering (api_render.py's default) refuses to export while errors,
// missing required fields, or unconfirmed agent drafts remain, so this
// panel's job is mostly making that list visible and actionable.

import { state, applyFieldUpdate, onChange } from "./state.js";

let container, api;

export function initRender(el, apiClient) {
  container = el;
  api = apiClient;
  container.querySelector("[data-action=validate]").addEventListener("click", runValidate);
  container.querySelector("[data-action=render]").addEventListener("click", runRender);
  onChange(() => {
    // Field edits can resolve/introduce validation issues; don't force a
    // manual re-check, but do drop a stale result rather than show it as current.
    clearSummary();
  });
}

async function runValidate() {
  setSummary("Checking...");
  try {
    const result = await api.validate(state.sid);
    renderResult(result);
  } catch (err) {
    setSummary(err.message || "Validation failed", true);
  }
}

function renderResult(result) {
  const list = container.querySelector(".awaiting-list");
  list.innerHTML = "";

  const problems = [...(result.missing_required || []), ...(result.errors || [])];
  for (const p of problems) {
    list.appendChild(issueRow(p.label || p.field_id, p.error || "required"));
  }
  for (const p of result.awaiting_confirmation || []) {
    list.appendChild(confirmRow(p.field_id, p.label));
  }

  const parts = [`${result.filled}/${result.total} filled`];
  if (result.errors?.length) parts.push(`${result.errors.length} error(s)`);
  if (result.missing_required?.length) parts.push(`${result.missing_required.length} missing`);
  if (result.awaiting_confirmation?.length) parts.push(`${result.awaiting_confirmation.length} to confirm`);
  setSummary(parts.join(" · "), !result.ok);

  container.querySelector("[data-action=render]").disabled = !result.ok || Boolean(result.awaiting_confirmation?.length);
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

function confirmRow(fieldId, label) {
  const row = document.createElement("div");
  row.className = "item confirm-row";
  const text = document.createElement("span");
  text.setAttribute("dir", "auto");
  text.textContent = label || fieldId;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "small";
  btn.textContent = "Confirm";
  btn.addEventListener("click", async () => {
    const version = (state.values[fieldId] || {}).version;
    try {
      const res = await api.setFields(state.sid, [{ field_id: fieldId, confirm: true, expected_version: version }]);
      const result = res.results?.[0];
      if (result?.ok) {
        applyFieldUpdate(fieldId, { confirmed: true, version: result.version });
        row.remove();
      }
    } catch {
      /* leave the row in place for retry */
    }
  });
  row.append(text, btn);
  return row;
}

async function runRender() {
  const btn = container.querySelector("[data-action=render]");
  btn.disabled = true;
  setSummary("Rendering...");
  try {
    const flatten = container.querySelector("[data-role=flatten]").checked;
    const result = await api.render(state.sid, { strict: true, flatten });
    setSummary("Ready.");
    const link = container.querySelector("[data-role=download]");
    link.href = result.download_url;
    link.classList.remove("hidden");
  } catch (err) {
    setSummary(err.body?.message || err.message || "Render failed", true);
  } finally {
    btn.disabled = false;
  }
}

function setSummary(text, isError = false) {
  const el = container.querySelector(".summary");
  el.textContent = text;
  el.style.color = isError ? "var(--danger)" : "var(--muted)";
}

/** Drop a result that no longer describes the form. A check is a snapshot taken
 * server-side; the moment a value changes it stops being true, and leaving it up
 * reads as current — which is worse than showing nothing, because "0 errors"
 * about the previous state is how someone files an incomplete form. The issue
 * list goes with it for the same reason. */
function clearSummary() {
  const btn = container.querySelector("[data-action=render]");
  if (btn) btn.disabled = true;
  container.querySelector(".awaiting-list").innerHTML = "";
  const link = container.querySelector("[data-role=download]");
  if (link) link.classList.add("hidden");
  setSummary("");
}
