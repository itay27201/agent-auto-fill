// Transcription against the `stt-gemini` Lambda.
//
// !!! DO NOT ADD REQUEST HEADERS TO THIS MODULE !!!
//
// The endpoint's OPTIONS method is misconfigured — a leftover MOCK
// requestTemplate sits on an AWS_PROXY integration — and returns HTTP 500.
// Browsers reject any preflight that isn't 2xx, so the *only* reason this works
// from a page is that the request never triggers one: a POST with no custom
// headers and a Content-Type of text/plain is a CORS "simple request", which
// goes straight out. The actual response does carry Access-Control-Allow-Origin,
// so the reply comes back fine.
//
// Passing `body` without a `headers` option makes fetch set
// `text/plain;charset=UTF-8` itself, which is exactly what we want. The Lambda
// runs json.loads on the body regardless of content type, so JSON travelling
// under a text/plain label parses normally.
//
// Add an Authorization header, an interceptor, a well-meaning
// `Content-Type: application/json`, or `credentials: "include"`, and the
// preflight fires, 500s, and the failure surfaces as an opaque browser CORS
// error with nothing in the server logs to explain it. The real fix is
// repairing that OPTIONS integration, but the endpoint belongs to another
// project's API.

// API Gateway gives up on the integration at 29s. Stopping a little after that
// turns a silent hang into an error we can actually explain.
const TIMEOUT_MS = 35000;

// Gemini narrates non-speech instead of returning nothing — a second of silence
// comes back as exactly "[Silence]". Only leading and trailing markers are
// stripped: a real transcription doesn't open or close with a bracket, and
// anything mid-sentence is likelier to be something the person actually said.
const MARKER = /^\s*\[[^\]\n]{0,40}\]\s*|\s*\[[^\]\n]{0,40}\]\s*$/;

export class SttError extends Error {
  constructor(message, kind) {
    super(message);
    this.name = "SttError";
    this.kind = kind;
  }
}

/** Returns the transcript, or "" when nothing intelligible was heard. */
export async function transcribe(url, audioBase64, mimeType) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      // No `headers` — see the note at the top of this file.
      body: JSON.stringify({
        action: "transcribe",
        audio: audioBase64,
        mime_type: mimeType,
      }),
      signal: controller.signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new SttError("Transcription timed out. Try a shorter recording.", "timeout");
    }
    throw new SttError(
      "Could not reach the transcription service. Check your connection and try again.",
      "network"
    );
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    throw new SttError(`Transcription failed (${res.status}).`, "http");
  }

  let body;
  try {
    body = await res.json();
  } catch {
    throw new SttError("The transcription service sent back something unreadable.", "parse");
  }

  if (!body || body.success !== true) {
    const detail = body && body.error ? `: ${String(body.error).slice(0, 140)}` : ".";
    throw new SttError(`Transcription failed${detail}`, "service");
  }

  return clean((body.data && body.data.transcribed_text) || "");
}

function clean(raw) {
  let out = String(raw);
  while (MARKER.test(out)) out = out.replace(MARKER, "");
  return out.trim();
}
