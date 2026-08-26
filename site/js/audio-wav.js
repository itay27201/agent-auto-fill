// Recorded audio -> 16 kHz mono 16-bit WAV, base64.
//
// MediaRecorder hands back whatever the browser feels like: Chrome and Firefox
// produce webm/opus, Safari produces fragmented mp4. Neither is a format Gemini
// documents support for inline audio — its list is wav, mp3, aiff, aac, ogg,
// flac — so rather than gamble on an undocumented container we decode with the
// browser's own codecs and re-encode to WAV, which is verified working against
// the endpoint. The happy side effect is that the Chrome/Safari difference stops
// existing before anything leaves the page.
//
// 16 kHz mono is well above what speech needs and keeps the payload small:
// 32 KB/s, so a minute of audio is ~1.9 MB of WAV and ~2.6 MB of base64, far
// inside API Gateway's 10 MB cap.

const TARGET_RATE = 16000;

let ctx = null;

/** One context for the life of the page. Chrome hard-caps live AudioContexts at
 * around six, so creating one per recording bricks the button partway through a
 * session. Callers must invoke this from inside a click — Safari refuses to
 * start a context without a user gesture. */
export function audioContext() {
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  if (!ctx) ctx = new Ctor();
  return ctx;
}

/** Decoded PCM -> WAV bytes at 16 kHz mono. Throws if the blob is not audio the
 * browser can decode, which is the caller's cue to fall back to the raw blob. */
export async function blobToWav(blob) {
  const context = audioContext();
  if (!context) throw new Error("No AudioContext");

  // decodeAudioData detaches the ArrayBuffer it is given, so this must be a
  // fresh read of the blob and the caller must keep the blob itself around.
  const decoded = await decode(context, await blob.arrayBuffer());
  const rendered = await toMono16k(decoded);
  return encodeWav(rendered);
}

/** Base64 without the size ceiling. String.fromCharCode.apply blows the
 * argument limit and throws RangeError somewhere past a few hundred KB, which
 * is well inside the range of a normal recording. */
export function bytesToBase64(bytes) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("Could not encode audio"));
    reader.onload = () => {
      const url = String(reader.result);
      resolve(url.slice(url.indexOf(",") + 1));
    };
    reader.readAsDataURL(new Blob([bytes]));
  });
}

/** Safari resolved decodeAudioData through callbacks long before it returned a
 * promise, and still accepts both. Supporting the pair is two lines and removes
 * a whole class of works-everywhere-but-here bug. */
function decode(context, arrayBuffer) {
  return new Promise((resolve, reject) => {
    const maybePromise = context.decodeAudioData(arrayBuffer, resolve, reject);
    if (maybePromise && typeof maybePromise.then === "function") {
      maybePromise.then(resolve, reject);
    }
  });
}

/** Resample and downmix in one pass. A 1-channel destination downmixes stereo
 * for us through the default channelCountMode, so there is no averaging to do
 * by hand. */
async function toMono16k(buffer) {
  const Ctor = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!Ctor) throw new Error("No OfflineAudioContext");

  const frames = Math.max(1, Math.ceil(buffer.duration * TARGET_RATE));
  // The positional constructor, not the options object — older WebKit only has
  // this form.
  const offline = new Ctor(1, frames, TARGET_RATE);
  const source = offline.createBufferSource();
  source.buffer = buffer;
  source.connect(offline.destination);
  source.start(0);
  return offline.startRendering();
}

function encodeWav(buffer) {
  const samples = buffer.getChannelData(0);
  const bytes = new Uint8Array(44 + samples.length * 2);
  const view = new DataView(bytes.buffer);

  const ascii = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };

  ascii(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true); // PCM header length
  view.setUint16(20, 1, true); // format: uncompressed PCM
  view.setUint16(22, 1, true); // channels
  view.setUint32(24, TARGET_RATE, true);
  view.setUint32(28, TARGET_RATE * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    // Clamp before scaling: rendering can overshoot ±1 slightly, and letting
    // that wrap turns a loud syllable into a burst of noise.
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }

  return bytes;
}
