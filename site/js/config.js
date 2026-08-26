// Backend URLs. `SiteStack` deploys the whole site/ directory verbatim (no
// build step), so config.json is a plain static file shipped alongside the
// HTML/JS rather than something injected at build time. The pipeline's
// DeployBackend step (infra/lib/pipeline-stack.ts) writes the real
// ApiUrl/WebSocketUrl into this file — via S3 + a CloudFront invalidation —
// right after the backend deploys, so this is populated automatically and
// nobody should ever need to enter these by hand.

// Transcription is the one backend URL that is *not* discovered from this
// project's stack. `/stt` is a route on the separate `text-to-sql` REST API
// (function `stt-gemini`), so DeployBackend — which reads ApiUrl/WebSocketUrl
// out of our own CloudFormation outputs and rewrites config.json wholesale —
// has no way to learn it, and anything committed into config.json is
// overwritten on the next deploy anyway. Keeping the default here means voice
// input works on a fresh deploy and on a local server alike; a `sttUrl` key in
// config.json still wins, for pointing an environment somewhere else.
const DEFAULT_STT_URL = "https://edczm0lp1f.execute-api.eu-west-1.amazonaws.com/dev/stt";

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

/** Deliberately not part of `isConfigured` — voice is an optional extra, and a
 * missing transcription endpoint must never stop the page from loading. */
export function sttUrl(cfg) {
  return (cfg && cfg.sttUrl) || DEFAULT_STT_URL;
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
