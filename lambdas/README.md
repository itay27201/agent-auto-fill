# Form-filling agent — backend

Nineteen Lambdas behind a REST API, two WebSocket agents, and a Step Functions
ingest pipeline. Both agents run on Bedrock Sonnet 4.6.

## Layout

```
common/            shared across every function
  config.py        env-driven settings, the two model ids
  aws.py           lazy boto3 clients
  api.py           REST plumbing, CORS, error wrapping
  schema.py        FormField model + validation      <- the contract
  geometry.py      a page's ruled cells and text, read out of the PDF
  ocr.py           Textract, for pages with no geometry to read
  form_map.py      where each box is, as markdown   <- the layout contract
  store.py         DynamoDB single-table access
  catalog.py       the closed list of forms
  guide.py         the markdown guide: parse/render/slice
  agent_loop.py    Converse streaming loop, shared by both agents
  tools.py         filling agent's toolConfig + dispatcher
  author_tools.py  authoring agent's toolConfig + dispatcher
functions/
  api_create_session.py   POST   /sessions
  api_get_session.py      GET    /sessions/{id}
  api_set_fields.py       PATCH  /sessions/{id}/fields    values
  api_set_schema.py       PATCH  /sessions/{id}/schema    box geometry
  api_validate.py         POST   /sessions/{id}/validate
  api_render.py           POST   /sessions/{id}/render
  api_catalog_list.py     GET    /catalog
  api_catalog_get.py      GET    /catalog/{cid}
  api_catalog_create.py   POST   /catalog              promote a session
  api_catalog_update.py   PATCH  /catalog/{cid}        edit + publish
  api_catalog_source.py   POST   /catalog/{cid}/sources
  api_catalog_session.py  POST   /catalog/{cid}/sessions   <- the fast path
  api_catalog_reingest.py POST   /catalog/{cid}/reingest   <- rebuild its boxes
  ingest_classify.py      S3 trigger + step 1
  ingest_extract.py       step 2
  ingest_enrich.py        step 3 (the only Bedrock call in ingest)
  ingest_finalize.py      step 4 + failure handler
  agent_chat.py           WebSocket `message` — the filling agent
  author_chat.py          WebSocket `author`  — the authoring agent
statemachine/ingest.asl.json
template.yaml
tests/                    run without AWS
```

## Two agents

They share the streaming loop and nothing else.

| | `agent_chat` | `author_chat` |
|---|---|---|
| Helps someone | fill a document | define one |
| WS route | `message` | `author` |
| Writes | form values in DynamoDB | `catalog/<cid>/guide.md` in S3 |
| Tools | `set_field`, `validate`, `highlight_field`, … | `read_source`, `write_section`, `write_field_note` |
| Trust rule | every value needs a `source` + `evidence` | every sentence needs a `basis` + `citation` |

The toolConfig is chosen per turn, so neither can call the other's tools: the
authoring agent has no way to write a form value, and the filling agent has no
way to edit a guide. `read_guide` is the one name in both, read-only in each.

## Two model tiers

Defining a form is rare, expensive and human-reviewed. Filling one is frequent,
cheap and has to be accurate. The pipeline is priced accordingly:

| | Define a form | Fill a form |
|---|---|---|
| When | catalog authoring, or the first upload of an unseen document | every session, including every registry cache hit |
| Model | `IngestModelId` — Opus 4.8 | `BedrockModelId` — Sonnet 4.6 |
| Geometry | read from the PDF, plus a Textract pass | none, reads `schema.json` |
| Writes | `schema.json`, `form-map.md`, `guide.md` | field values |

`llm_json.invoke_json` defaults to the ingest tier because every caller of it is
on the define-once path. The chat agents go through `agent_loop` instead, and
`AgentChatFn`'s IAM names only the chat profile — so no chat turn can reach the
expensive model even by misconfiguration.

Two things about the ingest tier will fail at runtime if you get them wrong,
and both are handled in `llm_json._converse`:

- **`temperature` is gone.** Sampling parameters were removed on Opus 4.7+ and
  Sonnet 5; they return a 400. Sonnet 4.6 still accepts them, which is why the
  old single-tier code could always send `temperature: 0`. `config.accepts_sampling`
  is the one place that knows which is which.
- **Thinking is opt-in.** On Opus 4.8 an omitted `thinking` block means the
  model runs *without* thinking, which is most of what the tier was chosen for.
  It is asked for explicitly, adaptive only — `budget_tokens` is gone too. The
  extra fields ride in `additionalModelRequestFields`, and a Bedrock version
  that rejects them is retried once without: losing the thinking costs quality,
  failing the call costs the upload.

## The model id

Sonnet 4.6 **cannot** be invoked on demand by its base id. Bedrock rejects
`anthropic.claude-sonnet-4-6` with an on-demand-throughput error; it needs an
inference profile, which is the base id with a geography prefix:

| Profile | Routing |
|---|---|
| `eu.anthropic.claude-sonnet-4-6` | stays in the EU |
| `us.anthropic.claude-sonnet-4-6` | stays in the US |
| `apac.anthropic.claude-sonnet-4-6` | APAC |
| `global.anthropic.claude-sonnet-4-6` | anywhere, ~10% cheaper |

Government forms usually mean a geography-pinned profile, not global. Note
also that 4.6 dropped the `-v1` suffix that earlier models carried.

The IAM `Resource` must be the **inference-profile** ARN plus the
foundation-model ARNs it routes to — a policy naming only the base model
fails at runtime. Both are already in `template.yaml`.

## Deploy

```bash
sam build && sam deploy --guided \
  --parameter-overrides BedrockModelId=eu.anthropic.claude-sonnet-4-6
```

Enable model access in the Bedrock console first (Model catalog → Claude
Sonnet 4.6 → Request access).

## Flow

There are two ways in, and they meet at the same session.

**Picking a catalog form — no ingest at all**

1. `GET /catalog` → the published list.
2. `POST /catalog/{cid}/sessions` → copies the schema, seeds the fields,
   returns a session that is already `ready`. No upload, no state machine,
   no model call; a couple of hundred milliseconds.
3. Straight to step 4 below.

**Uploading your own**

1. `POST /sessions` → session id + presigned PUT URL. The browser uploads
   straight to S3; the document never passes through Lambda, which would cap
   it at 6MB.
2. S3 `ObjectCreated` → `on_upload` → state machine.
3. Classify → extract → enrich → finalize. Poll `GET /sessions/{id}` until
   `status` is `ready`. If the document's SHA-256 matches a published catalog
   form, classify short-circuits and the session picks up that form's guide.
   Extract also reads the page's ruled cells and text out of the PDF and
   numbers every place a person could write; enrich shows the model that
   numbered page and takes back a `region_id` per field, never a coordinate.

**Both, from here**

4. Frontend draws field boxes from `schema[].bbox` (normalized 0–1,
   top-left origin, so overlay works at any zoom). Fields whose box failed
   ingest's checks carry `bbox_confidence: "low"`, are drawn nowhere, and are
   flagged in the panel for someone to place.
5. User types → `PATCH .../fields`. User chats → WebSocket `message`.
   User drags a box → `PATCH .../schema`.
6. `POST .../render` → presigned download. A strict render refuses while a
   filled field still has no box.

**Defining a form** reuses the upload flow rather than adding a second
pipeline: upload the blank form, `POST /catalog` to promote the finished
session into a draft, write the guide over the `author` WebSocket route, then
`PATCH /catalog/{cid}` with `status: published`. This is the tier worth
spending on — check the form map and place any boxes ingest declined *before*
publishing, because everything fixed here is inherited by every session the
form ever has, and everything missed here is too.

### WebSocket protocol

One API, two routes. `RouteSelectionExpression` is `$request.body.action`.

**Filling** — `action: "message"` → `agent_chat`:
```json
{"action": "message", "session_id": "...", "message": "...",
 "scope_field_ids": ["national_id", "family_name"]}
```

`scope_field_ids` is what the user selected in the viewer. It shrinks the
prompt to one section and constrains where the agent may write.

Receive: `turn_start`, `text` (token deltas), `tool_start`, `field_updated`,
`highlight`, `turn_end`, `error`.

**Authoring** — `action: "author"` → `author_chat`:
```json
{"action": "author", "catalog_id": "form-106", "message": "...",
 "history": [{"role": "user", "text": "..."}]}
```

The transcript is client-side: defining a document is one sitting, not a
resumable session, and the durable artifact is `guide.md`, flushed to S3 after
every write. Only plain text turns from `history` are accepted — echoing
client-supplied tool blocks back into the prompt would let the page fabricate
tool results.

Receive: `turn_start`, `text`, `tool_start`, `guide_updated` (the whole
markdown, so the page re-renders live), `turn_end`, `error`.

## Design decisions worth knowing

**The agent never touches the document.** It edits form state; rendering is a
separate step. This is why the same agent works across PDF, DOCX, fillable
and flat.

**The geometry is reconstructed per table, not per page.** The obvious way to
rebuild a grid — one sorted list of every y on the page, one of every x, pair
adjacent values — is wrong on any page with more than one table, and a
government form is eight of them. Section א's columns get sliced by the children
table's columns three hundred points further down, and the true cell is never
even a candidate: on the 101 that reconstructed *one* cell for the whole of page
one. `geometry.cells` scopes the columns to the row band instead, so each table
reconstructs against its own grid. Same page, same rules, 60 cells.

Two supporting details, both load-bearing. Rule positions are *clustered* rather
than rounded, because two strokes 0.2pt apart are one border and rounding makes
them two grid lines with a hairline cell between. And an edge counts as covered
by the *union* of collinear rules, because a table border is routinely drawn one
segment per cell and demanding a single spanning rule rejects the row.

**A cell that contains its own label is still a writing area.** Plenty of forms
do not give labels a row of their own: `שם` is printed in the top corner of the
box you write the name into. Rejecting every cell with text in it loses sections
א, ב and ו of the 101 — every field anyone actually fills. `_under_label` keeps
those and takes the space beneath the label, and hands the label itself to the
model as `nearby_text`, where it is the best disambiguator there is. The
opposite layout — a header row of labels with empty cells under it — is settled
by the form itself: if it drew an empty box under the label, that is the box.

Measured on page one of the 101: 2 candidate regions before these two changes,
55 after.

**A model picks a box; it never invents one.** The flat path used to ask the
vision model to estimate four normalized floats per field from a page image.
That is the one thing vision models are worst at, and on the Israeli 101 form it
put the employer's name across the instructions paragraph and the deduction-file
number in the name cell — confidently, with nothing downstream checking.

A printed form is not a picture. It is a table of ruled cells, and the PDF says
exactly where every rule and glyph sits. `geometry.py` reads that out, numbers
every blank cell and underline, and `ingest_extract` draws those numbers onto a
copy of the page raster. The model is shown the numbered image and returns a
`region_id`; the coordinates come from the PDF. A wrong answer degrades from
"a box in the wrong place" to "the wrong box", which the checks below and the
viewer's box editor both catch.

Flat pages are also enriched one model call per page rather than one per
document. That retires the `ENRICH_MAX_TOKENS` truncation the single call kept
hitting, and a failed page now costs a page.

**An unplaced field is honest; a misplaced one is a lie.** Every box is run
through `geometry.sanity_check`: in range, non-degenerate, not on top of the
form's own printing, not on its own label, not overlapping another field. A box
that fails loses only its geometry — the field keeps its label, type, section
and help, and is marked `bbox_confidence: "low"`. The viewer draws it nowhere
and flags it in the panel, `api_render` refuses to stamp it, and a strict render
refuses to export while a filled field has nowhere to go. This is the same rule
`set_field` enforces for values: a blank box is always better than a wrong one
on an official form.

**Textract answers "where", the model answers "what".** Reconstructing cells
from ruled lines reaches about three quarters of the 101's first page and cannot
reach the rest — a writing area with no ruled box around it leaves nothing to
reconstruct, and a scan has no lines at all. `common/ocr.py` fills those gaps,
and it runs in exactly two situations: a page with no text layer, and while a
form is being *defined*. Never on an ordinary upload.

The part worth paying for is not the extra rectangles. Textract's
`KEY_VALUE_SET` says which printed label each box belongs to, and that link is
precisely what cell reconstruction throws away. `textract_label` carries it to
the model as a named candidate — *"this box is probably labelled שם"* — which it
confirms or overrides against the page image. Position stays Textract's to
decide and meaning stays the model's, so a wrong label costs a hint, never a box.

**A catalog entry can be rebuilt, but never behind your back.** The catalog path
copies the entry's own `schema.json` and never consults the registry, so
`SCHEMA_VERSION` cannot reach it — which is why a form defined by an older
pipeline kept serving its old boxes even after the pipeline improved.
`POST /catalog/{cid}/reingest` re-reads the entry's stored master through the
ordinary state machine and hands back a session; you look at the result and then
`PATCH /catalog/{cid}` with `adopt_schema_from_session`. Two steps on purpose:
an entry can hold boxes a person placed by hand and a guide a person reviewed,
and both survive the adoption. Discarding reviewed work to pick up a better
default is not a trade the system gets to make on its own.

**Anyone can move a box.** Before `api_set_schema` there was no path anywhere in
the system to fix a bbox — not the API, not the UI, not the authoring agent —
and a wrong one went into a registry with no TTL, so every later upload of that
form inherited it. It is a separate route from `PATCH .../fields` because the two
have different concurrency stories: values are per-item conditional writes shared
by two writers, geometry is one S3 object rewritten whole by the one person
looking at the page.

**`form-map.md` is the third artifact.** `schema.json` is the machine contract
and `guide.md` is what a PDF cannot contain; the map answers the question neither
can — *which box is this?* Form 101 labels at least three different fields `שם`,
so the agent's field list of ids, labels and sections could not tell them apart.
The map gives each field its row and side and calls out every repeated label by
name. It rides in the system prompt behind the cache point, so the layout costs
tokens once per session instead of being re-reasoned every turn, and it is
markdown for the same reason the guide is: a person has to be able to read it
and see that a box was misunderstood. It is generated in code from the schema,
never asked of a model, so it cannot drift from `schema.json`.

**Writes state which box they are going into.** `set_field` already demanded a
`source` and `evidence`, which establish that a value is *real*. Neither says
anything about whether it is going in the *right place*, and that is the failure
that actually happened: a correct, correctly-sourced value in the wrong cell.
`field_label` is checked against the schema and a mismatch is refused, with the
fields that do carry that label named in the error so the agent can correct
itself inside the same turn.

**The transcript is durable, so it is normalized on read as well as on
write.** A filling session stores its messages and replays them into Converse
every turn, which means one block Bedrock rejects — an empty text block, a
tool result whose call fell out of the window — kills not the turn that wrote
it but every turn after it, permanently. `agent_loop.sanitize` runs over the
history before each call, so a transcript damaged by an older bug keeps
working; nothing empty is ever persisted in the first place. An empty stream
is retried once and then reported, never papered over with invented text.

**Every write carries a source.** `user_said`, `profile`, or `source_doc` —
there is no `inferred`. Values written by the agent land unconfirmed and
`validate` blocks export until the user confirms them. On an official form
the model drafts, the human attests.

**Per-field DynamoDB items, not one blob.** Two writers share this state; a
single item would make the user's typing and the agent's writes clobber each
other. Conditional writes on `version` surface conflicts as 409.

**The event log feeds the next prompt.** Without it the agent re-asks for
values the user just typed in manually.

**The form registry is the real asset.** Schemas are cached by document
SHA-256. The second upload of the same form skips the vision pass entirely.
Registry entries have no TTL; session data does.

**...which is why a cache hit has to match on pipeline version too.** No TTL
means a schema built by an older, worse ingest is inherited forever. Deleting
the document and uploading it again does nothing: the cache is keyed by the
file's bytes, so identical bytes hit the identical entry — the whole point of
it — and ingest never re-runs. `config.SCHEMA_VERSION` closes that. Bump it when
a change alters what ingest *produces*, and every cached form re-ingests once,
by itself, the next time somebody uploads it. `POST /sessions` also takes
`force_reingest` for the different case where a current-version schema is simply
wrong for one document.

A **catalog** entry is stamped with the same version but never rebuilt
automatically. Its schema can carry boxes a person placed by hand and its guide
was reviewed; discarding that to pick up a better default is not a trade the
system gets to make. `cat.get` and the listing report `schema_stale` instead,
and re-promoting is a person's decision.

**The catalog is that registry made visible.** A government issues a fixed
list of forms, so the interesting case is not "an arbitrary document" but "the
twenty documents everyone files". A catalog entry is keyed by a stable slug
(`form-106`), not by the hash — an agency reissuing the same form with a new
revision date changes the bytes and would have orphaned a hash-keyed entry.
The hash stays on the entry as a secondary pointer so uploads still match.

**Catalog artifacts live under `catalog/` in ArtifactsBucket.** Not
DocsBucket, whose purge rule has no prefix filter and expires the whole bucket
after seven days. A catalog session's `doc_bucket` records which bucket its
master is in, which is why `api_render` reads
`sess.get("doc_bucket") or DOCS_BUCKET`.

**The guide is markdown, not JSON.** `schema.json` is a machine contract the
viewer and renderer depend on and stays JSON. The guide holds what a PDF
cannot contain — eligibility, attachments, deadlines, why a form gets
rejected — and an official whose job is knowing that form has to be able to
open it, fix a wrong deadline, and save.

**The authoring agent drafts; a person publishes.** Same rule `set_field`
enforces for values. `PATCH /catalog/{cid}` is the only path to `published`,
no tool can reach it, and publishing an empty guide is refused.

**Prompt caching on the schema block.** The field list is identical across
every turn in a session and is most of the prompt.

## Why WebSocket and not a streaming function URL

Lambda response streaming is native to the Node.js managed runtime only. In
Python it requires the Lambda Web Adapter layer in front of an ASGI app. A
WebSocket API is plain boto3, and it also lets the agent push UI events
mid-turn — `highlight_field` scrolls the viewer while the model is still
talking. Plain REST would also hit API Gateway's 29-second integration
timeout on any multi-tool turn.

## Tests

```bash
python3 tests/test_roundtrip.py     # build a fillable PDF, extract, fill, verify
python3 tests/test_tools.py         # filling tools: source + evidence enforcement
python3 tests/test_author_tools.py  # authoring tools: basis + citation enforcement
python3 tests/test_guide.py         # guide parse/render round-trip, prompt budget
python3 tests/test_agent_loop.py    # streaming loop: nothing invalid is sent or stored
python3 tests/test_guide_checks.py  # coverage counted in code, not asked of the model
python3 tests/test_note_batch.py    # chunking, retries, and what a batch missed
python3 tests/test_geometry.py      # boxes read from the PDF, and the ones refused
```

All eight run without AWS credentials.

## Not done yet

- **Auth.** The Cognito authorizer is commented out in `template.yaml`.
  Do not point this at real documents until it is on. This matters more now
  than it did: with `caller()` returning `"anonymous"` for everyone, **any
  user can edit or publish any catalog entry**, including one somebody else
  reviewed. The entry carries `created_by` and a `status` so an
  author-or-admin check is a policy change rather than a migration.
- **DOCX → PDF** needs LibreOffice, which is ~400MB — past the 250MB layer
  limit. `ExtractFn` has to become a container image function.
- **Textract's Hebrew accuracy is unmeasured.** Everything else in the geometry
  path has a number against it; this does not. Its key/value detection is
  markedly weaker in Hebrew than in English, so treat its contribution on a
  Hebrew form as unproven. The design limits the damage — its regions are
  unioned with the PDF's rather than replacing them, and `textract_label` is a
  hint the model may override — but if it turns out to add little here, the
  cell reconstruction is what carries the result.
- **Hebrew/RTL overlay** needs a TTF at `RTL_FONT_PATH` in the layer;
  reportlab's built-in fonts render Hebrew as black boxes. Irrelevant if
  your forms are AcroForm PDFs or English.
- **Profile lookup.** `source: "profile"` is accepted by the tool contract
  but nothing populates a saved profile yet.
- **Bedrock Guardrails** for PII are not attached.
