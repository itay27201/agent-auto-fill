# UI review and change list — document filling assistant

Based on the current `site/` frontend: `index.html`, `session.html`, `catalog.html` and
the modules in `js/` (`home.js`, `catalog-list.js`, `upload.js`, `session.js`, `viewer.js`,
`fields-panel.js`, `chat.js`, `render.js`, `guide-panel.js`, `author.js`, `activity.js`,
`state.js`, `ws-client.js`, `md.js`, `api.js`, `config.js`).

---

## The diagnosis in one paragraph

The engineering underneath is ahead of the interface. The state machine is right (draft →
confirmed, optimistic writes with version checks, unplaced boxes, strict render), the agent
plumbing is right, and the two-agent split is right. What the UI does not yet do is **make
that machine legible to the two very different people using it**. A citizen filling a form is
currently shown box geometry, "flatten", "render" and a scrolling wall of 97 inputs; a
government author is given a three-step wizard that ends with `window.confirm()` and no way
back to anything they published. Almost every item below is about moving each capability to
the audience that should see it, and giving the draft/confirm promise a visual language.

Three changes carry most of the value:

1. **Make the agent's writing visible on the page** (§1.1) — right now the agent fills a field
   and nothing moves in the document. That moment *is* the product.
2. **Move box placement out of the citizen's session and into authoring** (§1.7, §3.3) — a
   person filing a form should never drag a bounding box.
3. **Hebrew-first, RTL-first UI** (§4) — the product is Israeli government forms, the chrome
   is `lang="en"`, LTR, in a font with no Hebrew coverage.

---

## 0. Visual direction

There is no design language yet, only accumulated class names (`.small`, `.ghost`, `.primary`,
`.chip`, `.chip-ok`, `.status-line`, `.hint-inline`, `.fine-print`, `.setup-banner`). Before
adding screens, fix the vocabulary. Proposed direction, grounded in what the product actually
is — paper forms, print, and handwriting:

**Concept: print vs. ink.** The scanned page is *print* (black, fixed, not yours). Everything
you or the agent add is *ink* on top of it. That maps one-to-one onto the state machine you
already have:

| State | Treatment | Meaning to the person |
|---|---|---|
| agent value, unconfirmed | **pencil** — graphite grey, slightly lighter weight | a suggestion, erasable |
| user value / confirmed | **ink** — ballpoint blue, full weight | this is your answer |
| problem (overflow, invalid, missing required) | **stamp red** underline/margin mark | fix before filing |
| validated & rendered | **stamp green** | filed-ready |

The moment a draft is confirmed, the value transitions from pencil grey to ink blue in place,
in the document. That single transition is the signature of the whole interface and it costs
one CSS transition on `.field-value .ink`. Use the same four colours in the fields panel rows,
the chat, and the check list so a person learns one mapping, not four.

**Palette** (starting point, tune later):

```
--paper        #F6F5F1   page surround, cool paper not cream
--print        #16181C   scanned print, primary text
--ink          #23459B   ballpoint blue — confirmed values, primary actions
--pencil       #7A8189   agent drafts, secondary text
--stamp        #B23A2F   errors, missing required
--seal         #2E6B4F   validated / published
```

**Typography.** `index.html`, `session.html` and `catalog.html` all load **Inter, which has no
Hebrew glyphs** — every Hebrew string in the UI is silently falling back to a system font, so
the interface is already mixing two typefaces at random. Replace with a deliberate pair that
covers both scripts:

- Display / headings: **Frank Ruhl Libre** (Hebrew serif, reads as official document without
  being stiff) — used sparingly, headings only.
- UI / body: **Assistant** or **Heebo** (both Hebrew + Latin, neutral, good at small sizes).
- Data / ids / field codes: one mono face (**IBM Plex Mono** has Hebrew-adjacent metrics that
  behave) for field ids, form numbers, dates.

Set an actual type scale (e.g. 12 / 14 / 16 / 20 / 28 / 40) and stop using `<strong>` and
`.small` as the scale.

---

## 1. The filling session — `session.html`

This is where the product lives. Ranked by value.

### 1.1 Show the agent writing into the document

**Now.** `chat.js` handles `field_updated` by calling `applyFieldUpdate`, which re-renders the
viewer. If the field is on page 4 and the person is looking at page 1, nothing visible happens.
`highlightField` exists but only fires on an explicit `highlight` event from the agent.

**Change.** On every `field_updated`:
- scroll the viewer to that box (reuse `highlightField`) — throttled, so a 40-field batch
  scrolls once per page rather than 40 times;
- animate the value in: the box outlines in pencil grey, the text fades in, the box settles;
- add a small floating counter over the viewer: `הסוכן מילא 12 שדות · 12 טיוטות`.

**Why.** The entire pitch — "the agent fills it for you in the right place" — currently happens
off-screen. Also: it is the only way a person can catch the agent putting the right value in
the wrong box.

### 1.2 Make confirming drafts cheap

**Now.** Confirmation exists in two disconnected places: a per-row banner in `fields-panel.js`
and a per-row button in the check results (`render.js:confirmRow`). One click per field. A
50-field agent batch is 50 clicks in two different lists.

**Change.**
- A persistent drafts bar at the top of the side panel: `12 ערכים ממתינים לאישור` with
  **Confirm all** and **Review one by one** (steps through them, scrolling the viewer to each).
- Confirm/undo directly on the box in the document (small ✓/✕ on hover / tap of a draft box).
- Undo on a confirmed value — right now confirming is one-way in the UI.
- Keyboard: `Enter` confirms the focused draft row, `↓`/`↑` move between fields.

### 1.3 Let the person hand the agent their documents

**Now.** There is no upload anywhere inside the session. The person can only type facts into
chat, one at a time.

**Change.** Add an attach control to the chat input (`session.html` `.chat-input`): ID card
photo, previous year's form, payslip, a PDF from the bank. The agent extracts and drafts from
it — into the same draft state, so nothing is riskier than it is today. On mobile, "take a
photo" as a first-class option.

**Why.** This is the difference between "the agent saves me typing" and "the agent fills my
form". Highest ROI feature request in this list, and it needs the least new UI — one button
plus a file chip row above the input.

### 1.4 Fix the side column — it is trying to be four panels at once

**Now.** `.side` stacks, top to bottom: tab strip → fields panel (can be 97 rows) → guide panel
→ validate bar → chat pane. On a real form the fields list pushes the chat into a sliver, and
the chat is the thing the product is named after.

**Change.** Make the right rail a real three-tab panel with chat as a peer, not a footer:

```
┌──────────────────────────────┬────────────────────────────┐
│  topbar: form name · progress · exit                      │
├──────────────────────────────┼────────────────────────────┤
│                              │ [ שיחה ] [ שדות ] [ הנחיות ]│
│                              ├────────────────────────────┤
│      document (scroll)       │                            │
│      boxes drawn on it       │   active panel             │
│                              │                            │
│                              ├────────────────────────────┤
│                              │ 12 טיוטות · אשר הכל        │
│                              ├────────────────────────────┤
│  ◀ page 2 of 5 ▶  [ערכים]    │ בדיקה · הפקת המסמך          │
└──────────────────────────────┴────────────────────────────┘
```

- Chat is the default tab (that is the pitch).
- The drafts bar and the action bar are fixed rails, always visible, never scrolled away.
- The fields tab gets a filter row: `הכל · חסר · טיוטות · בעיות` — on a 97-field form, "what's
  left" is the only view that matters.

### 1.5 Turn progress into progress

**Now.** `progressEl` in the topbar is used for four unrelated things: the field count,
`Placed {label}`, box-save failures, and refetch reasons via `flashProgress`. An error is
displayed in the slot labelled "progress".

**Change.** Split them:
- **Progress**: a real meter — `18 מתוך 34 שדות חובה` with a bar. Update it on every state
  change, computed locally (no server call).
- **Transient messages** ("box saved", "field refreshed elsewhere"): a toast, bottom-left,
  auto-dismiss.
- **Errors**: inline where the thing failed, in stamp red, with what to do next.

### 1.6 Continuous checking, not a "Check" button

**Now.** `render.js` requires an explicit click on **Check** to learn anything. Everything is
server-side, so the person's picture of the form is stale by default. (`onChange` intends to
clear a stale summary but `setSummary(null)` returns before clearing — see §7.)

**Change.** Compute required/empty/draft counts client-side from `state` on every change and
show them permanently. Keep the server call only as the pre-render gate, and rename it in the
person's language:

| Now | Suggested |
|---|---|
| Check | בדיקה לפני הפקה / *Check before filing* |
| Render | הפקת המסמך / *Create the final document* |
| flatten (checkbox) | נעילת הערכים כך שלא ניתן לערוך אותם |
| fields | שדות / *answers* |
| Place boxes | (remove from this screen — see §1.7) |

"flatten" and "render" are implementation words in front of a citizen.

### 1.7 Remove box placement from the citizen's session

**Now.** `viewer.js` gives every filler a "Place boxes (n)" mode with drag handles, and
`fields-panel.js:placeBanner` tells them *"This box has no place on the page… It will be left
out of the export."*

**Change.** Move the whole placing capability to the authoring app (§3.3). In the filling
session, an unplaced field becomes a quiet, honest, non-actionable notice: the answer is saved
but cannot be printed on this form, and the form's owner has been notified. Keep the drag UI
behind a `?edit=1` flag for your own debugging.

**Why.** Two audiences, one screen. A person filing a tax form has no idea what a bounding box
is, cannot be responsible for the schema being wrong, and the message as written is alarming
without being actionable.

### 1.8 The overflow warning is invisible

**Now.** `measureOverflow()` detects a value wider than its box and puts it in a `title`
attribute — invisible on touch, invisible to anyone not hovering.

**Change.** Draw it: stamp-red edge on the box, a marker in the margin, an entry in the "what's
left" list ("הערך ארוך מהמסגרת — יחתך בהדפסה"), and a suggested fix (shorten / abbreviate /
the agent offers a shorter form). This is the failure that silently ruins the exported file.

### 1.9 Chat panel details

- **Empty state.** The first thing in the log today is the system line "Connected." Replace with
  a short prompt plus 3–4 starter chips derived from the guide: `מלא מה שאפשר מהפרטים שלי`,
  `אילו מסמכים צריך לצרף?`, `מה זה שדה 4ב?`.
- **Markdown.** `chat.js` uses `textContent`; `md.js` already exists and is used by `author.js`.
  Agent answers about eligibility and attachments are lists — render them as lists.
- **Stop.** A running turn only disables the send button. Add **עצור** — a long batch with no
  way out feels like a hang.
- **Scope is undiscoverable.** Clicking a box selects it for chat, but nothing says so. Add a
  hint on first visit and a hover affordance on boxes ("לחץ כדי לדבר על השדה הזה").
- **Selection → chat** should be reversible from the chip and from the box, both.

### 1.10 The loading screen lies about catalog sessions

**Now.** `startFromCatalog` returns a session that is ready immediately, but `session.js` runs
the same `pollUntilReady` path and the person sees **"Reading your document"** with the ingest
copy for a form that needed no reading.

**Change.** If the session arrives `ready` on the first poll, skip the loading shell entirely
(no flash). For genuine uploads, replace the single status line with staged progress — העלאה →
קריאת העמודים → זיהוי השדות → מוכן — and show page thumbnails as they rasterize, so the wait
has something true in it. Five minutes of `Processing (working)...` is the current worst case.

### 1.11 Nobody can find their form again

**Now.** The session id exists only in the URL. Close the tab, lose the work.

**Change.** Keep a local list of recent sessions (`localStorage`) and surface it on the home
page: `טפסים שהתחלת` with form name, progress and last-touched date. Plus a "copy link" in the
topbar. Cheap to build, prevents the worst possible experience of the product.

### 1.12 A legend for the box states

Boxes can be `has-value`, `draft`, `selected`, `overflows`, `unplaced`, `hand-placed`,
`movable`. Nothing explains any of it. With the print/ink language from §0, add a small
collapsible legend under the viewer toolbar — four rows, not seven.

---

## 2. Home and catalog — `index.html`, `catalog-list.js`

### 2.1 Two audiences on one page

**"Define a document"** sits next to the citizen's search box. The authoring flow is an
internal tool (and `api.js` notes the Cognito authorizer is still commented out). Move it to
its own entry point — `/admin` — and out of the citizen's field of view. At minimum, demote it
to a footer link until auth exists.

### 2.2 Cards should tell people what they're in for

**Now.** A card shows name, agency, description, `n fields`, `has a guide`.

**Change.** `has a guide` is a system fact. Replace it with what the guide *contains*:
- **You'll need:** first two items from the guide's `Required attachments` section
- **~10 דקות** estimate derived from field count
- **מי רשאי להגיש** — one line from Eligibility

`n fields` is fine as a secondary chip; "97 fields" also usefully warns people.

### 2.3 The list will not scale

Client-side substring search over everything `listCatalog` returns works for 12 forms, not 400.
Add: grouping by agency, filter chips (`מס הכנסה · ביטוח לאומי · רשות האוכלוסין`), a "most
used" row at the top, and server-side search when the list grows.

### 2.4 Move the three promises out of the upload tab

`Assistant-drafted / Always a draft first / Export when ready` are true for both paths but sit
inside the upload panel, where most people never look. Put them under the hero, above the tabs.
"Nothing is final until you confirm it" is the product's core promise and deserves to be seen.

---

## 3. Authoring — `catalog.html`, `author.js`

### 3.1 There is no way back to anything

**Now.** Create an entry → write a guide → publish → leave. There is no list of entries, no
edit path, no unpublish, and no way to reach a draft you started yesterday. `listCatalog` even
takes `includeDrafts`, and nothing in the UI calls it.

**Change.** An entries screen as the authoring home: name, agency, status (draft / published),
field count, guide completeness, last edited. Each row opens the guide workbench. Add unpublish
and edit-after-publish (with a "published version is live" warning).

### 3.2 Make the wizard a real stepper

`Step 1 of 3` is a text label with no back navigation. You cannot rename a form after step 2.
Give it a clickable stepper, allow going back, validate step 2 inline (only `name` is enforced
today, silently), and autosave.

### 3.3 Add step 3½: check the boxes

**Now.** `reingestCatalogEntry` and `adoptSchemaFromSession` exist in `api.js` and **nothing in
the UI calls them.** A whole recovery capability has no interface. Meanwhile the citizen is the
one asked to fix bad geometry (§1.7).

**Change.** A verification step between "name it" and "write the guide":
- the document with all detected boxes drawn, exactly as the filler will see them;
- `n שדות לא מוקמו` with a jump list, and the drag/draw UI moved here;
- a **קרא מחדש את הטופס** button wired to `reingestCatalogEntry` → diff → `adoptSchemaFromSession`,
  showing what would change before adopting;
- an "עדין לא נבדק" badge on the entry until an author has walked it.

This is the single highest-value addition on the authoring side: it is where wrong geometry
should be caught, once, by the person who owns the form.

### 3.4 Sources are opaque

`renderSources` prints `s.source_id` as a chip. Show the filename, size, page count, and
whether the agent has read it yet. Add remove (needs an API route — see §7). Add drag-and-drop
onto the source area, and accept `.docx` alongside `.pdf/.txt/.md`.

### 3.5 Replace the publish gate

`window.confirm()` with a generated paragraph is the wrong instrument for the most consequential
click in the app. Make it a review sheet:
- what's missing, as a list with jump links (`3 sections empty`, `27 fields have no note`);
- a preview of exactly what a citizen will see;
- publish as the primary action, with a clear note that the guide becomes live.

### 3.6 Say that the conversation is not saved

`author.js` keeps `history` client-side by design; the guide is the durable artifact. That is a
reasonable decision the author cannot possibly know. One quiet line in the chat header: `השיחה
לא נשמרת — המדריך כן.`

---

## 4. Hebrew and RTL — treat as a blocker, not a polish item

The product exists for Hebrew government forms, and the interface is built LTR-first:

- All three pages are `<html lang="en">`. Set `lang="he" dir="rtl"` and mirror the layout: the
  document pane on the right, the panel on the left.
- `dir="auto"` is applied to individual inputs and chat bubbles, which handles text but not
  layout: the topbar, tab strips, step numbers, chips, drop zones, banners and the chat
  alignment are all still LTR.
- Convert directional CSS to logical properties (`margin-inline-start`, `padding-inline`,
  `inset-inline-start`, `text-align: start`). Do this once, before more screens are written.
- Inter has no Hebrew coverage — see §0.
- All UI copy is English while every form is Hebrew. Add a string table and a language toggle;
  Hebrew should be the default, English the option.
- Mixed-direction values (Hebrew name + Latin/digit id) inside a single box: `viewer.js`
  mirrors `api_render.py`'s RTL rule, which is right. Add `dir="auto"` and `unicode-bidi:
  plaintext` consistently to *every* value overlay and label so the preview matches the export.

---

## 5. Accessibility and input

- **Field boxes are `<div>`s with click handlers** — not focusable, no role, no name. Make them
  `<button>`s (or add `tabindex`/`role`/`aria-label`) so the document is navigable by keyboard.
- **The fields panel rebuilds the entire DOM on every state change** (`container.innerHTML = ""`),
  with `captureFocus`/`restoreFocus` compensating. This will break Hebrew IME composition and
  screen-reader position whenever the agent writes mid-typing. Move to keyed, surgical updates
  of the changed rows.
- **Agent writes need `aria-live="polite"`** — a blind user currently gets no notification that
  12 fields were filled.
- `select-toggle` has a `title` and no accessible name; `chat-send` is an empty button with an
  `aria-label` and no visible text — verify it renders an icon.
- Guarantee a visible focus ring everywhere (the drop zones and cards especially).
- Respect `prefers-reduced-motion` for the pulse/highlight animations added in §1.1.
- Touch targets: `.small` buttons and the box drag handles are well under 44px.

## 5.1 Mobile

Nothing in the markup suggests a mobile layout, and the side-by-side viewer + rail cannot work
at 380px. A large share of people filling government forms will do it on a phone. Minimum:

- document as the full-width surface, chat as a bottom sheet (swipe up to expand);
- "מה נשאר" as a pill that opens the fields list as a sheet;
- the action bar as a sticky footer;
- camera capture for both the form upload and the attachment flow in §1.3.

---

## 6. Component and token cleanup

Before building any of the above, extract a small system, or these screens will keep diverging:

- **Tokens**: the six colours in §0, a 4px spacing scale, two radii, three shadows, the type
  scale. One `tokens.css`.
- **Components** used by all three pages: `Button` (primary/secondary/ghost/danger, one size
  scale), `Chip`, `Banner` (info/warn/error/success), `EmptyState`, `Sheet`, `Toast`,
  `FieldRow`, `Stepper`. Today the same visual thing is spelled `.small`, `.small.ghost`,
  `.button-link`, `.primary` depending on the file.
- **One error convention**: inline (form-level) / toast (transient) / banner (fatal). Currently
  errors surface in `.status-line`, `activity.note(..., "error")`, and `progressEl` depending on
  which module caught them.
- **One copy voice**: active, sentence case, no system nouns. `render.js:setSummary` prints
  `12/34 filled · 2 error(s) · 1 to confirm` — a log line, not a sentence.

---

## 7. Things I noticed in the code while reviewing (not strictly UI)

1. **`render.js:setSummary(null)` does nothing.** `onChange` calls `setSummary(null)` intending
   to clear a stale validation result, but the function's first line is `if (text === null)
   return;` — so the stale summary stays on screen and reads as current. Either clear it or
   mark it stale explicitly.
2. **`progressEl` is overloaded** (§1.5) — errors are rendered in the progress slot.
3. **`flashProgress` can restore a stale string**: if two flashes overlap, the second captures
   the first's message as `prev`.
4. **No source deletion route.** `author.js` can add sources, never remove one; `api.js` has no
   DELETE for `/catalog/{cid}/sources`.
5. **`reingestCatalogEntry` / `adoptSchemaFromSession` are dead in the UI** (§3.3).
6. **`listCatalog({ includeDrafts })` is never called with drafts** (§3.1).
7. **`unplacedFields()` in `state.js` is exported and unused** — `viewer.js` and
   `fields-panel.js` each re-implement the `bbox_confidence === "low"` filter inline.
8. **No auth anywhere**, per `api.js`'s own comment. Until Cognito lands, the authoring UI
   should not be linked from the public home page (§2.1).
9. **`md.js` renders agent-written markdown into `innerHTML`.** The escaping looks careful, but
   this is untrusted text on the shortest path to the DOM — worth a CSP and a test suite of
   hostile guide strings before it goes near real users.

---

## 8. Suggested order

**Phase 1 — the filling experience (biggest visible gain)**
§0 tokens + type · §4 RTL/Hebrew · §1.1 show the agent writing · §1.2 bulk confirm ·
§1.5 real progress · §1.6 continuous checking + copy rename · §7.1 the stale-summary bug

**Phase 2 — separate the audiences**
§1.7 move placing out · §3.3 authoring verification step (wire up reingest/adopt) ·
§2.1 split the admin entry point · §1.10 stop lying about catalog loads

**Phase 3 — reach and retention**
§1.3 attach documents to chat · §5.1 mobile · §1.11 recent sessions · §2.2 richer cards

**Phase 4 — authoring maturity**
§3.1 entries list + edit/unpublish · §3.5 publish review sheet · §3.4 sources · §3.2 stepper

**Ongoing**
§5 accessibility · §6 component system · §1.12 legend · §1.9 chat polish