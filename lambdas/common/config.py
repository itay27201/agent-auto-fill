"""Central configuration. Everything is env-driven so the same code runs in
all stages without a rebuild."""
import os

# ---------------------------------------------------------------- storage
TABLE_NAME = os.environ.get("TABLE_NAME", "form-agent")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "")
ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")

# ---------------------------------------------------------------- bedrock
# Sonnet 4.6 cannot be invoked on-demand by its base id
# ("anthropic.claude-sonnet-4-6"). Bedrock requires an *inference profile*,
# which is the base id with a geography prefix:
#
#   eu.anthropic.claude-sonnet-4-6      keeps traffic inside the EU
#   us.anthropic.claude-sonnet-4-6      keeps traffic inside the US
#   apac.anthropic.claude-sonnet-4-6    APAC
#   global.anthropic.claude-sonnet-4-6  routes anywhere, ~10% cheaper
#
# Pick by data-residency requirement. Government forms usually mean a
# geography-pinned profile, not global.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "eu-central-1"))

# Defining a form is a rare, expensive, human-reviewed act; filling one is
# frequent and must be cheap. So the two paths run different models.
#
# Reading a dense RTL government form and deciding which ruled cell each label
# belongs to happens ONCE per form -- the registry and the catalog make sure of
# that -- and every session afterwards inherits the answer. A better model there
# costs a few cents once and is paid back on the first re-upload; a better model
# in the chat agent would be billed on every turn of every session forever.
#
# Opus 4.8 is $5/$25 per MTok against Sonnet 4.6's $3/$15. That premium applies
# to ingest and to the authoring agent's bulk note pass, and to nothing else.
INGEST_MODEL_ID = os.environ.get("INGEST_MODEL_ID", "eu.anthropic.claude-opus-4-8")

# The IAM policy Resource must be the inference-profile ARN, not the base
# model ARN:
#   arn:aws:bedrock:{region}:{account}:inference-profile/eu.anthropic.claude-sonnet-4-6
# plus the foundation-model ARNs in every region the profile can route to.

# Per-request read timeout on the runtime client. Not the time the model is
# allowed to think: both agents and ingest stream, so this only has to cover
# the gap between chunks. botocore's 60s default is what made ingest fail.
BEDROCK_READ_TIMEOUT = int(os.environ.get("BEDROCK_READ_TIMEOUT", "300"))

# Reasoning effort for the define-once pass. Deciding which of forty ruled cells
# a Hebrew label belongs to is exactly the work extra thinking buys, and it is
# billed once per form.
INGEST_EFFORT = os.environ.get("INGEST_EFFORT", "high")


# Models that removed the sampling parameters. Matched as substrings because
# the same model arrives as "claude-opus-4-8", "anthropic.claude-opus-4-8" and
# "eu.anthropic.claude-opus-4-8" depending on who is calling.
_NO_SAMPLING = (
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
    "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
)


def accepts_sampling(model_id: str) -> bool:
    """Whether this model still takes `temperature` / `top_p` / `top_k`.

    Anthropic removed the sampling parameters on the reasoning models: Opus 4.7
    and later, Sonnet 5 and Fable 5 reject them with a 400. Opus 4.6 and Sonnet
    4.6 still accept them, which is why `invoke_json` has always been able to
    send `temperature: 0` — and why adding an Opus tier is the thing that breaks
    it. Getting this wrong is not a slightly worse answer, it is a
    ValidationException on every single ingest.
    """
    return not any(m in model_id for m in _NO_SAMPLING)

MAX_AGENT_TURNS = int(os.environ.get("MAX_AGENT_TURNS", "8"))
# Generous on purpose. Only tokens actually generated are billed, so a high
# ceiling costs nothing on a normal turn — but a reply truncated mid-tool-call
# loses the tool block entirely, and Hebrew runs 2-3x more tokens than English
# for the same text, so 4096 was close enough to the edge to matter.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "16384"))

# ---------------------------------------------------------------- ingest
# Which generation of the pipeline built a cached schema.
#
# The registry is keyed by document SHA-256 and has no TTL, which is what makes
# the second upload of a form nearly free. It also means a schema built by an
# older, worse pipeline is inherited forever: re-uploading the identical file
# hashes the same, hits the cache, and never re-runs ingest at all. There was no
# way to invalidate it short of deleting the DynamoDB item by hand.
#
# So a cache hit must match on version as well as on hash. Bump this whenever a
# change alters what ingest *produces* — not when it only changes how it gets
# there — and every previously-cached form re-ingests once, automatically, the
# next time somebody uploads it.
#
#   1  vision-estimated bboxes, no geometry, no form map
#   2  boxes from the PDF's own ruled geometry, sanity-checked, + form-map.md
#   3  collinear rules no longer swallow each other, so a row comes back as its
#      real columns instead of one box spanning several; checkbox glyphs are
#      writing areas rather than printed text; boxes printed as character cells
#      carry a comb; Textract may subdivide a region the PDF read too wide
SCHEMA_VERSION = int(os.environ.get("SCHEMA_VERSION", "3"))

INGEST_STATE_MACHINE_ARN = os.environ.get("INGEST_STATE_MACHINE_ARN", "")
RASTER_DPI = int(os.environ.get("RASTER_DPI", "150"))
MAX_INGEST_PAGES = int(os.environ.get("MAX_INGEST_PAGES", "20"))
# Output budget for the one enrich call. A full field description — label,
# help text, validation, bbox — runs ~200 tokens, so a form with a hundred
# fields needs far more than the 4096 a chat turn gets. Measured: two pages
# of the ITC-101 income-tax form blew straight past 8192 and truncated.
#
# Thinking counts against this too, which is what 32000 ran out of rather than
# the answer being long. Observed live on page one of the 101, at 92 regions and
# INGEST_EFFORT=high: the budget was gone after 3029 characters of JSON, so
# essentially all of it went on reasoning. Opus 4.8 caps at 128000 and llm_json
# streams, so there is room; 64000 leaves the define pass enough to think about a
# dense page and still write the schema. The effort setting is deliberate and
# stays where it is — this is billed once per form.
ENRICH_MAX_TOKENS = int(os.environ.get("ENRICH_MAX_TOKENS", "64000"))

# ---------------------------------------------------------------- authoring
# Field notes are written in chunks, not one tool call per field. 97 separate
# calls is what blew the agent's turn budget and left a guide covering 70 of
# 97 fields published as finished.
#
# Chunk size trades two failures against each other: too large and the answer
# truncates (the reason ENRICH_MAX_TOKENS above is 32000), too small and the
# fan-out pays the per-call overhead more often than it needs to. 15 notes at
# ~200 chars each sits well inside NOTE_MAX_TOKENS.
NOTE_CHUNK_SIZE = int(os.environ.get("NOTE_CHUNK_SIZE", "15"))
NOTE_MAX_TOKENS = int(os.environ.get("NOTE_MAX_TOKENS", "8000"))
# Bedrock throttling is the ceiling here, not CPU. Four in flight keeps a long
# form under a minute without tripping the account's requests-per-minute quota.
NOTE_CONCURRENCY = int(os.environ.get("NOTE_CONCURRENCY", "4"))

# ---------------------------------------------------------------- misc
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
UPLOAD_URL_TTL = int(os.environ.get("UPLOAD_URL_TTL", "900"))
DOWNLOAD_URL_TTL = int(os.environ.get("DOWNLOAD_URL_TTL", "900"))
# Presigned GET TTL for page images shown in the viewer. Longer than
# DOWNLOAD_URL_TTL: a person filling out a long form stays on one page for a
# lot longer than the time it takes to grab a finished render.
VIEW_URL_TTL = int(os.environ.get("VIEW_URL_TTL", "3600"))
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

# Hebrew / RTL text stamped onto flat PDFs needs a real TTF; the reportlab
# built-ins have no Hebrew glyphs. Ship one in the layer.
RTL_FONT_PATH = os.environ.get("RTL_FONT_PATH", "/opt/fonts/NotoSansHebrew-Regular.ttf")
