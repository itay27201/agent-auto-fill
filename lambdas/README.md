# Form-filling agent — backend

Nineteen Lambdas behind a REST API, two WebSocket agents, and a Step Functions
ingest pipeline. Both agents run on Bedrock Sonnet 4.6.

## Layout

```
common/            shared across every function
  config.py        env-driven settings, model id
  aws.py           lazy boto3 clients
  api.py           REST plumbing, CORS, error wrapping
  schema.py        FormField model + validation      <- the contract
  store.py         DynamoDB single-table access
  catalog.py       the closed list of forms
  guide.py         the markdown guide: parse/render/slice
  agent_loop.py    Converse streaming loop, shared by both agents
  tools.py         filling agent's toolConfig + dispatcher
  author_tools.py  authoring agent's toolConfig + dispatcher
functions/
  api_create_session.py   POST   /sessions
  api_get_session.py      GET    /sessions/{id}
  api_set_fields.py       PATCH  /sessions/{id}/fields
  api_validate.py         POST   /sessions/{id}/validate
  api_render.py           POST   /sessions/{id}/render
  api_catalog_list.py     GET    /catalog
  api_catalog_get.py      GET    /catalog/{cid}
  api_catalog_create.py   POST   /catalog              promote a session
  api_catalog_update.py   PATCH  /catalog/{cid}        edit + publish
  api_catalog_source.py   POST   /catalog/{cid}/sources
  api_catalog_session.py  POST   /catalog/{cid}/sessions   <- the fast path
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

**Both, from here**

4. Frontend draws field boxes from `schema[].bbox` (normalized 0–1,
   top-left origin, so overlay works at any zoom).
5. User types → `PATCH .../fields`. User chats → WebSocket `message`.
6. `POST .../render` → presigned download.

**Defining a form** reuses the upload flow rather than adding a second
pipeline: upload the blank form, `POST /catalog` to promote the finished
session into a draft, write the guide over the `author` WebSocket route, then
`PATCH /catalog/{cid}` with `status: published`.

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
```

All five run without AWS credentials.

## Not done yet

- **Auth.** The Cognito authorizer is commented out in `template.yaml`.
  Do not point this at real documents until it is on. This matters more now
  than it did: with `caller()` returning `"anonymous"` for everyone, **any
  user can edit or publish any catalog entry**, including one somebody else
  reviewed. The entry carries `created_by` and a `status` so an
  author-or-admin check is a policy change rather than a migration.
- **DOCX → PDF** needs LibreOffice, which is ~400MB — past the 250MB layer
  limit. `ExtractFn` has to become a container image function.
- **Textract** is not wired in. `ingest_enrich` does field detection with
  vision alone. For English forms, Textract `AnalyzeDocument` with `FORMS`
  is cheaper and probably more accurate — the seam is `_invoke()`.
- **Hebrew/RTL overlay** needs a TTF at `RTL_FONT_PATH` in the layer;
  reportlab's built-in fonts render Hebrew as black boxes. Irrelevant if
  your forms are AcroForm PDFs or English.
- **Profile lookup.** `source: "profile"` is accepted by the tool contract
  but nothing populates a saved profile yet.
- **Bedrock Guardrails** for PII are not attached.
