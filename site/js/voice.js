// The microphone button: permission -> record -> encode -> transcribe.
//
// Deliberately knows nothing about the session, the WebSocket, or the shared
// store. It hands a finished transcript to its caller and that is the whole
// contract, which is what lets the authoring chat reuse it and what keeps the
// once-a-second recording timer from triggering a store notify (and with it a
// full re-render of the PDF overlay) on every tick.

import { blobToWav, bytesToBase64, audioContext } from "./audio-wav.js";
import { transcribe, SttError } from "./stt.js";

// API Gateway cuts the integration off at 29s and there is no retry behind it.
// A minute of speech is far more than anyone dictates into a form field, and
// leaves plenty of headroom for the round trip.
const MAX_MS = 60000;
const WARN_MS = 10000;
// Below this it is a stray double-click, not an utterance. Skipping the request
// saves a two-second round trip to be told "[Silence]".
const MIN_MS = 400;

// First supported wins. Whatever we get is decoded and re-encoded to WAV
// anyway, so this only decides which container the browser hands us.
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus", // Chrome, Edge, Firefox
  "audio/webm",
  "audio/mp4;codecs=mp4a.40.2", // Safari
  "audio/mp4",
  "audio/ogg;codecs=opus", // older Firefox
];

/** getUserMedia needs a secure context, so this is false on plain http from
 * anywhere but localhost. The button stays hidden in that case — an absent
 * control is more honest than one whose only possible outcome is an error. */
export function isVoiceSupported() {
  return Boolean(
    window.isSecureContext &&
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function" &&
      window.MediaRecorder
  );
}

export function createVoiceInput({ button, status, sttUrl, onTranscript, onNote, onRecordStart }) {
  if (!button || !isVoiceSupported()) return { supported: false };
  button.classList.remove("hidden");

  let phase = "idle";
  let stream = null;
  let recorder = null;
  let chunks = [];
  let startedAt = 0;
  let ticker = null;
  let autoStop = null;
  // getUserMedia cannot be aborted. If someone clicks again while the browser
  // prompt is up, this is how we know to drop the stream the instant it
  // arrives — otherwise walking away and granting permission later leaves a
  // live microphone with no UI attached to it.
  let cancelled = false;

  button.addEventListener("click", () => {
    if (phase === "idle") start();
    else if (phase === "permission") cancelPending();
    else if (phase === "recording") stop();
  });

  window.addEventListener("pagehide", release);

  return { supported: true, cancel: () => phase === "recording" && stop() };

  async function start() {
    cancelled = false;
    // Before the prompt, not after: any speech still playing would otherwise be
    // recorded off the speakers and sent to Gemini as if the person said it.
    if (onRecordStart) onRecordStart();
    setPhase("permission");

    let granted;
    try {
      granted = await navigator.mediaDevices.getUserMedia({
        // All non-`exact`, so a device that can't honour one ignores it rather
        // than throwing OverconstrainedError. sampleRate is deliberately absent
        // — it is poorly supported and we resample ourselves regardless.
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
    } catch (err) {
      setPhase("idle");
      if (!cancelled) onNote(micErrorText(err), "error");
      return;
    }

    if (cancelled) {
      stopTracks(granted);
      setPhase("idle");
      return;
    }

    stream = granted;
    chunks = [];
    try {
      recorder = makeRecorder(stream);
    } catch {
      release();
      setPhase("idle");
      onNote("Could not start recording on this browser.", "error");
      return;
    }

    recorder.ondataavailable = (e) => e.data && e.data.size && chunks.push(e.data);
    recorder.onstop = finish;

    // Waking the AudioContext here spends the click we already have; Safari
    // will not start one without a user gesture, and by decode time there is
    // no gesture left.
    const ctx = audioContext();
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});

    startedAt = Date.now();
    try {
      // Chunked, so the blob is assembled from ondataavailable rather than
      // depending on a single final chunk.
      recorder.start(1000);
    } catch {
      // Whatever went wrong, the microphone must not stay open behind a button
      // that has gone back to looking idle.
      release();
      setPhase("idle");
      onNote("Could not start recording.", "error");
      return;
    }
    setPhase("recording");
    ticker = setInterval(renderTimer, 250);
    autoStop = setTimeout(stop, MAX_MS);
    renderTimer();
  }

  function stop() {
    clearTimers();
    if (recorder && recorder.state !== "inactive") {
      recorder.stop(); // asynchronous — the blob is assembled in onstop
    } else {
      release();
      setPhase("idle");
    }
  }

  function cancelPending() {
    cancelled = true;
    setPhase("idle");
  }

  async function finish() {
    const elapsed = Date.now() - startedAt;
    const type = (recorder && recorder.mimeType) || "";
    const blob = new Blob(chunks, { type: type || "audio/webm" });
    chunks = [];
    // Release before transcribing, not after: leaving the OS microphone
    // indicator lit through a network round trip reads as still recording.
    release();

    if (elapsed < MIN_MS || !blob.size) {
      setPhase("idle");
      onNote("That was too short — try again and speak for a moment.");
      return;
    }

    setPhase("working");

    let payload;
    try {
      payload = await encode(blob, type);
    } catch {
      setPhase("idle");
      onNote("Could not read the recording.", "error");
      return;
    }

    try {
      const text = await transcribe(sttUrl, payload.base64, payload.mime);
      setPhase("idle");
      if (!text) {
        onNote("No speech detected — try again, a bit closer to the mic.");
        return;
      }
      onTranscript(text);
    } catch (err) {
      setPhase("idle");
      onNote(
        err instanceof SttError ? err.message : "Transcription failed. Try again.",
        "error"
      );
    }
  }

  function setPhase(next) {
    phase = next;
    button.dataset.state = next;
    button.disabled = next === "working";
    button.setAttribute("aria-pressed", String(next === "recording"));
    const label = LABELS[next];
    button.title = label;
    button.setAttribute("aria-label", label);
    // `recording` blanks it for the instant before renderTimer fills in the clock.
    if (status) status.textContent = STATUS[next];
  }

  function renderTimer() {
    if (!status || phase !== "recording") return;
    const elapsed = Date.now() - startedAt;
    const left = Math.max(0, MAX_MS - elapsed);
    const clock = `${Math.floor(elapsed / 60000)}:${String(
      Math.floor((elapsed % 60000) / 1000)
    ).padStart(2, "0")}`;
    // Warn before the auto-stop rather than cutting someone off mid-sentence.
    status.textContent =
      left <= WARN_MS ? `Recording ${clock} · ${Math.ceil(left / 1000)}s left` : `Recording ${clock}`;
  }

  function clearTimers() {
    clearInterval(ticker);
    clearTimeout(autoStop);
    ticker = null;
    autoStop = null;
  }

  function release() {
    clearTimers();
    stopTracks(stream);
    stream = null;
    recorder = null;
  }
}

async function encode(blob, recorderMime) {
  try {
    return { base64: await bytesToBase64(await blobToWav(blob)), mime: "audio/wav" };
  } catch {
    // Decoding failed, which in practice means Safari could not read back its
    // own fragmented mp4. Send the original bytes instead: that container is
    // AAC, which Gemini does accept. Worth five lines; not worth a second
    // encoder.
    const raw = new Uint8Array(await blob.arrayBuffer());
    return {
      base64: await bytesToBase64(raw),
      mime: (recorderMime || blob.type || "audio/webm").split(";")[0],
    };
  }
}

function makeRecorder(stream) {
  // isTypeSupported was missing from early Safari's MediaRecorder, and a build
  // that returns false for everything is a real state — the no-options
  // constructor is the honest fallback, and recorder.mimeType reports whatever
  // we actually got.
  const supported =
    typeof MediaRecorder.isTypeSupported === "function" &&
    MIME_CANDIDATES.find((t) => MediaRecorder.isTypeSupported(t));
  return supported ? new MediaRecorder(stream, { mimeType: supported }) : new MediaRecorder(stream);
}

function stopTracks(s) {
  if (s) s.getTracks().forEach((t) => t.stop());
}

function micErrorText(err) {
  switch (err && err.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "Microphone blocked. Allow microphone access for this site, then try again.";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "No microphone found.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone is in use by another app.";
    default:
      return "Could not start the microphone.";
  }
}

const LABELS = {
  idle: "Dictate a message",
  permission: "Cancel",
  recording: "Stop recording",
  working: "Transcribing",
};

const STATUS = {
  idle: "",
  permission: "Waiting for microphone access...",
  recording: "",
  working: "Transcribing...",
};
