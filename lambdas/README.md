# Form-filling agent — backend

Twelve Lambdas behind a REST API, a WebSocket agent, and a Step Functions
ingest pipeline. Agent runs on Bedrock Sonnet 4.6.

## Layout

```
common/            shared across every function
  config.py        env-driven settings, model id
  aws.py           lazy boto3 clients
  api.py           REST plumbing, CORS, error wrapping
  schema.py        FormField model + validation      <- the contract
  store.py         DynamoDB single-table access
  tools.py         Bedrock toolConfig + dispatcher
functions/
  api_create_session.py   POST   /sessions
  api_get_session.py      GET    /sessions/{id}
  api_set_fields.py       PATCH  /sessions/{id}/fields
  api_validate.py         POST   /sessions/{id}/validate
  api_render.py           POST   /sessions/{id}/render
  ingest_classify.py      S3 trigger + step 1
  ingest_extract.py       step 2
  ingest_enrich.py        step 3 (the only Bedrock call in ingest)
  ingest_finalize.py      step 4 + failure handler
  agent_chat.py           WebSocket agent loop
statemachine/ingest.asl.json
template.yaml
tests/                    run without AWS
```

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

1. `POST /sessions` → session id + presigned PUT URL. The browser uploads
   straight to S3; the document never passes through Lambda, which would cap
   it at 6MB.
2. S3 `ObjectCreated` → `on_upload` → state machine.
3. Classify → extract → enrich → finalize. Poll `GET /sessions/{id}` until
   `status` is `ready`.
4. Frontend draws field boxes from `schema[].bbox` (normalized 0–1,
   top-left origin, so overlay works at any zoom).
5. User types → `PATCH .../fields`. User chats → WebSocket `message`.
6. `POST .../render` → presigned download.

### WebSocket protocol

Send:
```json
{"action": "message", "session_id": "...", "message": "...",
 "scope_field_ids": ["national_id", "family_name"]}
```

`scope_field_ids` is what the user selected in the viewer. It shrinks the
prompt to one section and constrains where the agent may write.

Receive: `turn_start`, `text` (token deltas), `tool_start`, `field_updated`,
`highlight`, `turn_end`, `error`.

## Design decisions worth knowing

**The agent never touches the document.** It edits form state; rendering is a
separate step. This is why the same agent works across PDF, DOCX, fillable
and flat.

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
python3 tests/test_roundtrip.py   # build a fillable PDF, extract, fill, verify
python3 tests/test_tools.py       # tool dispatcher + source enforcement
```

Both run without AWS credentials.

## Not done yet

- **Auth.** The Cognito authorizer is commented out in `template.yaml`.
  Do not point this at real documents until it is on.
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
