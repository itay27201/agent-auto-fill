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

# The IAM policy Resource must be the inference-profile ARN, not the base
# model ARN:
#   arn:aws:bedrock:{region}:{account}:inference-profile/eu.anthropic.claude-sonnet-4-6
# plus the foundation-model ARNs in every region the profile can route to.

# Per-request read timeout on the runtime client. Not the time the model is
# allowed to think: both agents and ingest stream, so this only has to cover
# the gap between chunks. botocore's 60s default is what made ingest fail.
BEDROCK_READ_TIMEOUT = int(os.environ.get("BEDROCK_READ_TIMEOUT", "300"))

MAX_AGENT_TURNS = int(os.environ.get("MAX_AGENT_TURNS", "8"))
# Generous on purpose. Only tokens actually generated are billed, so a high
# ceiling costs nothing on a normal turn — but a reply truncated mid-tool-call
# loses the tool block entirely, and Hebrew runs 2-3x more tokens than English
# for the same text, so 4096 was close enough to the edge to matter.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "16384"))

# ---------------------------------------------------------------- ingest
INGEST_STATE_MACHINE_ARN = os.environ.get("INGEST_STATE_MACHINE_ARN", "")
RASTER_DPI = int(os.environ.get("RASTER_DPI", "150"))
MAX_INGEST_PAGES = int(os.environ.get("MAX_INGEST_PAGES", "20"))
# Output budget for the one enrich call. A full field description — label,
# help text, validation, bbox — runs ~200 tokens, so a form with a hundred
# fields needs far more than the 4096 a chat turn gets. Measured: two pages
# of the ITC-101 income-tax form blew straight past 8192 and truncated.
ENRICH_MAX_TOKENS = int(os.environ.get("ENRICH_MAX_TOKENS", "32000"))

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
