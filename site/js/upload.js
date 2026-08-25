// The "upload your own" path: presigned PUT straight to S3, then poll the
// session while the ingest pipeline classifies and reads the document.
//
// Mounted by home.js into a tab rather than owning the page, because the
// landing page now leads with the catalog — most people are filling a form the
// government already issues, and for those this whole path is a slower way to
// reach the same place.

import { createApi, uploadToS3 } from "./api.js";

/**
 * @param els.dropZone / els.fileInput / els.statusLine
 * @param opts.onSession  where to go once the upload lands. Defaults to the
 *        filling session; catalog.html passes its own, because defining a
 *        document continues on that page instead.
 */
export function initUpload(els, cfg, opts = {}) {
  const { dropZone, fileInput, statusLine } = els;
  const api = createApi(cfg.apiUrl);
  const accept = opts.accept || ".pdf,.docx";
  if (fileInput) fileInput.accept = accept;

  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag");
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (file) handleFile(file);
  });

  async function handleFile(file) {
    setStatus(`Uploading ${file.name}...`);
    try {
      const contentType = file.type || "application/octet-stream";
      const { session_id, upload_url } = await api.createSession(file.name, contentType);
      await uploadToS3(upload_url, file, contentType);
      setStatus("Uploaded. Processing the document...");
      if (opts.onSession) {
        opts.onSession(session_id, file);
      } else {
        window.location.href = `./session.html?sid=${encodeURIComponent(session_id)}`;
      }
    } catch (err) {
      setStatus(err.message || "Upload failed", true);
    }
  }

  function setStatus(text, isError = false) {
    if (!statusLine) return;
    statusLine.textContent = text;
    statusLine.classList.toggle("error", isError);
  }

  return { setStatus };
}
