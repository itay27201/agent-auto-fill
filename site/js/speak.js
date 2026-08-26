// Reads the agent's reply aloud, using the browser's own speech synthesiser.
//
// The reply arrives as a stream of deltas, so this cuts it into sentences and
// queues each one as it completes rather than waiting for the turn to end.
// speechSynthesis maintains its own FIFO queue, so successive speak() calls
// play in order with no scheduling on our side, and the first sentence starts
// talking while the rest is still arriving.
//
// Everything stays on the device: no audio is uploaded, which on a form where
// people dictate ID numbers and income figures is worth something. The trade is
// voice quality against a hosted model — {feed, flush, cancel} is the seam to
// swap behind if that becomes the deciding factor, though a batch backend would
// cost the sentence-by-sentence behaviour.

const STORAGE_KEY = "afx.speak";
// Past this with no sentence ending in sight, cut at a comma or a space anyway.
// Otherwise an unpunctuated stretch sits silent until the turn ends.
const SOFT_LIMIT = 200;
const BOUNDARY = /[.!?׃…\n]/;
const HEBREW = /[֐-׿]/;
// Chrome stops speaking after roughly fifteen seconds unless nudged. Sentence
// chunking mostly keeps utterances under that; this covers the long ones.
const KEEPALIVE_MS = 10000;

export function isSpeechSupported() {
  return typeof window !== "undefined" && "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
}

export function createSpeaker({ button, onNote }) {
  if (!button || !isSpeechSupported()) return stub();

  const synth = window.speechSynthesis;
  let on = loadPreference();
  let pending = "";
  let voices = [];
  let keepalive = null;
  // Chrome silently drops speak() when the page has had no user interaction.
  // Clicking the toggle is that interaction, but the preference persists, so a
  // reload with it already on needs to wait for any gesture before the first
  // utterance.
  let primed = false;

  resolveVoices().then((list) => {
    voices = list;
    // No voices at all is a real state — desktop Linux especially. Leave the
    // button hidden rather than offer a control that cannot do anything.
    if (list.length) button.classList.remove("hidden");
  });

  const prime = () => {
    primed = true;
    window.removeEventListener("pointerdown", prime, true);
    window.removeEventListener("keydown", prime, true);
  };
  window.addEventListener("pointerdown", prime, true);
  window.addEventListener("keydown", prime, true);

  button.addEventListener("click", () => {
    on = !on;
    savePreference(on);
    render();
    if (!on) cancel();
  });

  window.addEventListener("pagehide", cancel);
  render();

  return { feed, flush, cancel, isOn: () => on };

  function feed(delta) {
    if (!on || !delta) return;
    pending += delta;

    for (;;) {
      const cut = nextCut(pending);
      if (cut < 0) break;
      const chunk = pending.slice(0, cut).trim();
      pending = pending.slice(cut);
      if (chunk) say(chunk);
    }
  }

  function flush() {
    const rest = pending.trim();
    pending = "";
    if (on && rest) say(rest);
  }

  function cancel() {
    pending = "";
    stopKeepalive();
    try {
      synth.cancel();
    } catch {
      // Nothing to do — cancelling an idle synthesiser is not a failure.
    }
  }

  function say(text) {
    if (!primed) return;
    let utterance;
    try {
      utterance = new SpeechSynthesisUtterance(text);
    } catch {
      return;
    }

    // Per utterance, not per session: the agent answers in Hebrew or English
    // depending on what it was asked, sometimes within one turn.
    const lang = HEBREW.test(text) ? "he-IL" : "en-US";
    utterance.lang = lang;
    const voice = pickVoice(voices, lang);
    if (voice) utterance.voice = voice;

    utterance.onerror = () => {
      // Turn it off rather than fail once per sentence for the rest of the turn.
      if (!on) return;
      on = false;
      savePreference(false);
      render();
      cancel();
      if (onNote) onNote("Could not read the reply aloud on this browser.", "warn");
    };
    utterance.onend = () => {
      if (!synth.speaking && !synth.pending) stopKeepalive();
    };

    try {
      synth.speak(utterance);
      startKeepalive();
    } catch {
      // Same as onerror, but for a synchronous throw.
    }
  }

  function startKeepalive() {
    if (keepalive) return;
    keepalive = setInterval(() => {
      if (!synth.speaking && !synth.pending) return stopKeepalive();
      // pause/resume is the long-standing workaround for Chrome's cutoff.
      synth.pause();
      synth.resume();
    }, KEEPALIVE_MS);
  }

  function stopKeepalive() {
    clearInterval(keepalive);
    keepalive = null;
  }

  function render() {
    button.setAttribute("aria-pressed", String(on));
    const label = on ? "Stop reading replies aloud" : "Read replies aloud";
    button.title = label;
    button.setAttribute("aria-label", label);
  }
}

/** Cut point just past the first sentence ending, or a fallback break once the
 * buffer has run long without one. Returns -1 when there is nothing to speak
 * yet. */
function nextCut(buffer) {
  const end = buffer.search(BOUNDARY);
  if (end >= 0) return end + 1;
  if (buffer.length < SOFT_LIMIT) return -1;
  const comma = buffer.lastIndexOf(",", SOFT_LIMIT);
  if (comma > 0) return comma + 1;
  const space = buffer.lastIndexOf(" ", SOFT_LIMIT);
  return space > 0 ? space + 1 : SOFT_LIMIT;
}

function pickVoice(voices, lang) {
  const prefix = lang.slice(0, 2);
  return (
    voices.find((v) => v.lang && v.lang.replace("_", "-") === lang) ||
    voices.find((v) => v.lang && v.lang.toLowerCase().startsWith(prefix)) ||
    null
  );
}

/** getVoices() is empty on Chrome until the list loads asynchronously, and
 * `voiceschanged` never fires at all on browsers where it is populated from the
 * start — so race the event against a timeout and take whatever we have. */
function resolveVoices() {
  const synth = window.speechSynthesis;
  const now = synth.getVoices();
  if (now && now.length) return Promise.resolve(now);

  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      synth.removeEventListener("voiceschanged", finish);
      resolve(synth.getVoices() || []);
    };
    synth.addEventListener("voiceschanged", finish);
    setTimeout(finish, 1500);
  });
}

function loadPreference() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false; // private mode, or storage disabled
  }
}

function savePreference(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
  } catch {
    // A preference that cannot be remembered still works for this session.
  }
}

function stub() {
  return { feed() {}, flush() {}, cancel() {}, isOn: () => false };
}
