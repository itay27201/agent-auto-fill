// Landing page: two ways in, one page.
//
// "Choose a form" leads because it is the better path whenever it applies —
// a government issues a fixed list of documents, and for anything on that
// list there is nothing to upload and nothing to wait for. "Upload your own"
// is the fallback for everything else.

import { waitForConfig, isConfigured, showUnavailableNotice } from "./config.js";
import { initCatalogList } from "./catalog-list.js";
import { initUpload } from "./upload.js";

const cfg = await waitForConfig();
if (!isConfigured(cfg)) {
  showUnavailableNotice(document.querySelector(".home-shell"));
} else {
  initCatalogList(document.getElementById("catalog-tab"), cfg);
  initUpload(
    {
      dropZone: document.getElementById("drop-zone"),
      fileInput: document.getElementById("file-input"),
      statusLine: document.getElementById("status-line"),
    },
    cfg
  );
}

// Tabs. The chosen tab is remembered so someone who came to upload does not
// get put back on the catalog every time they land here.
const tabs = Array.from(document.querySelectorAll("[data-tab]"));
const panels = new Map(
  Array.from(document.querySelectorAll("[data-panel]")).map((el) => [el.dataset.panel, el])
);

function selectTab(name) {
  for (const tab of tabs) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const [key, panel] of panels) panel.classList.toggle("hidden", key !== name);
  try {
    localStorage.setItem("home-tab", name);
  } catch {
    // Private browsing, or site data blocked. The tab still switches.
  }
}

for (const tab of tabs) tab.addEventListener("click", () => selectTab(tab.dataset.tab));

let initial = "catalog";
try {
  const saved = localStorage.getItem("home-tab");
  if (saved && panels.has(saved)) initial = saved;
} catch {
  // ignore
}
selectTab(initial);
