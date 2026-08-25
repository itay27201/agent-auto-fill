import { waitForConfig, isConfigured, showUnavailableNotice } from "./config.js";
import { createApi, uploadToS3 } from "./api.js";

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const statusLine = document.getElementById("status-line");

let cfg = await waitForConfig();
if (!isConfigured(cfg)) {
  showUnavailableNotice(document.querySelector(".upload-shell"));
}

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
    const api = createApi(cfg.apiUrl);
    const contentType = file.type || "application/octet-stream";
    const { session_id, upload_url } = await api.createSession(file.name, contentType);
    await uploadToS3(upload_url, file, contentType);
    setStatus("Uploaded. Processing the document...");
    window.location.href = `./session.html?sid=${encodeURIComponent(session_id)}`;
  } catch (err) {
    setStatus(err.message || "Upload failed", true);
  }
}

function setStatus(text, isError = false) {
  statusLine.textContent = text;
  statusLine.classList.toggle("error", isError);
}
