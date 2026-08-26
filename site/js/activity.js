// The activity log: the muted rows under the conversation that say what the
// agent is doing right now.
//
// Both agents batch. Asked to write notes for a 97-field form, the authoring
// agent fires `write_field_note` fifty times inside one Converse turn; the
// filling agent does the same with `set_field`. One row per call buried the
// conversation under fifty identical lines, so consecutive calls to the *same*
// tool collapse into a single row that counts up in place. A different tool
// name closes that row and opens a new one, which is what keeps the sequence
// of work readable — "wrote 6 sections", then "looking at the form's fields",
// then "wrote 41 field notes".
//
// The count is of *finished* calls (`tool_end`), not started ones. A row that
// claimed 50 while the model was still mid-batch would be a progress bar that
// runs ahead of the work.

const ICON_RUNNING = "running";
const ICON_DONE = "done";
const ICON_FAILED = "failed";

// `one` is what a single call reads as; `many` takes over from the second.
// Phrasing them separately is why the counter reads as a sentence rather than
// as a label with a number stapled on.
const TOOL_LABELS = {
  // authoring agent — see lambdas/common/author_tools.py
  list_sources: { one: "checking what you uploaded", many: (n) => `checked what you uploaded (${n}×)` },
  read_source: { one: "reading a reference document", many: (n) => `read ${n} reference documents` },
  get_field_list: { one: "looking at the form's fields", many: (n) => `looked at the form's fields (${n}×)` },
  read_guide: { one: "re-reading the guide", many: (n) => `re-read the guide (${n}×)` },
  write_section: { one: "writing a section", many: (n) => `wrote ${n} sections` },
  write_field_note: { one: "writing a note on a field", many: (n) => `wrote ${n} field notes` },
  // The batch tool. One call, so its row moves on note_progress rather than on
  // tool_end — `many` is what the running count reads through.
  write_field_notes: { one: "writing the field notes", many: (n) => `wrote ${n}` },

  // filling agent — see lambdas/common/tools.py
  get_schema: { one: "reading the form", many: (n) => `read the form (${n}×)` },
  list_unfilled: { one: "checking what is left", many: (n) => `checked what is left (${n}×)` },
  set_field: { one: "filling a field", many: (n) => `filled ${n} fields` },
  set_fields: { one: "filling fields", many: (n) => `filled ${n} groups of fields` },
  clear_field: { one: "clearing a field", many: (n) => `cleared ${n} fields` },
  validate: { one: "checking the form", many: (n) => `checked the form (${n}×)` },
  explain_field: { one: "looking up what a field wants", many: (n) => `looked up ${n} fields` },
  highlight_field: { one: "pointing at a field", many: (n) => `pointed at ${n} fields` },
};

/** An activity log bound to a chat log element.
 *
 * `tool`/`toolDone` drive the collapsing rows; `note` is for the one-off
 * system lines ("Connected.", "Added booklet.pdf.") that never collapse.
 */
export function createActivityLog(logEl) {
  // The row currently accepting counts, or null between runs.
  let open = null;

  function scroll() {
    logEl.scrollTop = logEl.scrollHeight;
  }

  function render() {
    if (!open) return;
    const label = TOOL_LABELS[open.name];
    // Until the first call finishes there is nothing to count, so a fresh row
    // shows the singular "doing it now" phrasing rather than "0".
    //
    // Counts successes, not attempts. The plural labels are success verbs, so
    // adding the failures in made a row of nothing but rejected writes read
    // "filled 2 fields · 2 failed" — claiming two fills that never happened,
    // which is exactly what an unfillable field looked like from the outside.
    const n = open.done;
    let text;
    if (open.total) {
      // A tool reporting its own progress knows the denominator, which is the
      // number that matters: "wrote 41 field notes" is not the same claim as
      // "wrote 41 of 97".
      const many = label ? label.many(open.at) : `${open.name}: ${open.at}`;
      text = `${many} of ${open.total}`;
    } else if (!label) {
      text = n > 1 ? `used ${open.name} ${n}×` : `using ${open.name}...`;
    } else if (open.failed) {
      // With failures on the row the count is worth showing even at zero:
      // "filled 0 fields · 2 failed" is the honest line, where the singular
      // "filling a field" would read as still in progress.
      text = label.many(n);
    } else {
      text = n > 1 ? label.many(n) : label.one;
    }
    if (open.failed) text += ` · ${open.failed} failed`;
    open.textNode.textContent = text;
  }

  function settle() {
    if (!open) return;
    open.node.classList.remove(ICON_RUNNING);
    open.node.classList.add(open.failed ? ICON_FAILED : ICON_DONE);
    open = null;
  }

  /** A tool call started. Same tool as the open row → keep counting in it. */
  function tool(name) {
    if (open && open.name === name) {
      open.started += 1;
      render();
      return;
    }
    settle();

    const node = document.createElement("div");
    node.className = `chat-msg tool activity ${ICON_RUNNING}`;
    const icon = document.createElement("span");
    icon.className = "activity-icon";
    const textNode = document.createElement("span");
    textNode.className = "activity-text";
    node.append(icon, textNode);

    open = { name, node, textNode, started: 1, done: 0, failed: 0, at: 0, total: 0 };
    render();
    logEl.appendChild(node);
    scroll();
  }

  /** A tool call finished. Counts against the open row if it is the same tool
   * — a late `tool_end` arriving after the row moved on is dropped rather than
   * credited to the wrong row. */
  function toolDone(name, ok = true) {
    if (!open || open.name !== name) return;
    if (ok) open.done += 1;
    else open.failed += 1;
    render();
    scroll();
  }

  /** A tool reporting its own progress mid-call. One `write_field_notes` call
   * covers a hundred fields, so `tool_end` fires once and would otherwise sit
   * silent for a minute; these frames are what make the row move. */
  function progress(name, done, total) {
    if (!open || open.name !== name) tool(name);
    open.at = done;
    open.total = total;
    render();
    scroll();
  }

  /** A plain line that never collapses: connection notices, errors, hints. */
  function note(text, kind = "info") {
    settle();
    const node = document.createElement("div");
    node.className = kind === "error" ? "chat-msg system-error" : "chat-msg tool";
    node.textContent = text;
    logEl.appendChild(node);
    scroll();
  }

  return { tool, toolDone, progress, settle, note };
}
