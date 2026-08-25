// Backend URLs. `SiteStack` deploys the whole site/ directory verbatim (no
// build step), so config.json is a plain static file shipped alongside the
// HTML/JS rather than something injected at deploy time. See lambdas/README
// and the SAM stack outputs (ApiUrl, WebSocketUrl) for the real values.
//
// Until config.json carries real values, callers fall back to whatever the
// person pasted into the one-time setup prompt (cached in localStorage) so
// the app is usable against a hand-deployed backend without waiting on the
// pipeline to wire this up automatically.

const STORAGE_KEY = "formAgent.config";

let cached = null;

export async function loadConfig() {
  if (cached) return cached;

  let fromFile = { apiUrl: "", wsUrl: "" };
  try {
    const res = await fetch("./config.json", { cache: "no-store" });
    if (res.ok) fromFile = await res.json();
  } catch {
    // config.json missing or unreachable — fall through to local overrides.
  }

  const local = readLocal();
  cached = {
    apiUrl: local.apiUrl || fromFile.apiUrl || "",
    wsUrl: local.wsUrl || fromFile.wsUrl || "",
  };
  return cached;
}

export function isConfigured(cfg) {
  return Boolean(cfg && cfg.apiUrl && cfg.wsUrl);
}

export function saveLocalConfig(apiUrl, wsUrl) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ apiUrl, wsUrl }));
  cached = { apiUrl, wsUrl };
}

function readLocal() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

/** Renders a one-time "paste your backend URLs" prompt into `container`
 * when the app has no usable config yet, and resolves once one is saved. */
export function promptForConfig(container) {
  return new Promise((resolve) => {
    const el = document.createElement("div");
    el.className = "setup-banner";
    el.innerHTML = `
      <strong>Backend not configured yet.</strong>
      <p>Paste the API and WebSocket URLs from the deployed backend stack's outputs
      (<code>ApiUrl</code> / <code>WebSocketUrl</code>). This is remembered on this device only.</p>
      <input type="text" placeholder="https://xxxx.execute-api.region.amazonaws.com/dev" data-role="api" />
      <input type="text" placeholder="wss://xxxx.execute-api.region.amazonaws.com/dev" data-role="ws" />
      <button class="primary" type="button">Save</button>
    `;
    container.prepend(el);
    el.querySelector("button").addEventListener("click", () => {
      const apiUrl = el.querySelector('[data-role="api"]').value.trim();
      const wsUrl = el.querySelector('[data-role="ws"]').value.trim();
      if (!apiUrl || !wsUrl) return;
      saveLocalConfig(apiUrl, wsUrl);
      el.remove();
      resolve({ apiUrl, wsUrl });
    });
  });
}
