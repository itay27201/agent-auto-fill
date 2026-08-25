// Backend URLs. `SiteStack` deploys the whole site/ directory verbatim (no
// build step), so config.json is a plain static file shipped alongside the
// HTML/JS rather than something injected at build time. The pipeline's
// DeployBackend step (infra/lib/pipeline-stack.ts) writes the real
// ApiUrl/WebSocketUrl into this file — via S3 + a CloudFront invalidation —
// right after the backend deploys, so this is populated automatically and
// nobody should ever need to enter these by hand.

let cached = null;

export async function loadConfig() {
  if (cached) return cached;
  cached = await fetchConfig();
  return cached;
}

/** Retries a few times with backoff — covers the brief window right after a
 * fresh deploy before the CloudFront invalidation for config.json lands. */
export async function waitForConfig(maxAttempts = 4) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const cfg = await loadConfig();
    if (isConfigured(cfg)) return cfg;
    if (attempt < maxAttempts) {
      cached = null;
      await sleep(1000 * attempt);
    }
  }
  return cached;
}

export function isConfigured(cfg) {
  return Boolean(cfg && cfg.apiUrl && cfg.wsUrl);
}

async function fetchConfig() {
  try {
    const res = await fetch("./config.json", { cache: "no-store" });
    if (res.ok) return await res.json();
  } catch {
    // network hiccup — treated the same as "not ready yet" by the caller
  }
  return { apiUrl: "", wsUrl: "" };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Friendly, non-technical notice for the rare case config.json still isn't
 * ready after retrying — no URLs, no form, nothing for an end user to act on. */
export function showUnavailableNotice(container) {
  const el = document.createElement("div");
  el.className = "setup-banner";
  el.innerHTML = `<strong>Just a moment.</strong><p>The service is finishing setup — try refreshing in a minute.</p>`;
  container.prepend(el);
}
