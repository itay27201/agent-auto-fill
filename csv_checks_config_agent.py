"""
CSV Checks Config Agent

Interactive agent for creating and managing CSV/XLSX file type configurations.
Helps users define:
  1. Extraction fields — what structured data to pull from each sheet type
  2. Control check codes — Python validation rules (cross-totals, completeness,
     cross-sheet reconciliation, format validity, outlier detection)

Each check is stored in S3 as JSON with status "draft" until the user tests and
approves it, after which status becomes "active".
"""

import ast
import hashlib
import json
import logging
import sys
import os
import re
import boto3
from botocore.config import Config as BotoConfig
from datetime import datetime, timezone
from typing import Optional
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

from config import S3_BUCKET, S3_CHECKS_PREFIX, S3_REGION, AWS_REGION

# Two models, two jobs. The conversational agent routes tools and talks to the user —
# Sonnet handles that well and every turn pays for it. Writing the check's Python is the
# hard part and the part that was failing, so it gets Opus and pays only per draft.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")
CODEGEN_MODEL_ID = os.environ.get("CODEGEN_MODEL_ID", "eu.anthropic.claude-opus-4-8")

# Adaptive thinking is off unless asked for on this model family. Verified accepted by
# invoke_model with the bedrock-2023-05-31 body; the switch is here so it can be turned
# off with an env var rather than a redeploy if that ever changes.
CODEGEN_THINKING = os.environ.get("CODEGEN_THINKING", "1").strip().lower() not in ('0', 'false', 'no')
CODEGEN_EFFORT = os.environ.get("CODEGEN_EFFORT", "high")

# max_tokens caps thinking AND response text together, so the old 4096 would truncate a
# check mid-function once thinking is on.
CODEGEN_MAX_TOKENS = int(os.environ.get("CODEGEN_MAX_TOKENS", "16000"))

# The reviewer that reads finished code back against the user's ORIGINAL words. Sonnet, not
# the codegen Opus: this is a reading task, it runs on every check, and using a different
# model from the one that wrote the code is the point — a model reviewing its own output
# agrees with itself. No thinking block; the review is short and structured.
REVIEW_MODEL_ID = os.environ.get("REVIEW_MODEL_ID", BEDROCK_MODEL_ID)
REVIEW_MAX_TOKENS = int(os.environ.get("REVIEW_MAX_TOKENS", "4000"))

# How many real rows the reviewer is shown. lambda_profile_csv_file stores exactly 15 per
# sheet (SAMPLE_ROWS_IN_PROFILE), so this asks for all of them: the profile says which values
# exist in a column, these say what a ROW looks like, and that is where the bugs that survive
# a green test run actually live.
SAMPLE_ROWS_IN_REVIEW = int(os.environ.get("CSV_CHECKS_REVIEW_SAMPLE_ROWS", "15"))

# The executor Lambda that actually runs draft code against the loaded file.
EXECUTOR_FUNCTION = os.environ.get("CSV_CHECKS_EXECUTOR_FUNCTION", "lambda-csv-checks-executor")

# The executor's own ceiling is 300s; boto3 defaults to a 60s read timeout, which would
# abandon a run that is still going. Retries off — a re-run is never free here.
_LAMBDA_CFG = BotoConfig(read_timeout=310, connect_timeout=10, retries={'max_attempts': 0})

# How many draft/test cycles the agent may spend on one check before it must stop and
# report. Enforced in code, not just asked for in the prompt.
MAX_TEST_ITERATIONS = int(os.environ.get("CSV_CHECKS_MAX_ITERATIONS", "4"))

# ============== S3 HELPERS ==============

_s3 = None

def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client('s3', region_name=S3_REGION)
    return _s3


def _s3_key(file_type: str, filename: str) -> str:
    return f"{S3_CHECKS_PREFIX}{file_type}/{filename}"


def _read_json(file_type: str, filename: str) -> Optional[dict]:
    try:
        resp = _get_s3().get_object(Bucket=S3_BUCKET, Key=_s3_key(file_type, filename))
        return json.loads(resp['Body'].read().decode('utf-8'))
    except _get_s3().exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"Read {file_type}/{filename}: {e}")
        return None


def _write_json(file_type: str, filename: str, data: dict) -> bool:
    try:
        _get_s3().put_object(
            Bucket=S3_BUCKET,
            Key=_s3_key(file_type, filename),
            Body=json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'),
            ContentType='application/json'
        )
        return True
    except Exception as e:
        logger.error(f"Write {file_type}/{filename}: {e}")
        return False


def _write_text(file_type: str, filename: str, text: str) -> bool:
    """Write a plain-text file (e.g. a .py check script) to S3."""
    try:
        _get_s3().put_object(
            Bucket=S3_BUCKET,
            Key=_s3_key(file_type, filename),
            Body=text.encode('utf-8'),
            ContentType='text/x-python'
        )
        return True
    except Exception as e:
        logger.error(f"Write text {file_type}/{filename}: {e}")
        return False


def _read_text(file_type: str, filename: str) -> Optional[str]:
    """Read a plain-text file (e.g. a draft .py) from S3, or None when absent."""
    try:
        resp = _get_s3().get_object(Bucket=S3_BUCKET, Key=_s3_key(file_type, filename))
        return resp['Body'].read().decode('utf-8')
    except _get_s3().exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"Read text {file_type}/{filename}: {e}")
        return None


# ============== DRAFT SCRATCH SPACE ==============
#
# A check under construction lives at csv_checks/{file_type}/_draft.py while the agent
# iterates on it. Keeping it server-side means the agent never has to echo a hundred lines
# of Python through a tool argument to test or save it — it names the file type, and the
# code stays where it is.
#
# One draft slot per file type: two people configuring the same file type at the same
# moment would overwrite each other. That matches how the wizard is actually used (one
# person, one file type, one sitting); a session-scoped key is the fix if it ever bites.

DRAFT_CODE_FILE = '_draft.py'
DRAFT_META_FILE = '_draft.json'
REVIEW_FILE = '_draft_review.json'


def _clear_draft(file_type: str) -> None:
    """Remove the scratch draft. Best-effort — a stale draft is overwritten, never read blind."""
    # The review goes with it. It is fingerprinted against the code so a leftover could not be
    # misapplied anyway, but leaving an approval lying around next to no draft invites exactly
    # that mistake the next time someone reads this bucket by hand.
    keys = [_s3_key(file_type, DRAFT_CODE_FILE), _s3_key(file_type, DRAFT_META_FILE),
            _s3_key(file_type, REVIEW_FILE)]
    _, errors = _delete_keys(keys)
    if errors:
        logger.warning(f"Could not fully clear draft for '{file_type}': {'; '.join(errors)}")


def _invoke_executor_test(file_type: str, code: str, s3_uri: str, seed: int) -> dict:
    """
    Run draft code against the loaded data file via the executor Lambda, synchronously.

    Returns the parsed run body, or {'error': ...} — never raises, because a failed test
    run is information the agent should act on, not a turn-ending exception.
    """
    payload = {
        "file_type": file_type,
        "pdf_file_path": s3_uri,
        "mode": "test_code",
        "code": code,
        "sample_seed": seed,
        "request_type": "csv-checks-executor",
    }
    try:
        client = boto3.client('lambda', region_name=AWS_REGION, config=_LAMBDA_CFG)
        resp = client.invoke(
            FunctionName=EXECUTOR_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        )
    except Exception as e:
        logger.error(f"Executor invoke failed: {e}")
        return {"error": f"Could not invoke {EXECUTOR_FUNCTION}: {e}"}

    if resp.get('FunctionError'):
        raw = resp['Payload'].read().decode('utf-8', errors='replace')
        logger.error(f"Executor returned FunctionError: {raw[:600]}")
        return {"error": f"The executor Lambda failed: {raw[:600]}"}

    try:
        outer = json.loads(resp['Payload'].read().decode('utf-8'))
    except Exception as e:
        return {"error": f"Could not parse the executor response: {e}"}

    # The executor wraps its answer as {'statusCode': .., 'body': '<json>'}
    body = outer.get('body', outer)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return {"error": f"Executor returned a non-JSON body: {body[:400]}"}

    if outer.get('statusCode', 200) != 200:
        return {"error": body.get('error', str(body)[:400])}
    return body


def _render_test_result(result: dict) -> str:
    """
    Render a test run for the agent to read.

    Both row samples are always shown, and the passed sample is labelled as the place to
    look. A check that reports zero gaps is the ambiguous case — clean file, or a rule
    that matched nothing? — and only these rows can tell the two apart.
    """
    if result.get('error'):
        return f"❌ The test run failed: {result['error']}\nThe draft was NOT changed."

    lines = []
    if result.get('execution_error'):
        lines.append(f"💥 THE CODE CRASHED: {result['execution_error']}")
        lines.append("Fix the error before judging the rule itself.\n")
    else:
        verdict = "PASSED (no gaps found)" if result.get('passed') else f"FAILED — {result.get('gap_count', 0)} gap(s)"
        lines.append(f"Result: {verdict}")
        lines.append(f"Rows scanned: {result.get('rows_scanned', '?')}\n")

    gaps = result.get('gaps', [])
    if gaps:
        lines.append(f"GAPS (showing {len(gaps)} of {result.get('gap_count', len(gaps))}):")
        for g in gaps:
            lines.append(
                f"  row {g.get('row')} · {g.get('col')!r} = {g.get('value')!r} "
                f"(expected {g.get('expected_value')!r}) — {g.get('description')}"
            )
        if result.get('gaps_truncated'):
            lines.append(f"  …and {result['gaps_truncated']} more not shown.")
        lines.append("")

    samples = result.get('row_samples') or {}
    for note in samples.get('notes', []):
        lines.append(f"note: {note}")
    lines.append("")

    # The fastest way to spot a whole class of value the rule never touched.
    vb = samples.get('value_breakdown') or {}
    if vb.get('column'):
        def _fmt(entries):
            return ", ".join(f"{e['value']!r}×{e['count']}" for e in entries) or "(none)"
        lines.append(f"VALUE BREAKDOWN for {vb['column']!r} — compare the two sides:")
        lines.append(f"  in FLAGGED rows: {_fmt(vb.get('flagged', []))}")
        lines.append(f"  in PASSED  rows: {_fmt(vb.get('passed', []))}")
        lines.append(
            "  If a value that should have been caught appears on the PASSED side, the rule "
            "missed an entire class of row — fix it before saving."
        )
        lines.append("")

    flagged = samples.get('flagged', [])
    lines.append(f"--- RANDOM SAMPLE OF ROWS THE CHECK FLAGGED ({len(flagged)}) ---")
    lines.append("Look for rows that are actually fine — those are false positives.")
    lines += [f"  {json.dumps(r, ensure_ascii=False)}" for r in flagged] or ["  (none — the check flagged nothing)"]

    passed = samples.get('passed', [])
    lines.append("")
    lines.append(f"--- RANDOM SAMPLE OF ROWS THE CHECK LET THROUGH ({len(passed)}) ---")
    lines.append(
        "THIS IS WHERE THE BUG USUALLY IS. Read every row and ask whether the rule you were "
        "asked for should have caught it. A row holding '#', '', '##' or an out-of-range "
        "value that appears here is a FALSE NEGATIVE — the check is broken even though it "
        "reported a result."
    )
    lines += [f"  {json.dumps(r, ensure_ascii=False)}" for r in passed] or ["  (none)"]

    return "\n".join(lines)


def _save_check_version(file_type: str, check_id: str, data: dict) -> None:
    """
    Snapshot a check under versions/{check_id}/{timestamp}.json.

    Best-effort by design: the live check_NNN.json is the source of truth, and failing
    to archive history must never block a save the user asked for.
    """
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if not _write_json(file_type, f"versions/{check_id}/{stamp}.json", data):
        logger.warning(f"Could not archive version for {check_id} — live check was still saved")


# ============== FILE PROFILE ==============
#
# The profile is written by lambda_profile_csv_file when the user loads a data file in
# step 2. It is what replaced interrogating the user for column names and sample rows:
# the uploaded file already holds those answers, so the agent reads them from here.
#
# It is built through the same loader the executor uses, so the column names below are
# exactly the ones analyze_data() will receive — including that header whitespace has
# already been stripped ('סוג המסמך ' arrives as 'סוג המסמך').

def _load_file_profile(file_type: str) -> Optional[dict]:
    """Load the stored data profile for a file type, or None when no file has been loaded yet."""
    return _read_json(file_type, 'file_profile.json')


def _render_profile_for_codegen(profile: dict, max_sample_rows: int = 3) -> str:
    """
    Render a profile as compact text for the code-generation prompt.

    Emphasises the two things generated code gets wrong without it: the exact column
    spelling, and each column's real domain (so an allowed-value rule can be written
    against the values that actually occur rather than guessed ones).
    """
    if not profile:
        return ""

    total = profile.get('total_rows', -1)
    total_txt = f"{total:,}" if isinstance(total, int) and total >= 0 else "unknown"

    out = [
        f"DATA PROFILE — file type '{profile.get('file_type', '')}'",
        f"Source file: {profile.get('source_s3_path', 'n/a')}",
        f"Checks execute against the FIRST {profile.get('sample_rows', '?')} rows "
        f"(file has {total_txt} rows in total).",
        "",
        "These are the DataFrames analyze_data() receives as list_of_dfs "
        "(also table_0, table_1, ...).",
        "Use the column names below VERBATIM — they are already whitespace-stripped, "
        "exactly as they will appear on the DataFrame.",
        "",
        "CAUTION: dtypes and value lists below come from this sample only. Values that do "
        "not appear here can still occur elsewhere in the file (a '#' sentinel in a column "
        "that looks numeric here is the common case), so compare defensively — normalise "
        "with .astype(str).str.strip() before equality/membership tests.",
    ]

    for sheet in profile.get('sheets', []):
        out.append("")
        out.append(
            f"{sheet.get('name', 'table_0')} — {sheet.get('profiled_rows', 0)} rows profiled, "
            f"{sheet.get('n_columns', 0)} columns"
        )
        for col in sheet.get('columns', []):
            tops = col.get('top_values', []) or []
            tops_txt = ", ".join(f"{t.get('value')!r}({t.get('count')})" for t in tops[:8])
            out.append(
                f"  [{col.get('index'):>2}] {col.get('name')!r}  "
                f"dtype={col.get('dtype')}  nulls={col.get('null_count')}  "
                f"distinct={col.get('n_distinct')}"
            )
            if tops_txt:
                out.append(f"        values: {tops_txt}")

        rows = sheet.get('sample_rows', [])[:max_sample_rows]
        if rows:
            out.append(f"  sample rows (first {len(rows)}):")
            for r in rows:
                out.append(f"    {json.dumps(r, ensure_ascii=False)[:600]}")

    return "\n".join(out)


def _render_sample_rows(profile: dict, max_rows: int = SAMPLE_ROWS_IN_REVIEW,
                        max_chars_per_row: int = 1600) -> str:
    """
    The first rows of the real file, verbatim, one JSON object per row.

    Deliberately NOT the same information as the column profile above. The profile says which
    values exist in a column; these rows say what a row LOOKS LIKE — and the mistakes that
    survive a green test run live between the columns, not inside one: a total row sitting
    among the detail rows, a repeated header, '1,234' stored as text in a column the profile
    calls numeric, a '#' sentinel where the code calls pd.to_numeric. A reviewer that can see
    a row can say what the code would do to it; one that only sees column summaries cannot.

    Returns '' when no rows are stored, so the caller can say so rather than imply the file
    was checked.
    """
    if not profile:
        return ""

    out = []
    for sheet in profile.get('sheets', []) or []:
        rows = (sheet.get('sample_rows') or [])[:max_rows]
        if not rows:
            continue
        out.append("")
        out.append(
            f"{sheet.get('name', 'table_0')} — first {len(rows)} row(s) of "
            f"{sheet.get('profiled_rows', '?')} profiled, exactly as analyze_data() receives "
            f"them (idx = DataFrame index):"
        )
        for i, row in enumerate(rows):
            line = json.dumps(row, ensure_ascii=False)
            if len(line) > max_chars_per_row:
                line = line[:max_chars_per_row] + " …(truncated)"
            out.append(f"  idx {i}: {line}")

    if not out:
        return ""
    header = "THE FIRST ROWS OF THE REAL FILE — read these before ruling on anything:"
    return header + "\n".join(out)


def _profile_or_hint(file_type: str, provided_sample: str = "") -> str:
    """
    Resolve the data description for code generation.

    Prefers the stored profile; falls back to anything the user explicitly pasted, and
    finally to the registered column list. Returning a clear "no profile" note matters:
    it tells the model to say so rather than invent column names.
    """
    profile = _load_file_profile(file_type)
    if profile:
        rendered = _render_profile_for_codegen(profile)
        if provided_sample and provided_sample.strip():
            rendered += f"\n\nADDITIONAL SAMPLE SUPPLIED BY THE USER:\n{provided_sample.strip()}"
        return rendered

    if provided_sample and provided_sample.strip():
        return provided_sample.strip()

    meta = _read_json(file_type, 'metadata.json')
    cols = ', '.join(meta.get('expected_columns', []) if meta else [])
    return (
        f"NO DATA PROFILE AVAILABLE for file type '{file_type}'. "
        f"Registered columns: {cols or 'none recorded'}. "
        f"Write defensively: verify a column exists before using it, and report a clear "
        f"gap if the expected column is missing."
    )


def _list_keys(prefix: str, strict: bool = False) -> list:
    """
    List every key under a prefix.

    strict=True re-raises instead of returning [] — destructive callers must not
    mistake "S3 is unreachable" for "this prefix is empty".
    """
    try:
        paginator = _get_s3().get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys.append(obj['Key'])
        return keys
    except Exception as e:
        logger.error(f"List {prefix}: {e}")
        if strict:
            raise
        return []


# A file type is a single S3 path segment. Anything else — empty, a slash, a wildcard —
# would widen the delete prefix to csv_checks/ and wipe every configured type.
_FILE_TYPE_RE = re.compile(r'^[a-z0-9][a-z0-9_.\-]*$')


def _normalize_file_type(file_type: str) -> str:
    """The normalisation create_file_type() applies, so delete targets what create wrote."""
    return (file_type or '').strip().lower().replace(' ', '_')


def _delete_keys(keys: list) -> tuple:
    """
    Delete S3 keys with delete_objects, batched at the API's 1000-key limit.

    Returns (deleted_keys, errors). S3 reports per-object failures in the 200 response
    body rather than as an exception, so a partial delete must be surfaced, not silently
    reported as success.
    """
    deleted, errors = [], []
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            resp = _get_s3().delete_objects(
                Bucket=S3_BUCKET,
                Delete={'Objects': [{'Key': k} for k in batch], 'Quiet': False}
            )
            deleted.extend(o['Key'] for o in resp.get('Deleted', []))
            for err in resp.get('Errors', []):
                errors.append(f"{err.get('Key')}: {err.get('Code')} {err.get('Message')}")
        except Exception as e:
            logger.error(f"delete_objects batch of {len(batch)} failed: {e}")
            errors.extend(f"{k}: {e}" for k in batch)
    return deleted, errors


def _next_check_id(file_type: str) -> str:
    prefix = f"{S3_CHECKS_PREFIX}{file_type}/check_"
    existing = [k for k in _list_keys(prefix) if k.endswith('.json')]
    nums = []
    for k in existing:
        m = re.search(r'check_(\d+)\.json$', k)
        if m:
            nums.append(int(m.group(1)))
    next_num = max(nums) + 1 if nums else 1
    return f"check_{next_num:03d}"


# ============== CODE GENERATION HELPER ==============

def _invoke_bedrock_text(model_id: str, system_prompt: str, user_message: str,
                         max_tokens: int, thinking: bool = False, effort: str = "high",
                         label: str = "model") -> str:
    """
    One Bedrock call, returning the first TEXT block verbatim.

    Deliberately does NOT strip markdown fences: the codegen callers want that and the
    reviewer, which returns JSON, does not — `^```\\s*` would leave a bare `json` line
    behind and break the parse.
    """
    client = boto3.client('bedrock-runtime', region_name=AWS_REGION)

    request = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}]
    }
    if thinking:
        request["thinking"] = {"type": "adaptive"}
        request["output_config"] = {"effort": effort}

    response = client.invoke_model(
        modelId=model_id,
        contentType='application/json',
        accept='application/json',
        body=json.dumps(request)
    )
    body = json.loads(response['body'].read())

    # Take the first TEXT block, not content[0]. With thinking on, a thinking block can
    # come first, and indexing position 0 blindly would either throw or return reasoning
    # in place of the answer.
    text = next(
        (b.get('text', '') for b in body.get('content', []) if b.get('type') == 'text'),
        ''
    ).strip()

    if body.get('stop_reason') == 'max_tokens':
        logger.warning(
            f"{label} hit max_tokens ({max_tokens}) — the output is probably truncated."
        )
    return text


def _invoke_codegen(system_prompt: str, user_message: str) -> str:
    """
    One Bedrock call that returns bare Python.

    Shared by first-draft generation and the repair pass so both get the same model,
    the same thinking settings, and — the part that actually matters — the same
    text-block extraction and fence stripping. A repair that returned a fenced string
    while the generator returned bare code would fail validation for a reason that has
    nothing to do with the fix.
    """
    code = _invoke_bedrock_text(
        model_id=CODEGEN_MODEL_ID,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=CODEGEN_MAX_TOKENS,
        thinking=CODEGEN_THINKING,
        effort=CODEGEN_EFFORT,
        label="Codegen",
    )
    # Strip markdown fences if model added them
    code = re.sub(r'^```python\s*', '', code)
    code = re.sub(r'^```\s*', '', code)
    code = re.sub(r'\s*```$', '', code)
    return code.strip()


def _generate_check_code(
    check_name: str,
    check_description: str,
    check_type: str,
    sample_data: str,
    accounting_context_hint: str,
) -> str:
    """Call Bedrock to generate a Python check function."""
    system_prompt = f"""You are an expert Python data validation engineer writing control checks for IFRS 17 insurance control sheets (Excel/CSV exported from SAP).

TASK: Write a Python function called `analyze_data` that performs the validation check described below.

CRITICAL OUTPUT FORMAT:
The function MUST return a dict with this exact structure:
{{
  "passed": bool,       # True if check passed (no gaps found)
  "gaps": [             # List of gaps — empty list if passed
    {{
      "description": str,     # Clear description of what is wrong
      "sheet": str,           # Sheet/DataFrame name (e.g. "table_0", "Premiums")
      "row": int,             # 0-based row index in the DataFrame (or -1 if whole-sheet issue)
      "col": str,             # Column name
      "value": any,           # The actual problematic value ("בפועל")
      "expected_value": any   # What the value should have been ("נדרש"), or None — see rule 7
    }}
  ]
}}

FUNCTION SIGNATURE:
def analyze_data():
    import pandas as pd
    import numpy as np
    # list_of_dfs is available as a global variable — a list of DataFrames, one per sheet
    # Access them as list_of_dfs[0], list_of_dfs[1], etc.
    # Also available as table_0, table_1, etc.
    ...
    return {{"passed": bool, "gaps": [...]}}

RULES:
1. Always wrap in try/except — if an exception occurs, return {{"passed": False, "gaps": [{{"description": "Check error: <error message>", "sheet": "N/A", "row": -1, "col": "N/A", "value": None}}]}}
   Note: use a regular string in the except block, not f-string with braces
2. Handle empty DataFrames gracefully
3. NUMERIC CONVERSION — CLEAN THE STRING BEFORE YOU COERCE. `pd.to_numeric(s, errors='coerce')`
   on its own is a trap on these exports: SAP writes thousands separators and currency symbols
   into the cell, so '1,234.56' and '₪1,234' both coerce to NaN. A cross-total rule then sums
   NaN, matches nothing, and reports passed=True on a file that is actually wrong — the worst
   possible outcome here. Always strip first, then coerce:
       s    = df[col].astype(str).str.replace(',', '', regex=False)
       s    = s.str.replace('₪', '', regex=False).str.replace('$', '', regex=False).str.strip()
       vals = pd.to_numeric(s, errors='coerce')
   After coercing, decide explicitly what a NaN means for your rule. An unparseable value is
   usually a gap in its own right, not a row to drop silently.
4. Check type: {check_type}
5. Accounting context: {accounting_context_hint}
6. Return ONLY raw Python code — no markdown fences, no explanations
7. ALWAYS set "expected_value" whenever the rule implies a specific correct value, so the user can
   see required-vs-actual side by side. Examples: an allowed-value rule gives the allowed set
   ("1 or 2"); a tag-pattern rule gives the pattern ("50XX or 30XX"); a cross-total rule gives the
   computed total the row should have matched; a required-field rule gives a short label such as
   "any non-empty value". Use None ONLY when the rule genuinely implies no single expected value
   (for example a statistical outlier).
8. NEVER TRUST THE PROFILED DTYPE. The dtypes shown in the data profile were inferred from a
   sample of the file, and a different slice can infer differently. A tagging column that looks
   like int64 in the sample becomes an object/str column as soon as one row holds a sentinel like
   '#', and an `== 1` comparison then silently matches nothing. So for any equality, membership or
   sentinel test on a categorical/tagging/code column, normalise first:
       s = df[col].astype(str).str.strip()
   and compare against string literals ('1', '2', '#'). Reserve pd.to_numeric(..., errors='coerce')
   for columns you are doing real arithmetic on.
9. TREAT MISSING-DATA SENTINELS AS MISSING. These SAP exports encode "not tagged" as the literal
   string '#' (and sometimes '##' or ''), not as NaN. A completeness or required-field rule must
   count '#' as missing, otherwise it passes on exactly the rows it exists to catch.
10. VERIFY THE COLUMN EXISTS before using it: `if col not in df.columns:` return a clear gap saying
   the column is absent. A KeyError surfaces to the user as an opaque execution error.
11. NEVER LOOP OVER EVERY ROW. This is not a style preference — the final run executes your code
   over the WHOLE file (971,000+ rows), and a per-row loop is what makes it time out. Measured on
   that row count, for one identical rule producing identical output:
       df.iterrows()                    46.4s
       for i in range(len(df)) + .iloc   6.4s
       vectorised (below)               0.34s
   So: BANNED — `df.iterrows()`, `df.itertuples()`, `for i in range(len(df))`, `for i in df.index`,
   `.iloc[i]` or `.loc[i]` inside a loop, `df.apply(..., axis=1)`.
   REQUIRED — use pandas and NUMPY to evaluate the condition on whole columns at once, producing a
   boolean mask, then build one gap per FAILING row only. The number of gaps is small; the number
   of rows is not.

   ⚠️ `pd` is already available, but `np` is NOT — you MUST write `import numpy as np` inside
   analyze_data() before using it, or the check dies with NameError at run time.

       import numpy as np
       s    = df[col].astype(str).str.strip()       # whole column, vectorised
       bad  = ~s.isin({{'1', '2'}})                 # whole column -> boolean mask
       vals = s.to_numpy()                          # numpy array: cheap positional access
       for pos in np.flatnonzero(bad.to_numpy()):   # iterates ONLY the failing rows
           gaps.append({{"description": "...", "sheet": sheet, "row": int(pos),
                         "col": col, "value": vals[pos], "expected_value": "1 or 2"}})

   Useful numpy/pandas building blocks — prefer these over any loop:
     np.flatnonzero(mask.to_numpy())   positions of the failures (this is your loop range)
     np.where(cond, a, b)              derive a column conditionally
     np.isclose(a, b, atol=0.01)       float comparison for cross-total rules — never `==`
     s.isin({{...}}) / s.str.startswith(...) / s.str.match(...)   membership and pattern tests
     pd.to_numeric(s, errors='coerce') numeric coercion before arithmetic
     df.groupby(keys)[col].sum()       cross-totals — never accumulate in a loop

   Combine conditions with & | ~ and parenthesise each term — Python's `and`/`or` do not work on
   Series. Convert numpy scalars with int()/float()/str() before putting them in a gap, because
   np.int64 is not JSON-serialisable.

   If a rule genuinely cannot be expressed as a mask, vectorise everything you can first and loop
   only over the already-filtered subset — never over the full frame — and add a line
   `# VECTORISATION-EXEMPT: <reason>` so the reason is recorded.

12. PERCENTAGES. A '%' suffix survives to_numeric as NaN, and one column often mixes '12%' with
   a bare 0.12. Strip and normalise before comparing:
       s   = df[col].astype(str).str.replace('%', '', regex=False).str.strip()
       pct = pd.to_numeric(s, errors='coerce') / 100.0
   For a percentage-change rule compute (new - old) / old and round only at the very end.

13. DATES. Report-period columns in these exports mix formats within a single column. Parse with
   a vectorised two-pass — never with .apply():
       d = pd.to_datetime(df[col], errors='coerce')
       missing = d.isna()
       if missing.any():
           d.loc[missing] = pd.to_datetime(df.loc[missing, col], format='%b %Y', errors='coerce')
   A value still NaT after both passes is unparseable: report it as a gap, do not skip it.

14. NaN-SAFE REDUCTIONS. .idxmax() / .idxmin() raise on an all-NaN column, and that reaches the
   user as an opaque execution error. Guard them:
       idx = vals.idxmax() if vals.notna().any() else None
   The same applies to .max()/.min() when you then index by the result.

15. SAFE INDEXING. Never assume a label or a position exists. Check `if idx is not None and idx
   in df.index:` before .loc[idx], and `if not sub.empty:` before sub.iloc[0]. These frames are
   built WITHOUT resetting the index, so index labels can have holes — prefer positional access
   via .to_numpy() (rule 11) over label lookups wherever the rule allows it.

16. ARITHMETIC. Coerce BOTH operands before any operation between columns (rule 3), and guard
   division so a zero denominator does not produce inf:
       denom = pd.to_numeric(d_s, errors='coerce').replace(0, np.nan)
       ratio = (pd.to_numeric(n_s, errors='coerce') / denom).replace([np.inf, -np.inf], np.nan)
   For float equality in a cross-total rule use np.isclose(a, b, atol=0.01) — never `==`.

17. STRING MATCHING — HEBREW AND ENGLISH. These sheets carry both, and a rule stated in prose
   names only one of them. Match both spellings, case-insensitively, and always pass na=False so
   nulls do not silently become True:
       mask = (s.str.contains('פרמיה', case=False, na=False)
               | s.str.contains('premium', case=False, na=False))
   Combine conditions with & | ~ and parenthesise every term — Python's `and`/`or` do not work
   on a Series.

18. FUZZY FALLBACK — VECTORISED ONLY. If a .str.contains() filter for a value the user named
   returns zero rows, the value is probably spelled differently in the file. Fall back to a
   similarity match computed over the DISTINCT values, not over every row:
       from difflib import SequenceMatcher
       uniq = s.dropna().unique()              # tens of values, not a million rows
       best = max(uniq, key=lambda u: SequenceMatcher(None, str(u), target).ratio())
       mask = s.eq(best)
   The analysis prompts elsewhere in this system do this with df.apply(..., axis=1). Do NOT copy
   that here — rule 11 bans it, and at this file's row count it is 46s against 0.34s.

19. READ FROM list_of_dfs ONLY. Never fabricate data: no df = pd.DataFrame({{...}}), no hardcoded
   row values, no sample rows copied out of the profile. The profile tells you what the file
   looks like; the check must read the real frames it is handed at run time.

BEFORE YOU RETURN — verify every one of these:
- analyze_data() is defined at top level and returns {{"passed": ..., "gaps": [...]}} on EVERY path
- `import numpy as np` is inside the function if you use np. — pd is pre-loaded, np is NOT
- every column you touch is checked against df.columns first (rule 10)
- every string you compare was .astype(str).str.strip()-normalised (rule 8)
- every number you compute on had separators and symbols stripped BEFORE to_numeric (rule 3)
- '#', '##' and '' are counted as missing wherever the rule is about completeness (rule 9)
- there is no .iterrows(), .itertuples(), .apply(axis=1), `for i in range(len(df))` or
  `for i in df.index` anywhere in the code (rule 11)
- every value placed in a gap is a plain Python type via int()/float()/str() — np.int64 is not
  JSON-serialisable and will fail the run
- expected_value is filled in wherever the rule implies a correct value (rule 7)
"""

    user_message = f"""Check name: {check_name}
Check description: {check_description}

Sample data structure (first rows of each sheet):
{sample_data}

Write the analyze_data function now."""

    return _invoke_codegen(system_prompt, user_message)


def _validate_check_code(code: str) -> Optional[str]:
    """
    Reject generated code before it reaches S3.

    A draft that does not parse costs a full upload + Step Function round-trip to discover
    at test time, so catch it here and let the agent regenerate immediately.
    Returns a problem description, or None when the code is usable.
    """
    if not code or not code.strip():
        return "the model returned no code"

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}"

    has_entrypoint = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "analyze_data"
        for node in tree.body
    )
    if not has_entrypoint:
        return "no top-level `analyze_data()` function was defined — the executor has nothing to call"

    # `pd` is injected into the execution namespace by pre_process_csv_file; `np` is NOT.
    # That asymmetry is invisible until the check runs and dies with NameError, so catch it
    # here rather than at test time.
    if re.search(r'\bnp\s*\.', code) and not re.search(r'import\s+numpy\s+as\s+np', code):
        return (
            "it uses `np.` but never imports numpy. Unlike `pd`, numpy is NOT pre-loaded in the "
            "execution namespace — add `import numpy as np` inside analyze_data()"
        )

    slow = _row_wise_constructs(code)
    if slow:
        return (
            f"it walks the DataFrame row by row ({', '.join(slow)}). The final run executes this "
            f"over the whole file — 971,000+ rows — where df.iterrows() measures 46s per check "
            f"against 0.34s for the vectorised form of the same rule, and the run times out. "
            f"Rewrite it as a boolean mask over whole columns and build a gap only for the rows "
            f"the mask selects (rule 11). If a rule truly cannot be vectorised, put a line "
            f"`# VECTORISATION-EXEMPT: <reason>` in the code and it will be accepted"
        )

    return None


# Row-wise pandas idioms. The prompt asks for vectorised code; this is what makes it stick,
# because a prompt rule can be ignored and a validator cannot. Deliberately literal: these
# names are unambiguous in pandas code and cheap to spot.
_ROW_WISE = (
    (r'\.iterrows\s*\(', 'df.iterrows()'),
    (r'\.itertuples\s*\(', 'df.itertuples()'),
    (r'\.apply\s*\([^)]*axis\s*=\s*1', 'df.apply(axis=1)'),
    (r'for\s+\w+\s+in\s+range\s*\(\s*len\s*\(', 'for i in range(len(...))'),
    (r'for\s+\w+\s+in\s+\w+\.index\b', 'for i in df.index'),
)


def _row_wise_constructs(code: str) -> list:
    """Row-wise idioms present in the code, unless it declares an explicit exemption."""
    if 'VECTORISATION-EXEMPT' in code:
        return []
    return [label for pattern, label in _ROW_WISE if re.search(pattern, code)]


def _fix_check_code(code: str, problem: str, sample_data: str) -> str:
    """
    One repair pass over code the validator rejected.

    A validation failure is nearly always mechanical — a missing numpy import, a stray
    row loop, a truncated line — and making the agent spend a whole tool call to
    re-request the draft costs a Bedrock round-trip plus an agent turn to fix something
    the model can correct from the error message alone. This is that correction, inline.

    It is NOT part of the draft/test budget: it never runs the code and never touches the
    MAX_TEST_ITERATIONS counter. If the repair still fails validation the caller reports
    the original problem and the agent decides what to do, exactly as before.
    """
    system_prompt = """You are fixing Python code that failed static validation before it could run.

Fix ONLY the stated problem. Do not restructure the check, do not rename anything, do not
change what the rule tests, and do not "improve" anything you were not asked about — the
logic has not been run yet and may well be correct.

Keep all of this exactly as it is:
- the top-level function name `analyze_data`
- the return contract: {"passed": bool, "gaps": [{"description", "sheet", "row", "col", "value", "expected_value"}]}
- the DataFrames it reads: list_of_dfs / table_0, table_1, ... are globals; `pd` is pre-loaded, `np` is NOT

Return ONLY the corrected raw Python — no markdown fences, no explanation, no commentary."""

    user_message = f"""THE CODE THAT FAILED VALIDATION:
{code}

WHAT THE VALIDATOR REJECTED:
{problem}

The data it runs against:
{sample_data}

Return the corrected code now."""

    return _invoke_codegen(system_prompt, user_message)


def _validate_or_repair(code: str, sample_data: str) -> tuple:
    """
    Validate generated code, and spend one silent repair attempt if it fails.

    Returns (code, problem). `problem` is None when the code is usable — either because it
    validated first time or because the repair fixed it. When the repair does not help, the
    ORIGINAL problem is returned rather than the repaired one: the agent's next move should
    be driven by what the model got wrong to begin with, not by whatever the fix pass
    happened to break.
    """
    problem = _validate_check_code(code)
    if not problem:
        return code, None

    logger.info(f"Draft failed validation ({problem}) — attempting one inline repair")
    try:
        repaired = _fix_check_code(code, problem, sample_data)
    except Exception as e:
        logger.warning(f"Repair pass could not run: {e}")
        return code, problem

    if _validate_check_code(repaired):
        logger.info("Repair pass did not produce valid code; reporting the original problem")
        return code, problem

    logger.info("Repair pass succeeded")
    return repaired, None


_REVIEW_SYSTEM_PROMPT = """You are a senior QA engineer, and what you are senior at is one narrow,
high-stakes question:

    Was this control requirement, written in plain language, translated FAITHFULLY into this
    Python code?

Not "is the code good". Not "does it run" — it has already been run against the real file, so
execution is settled. What is NOT settled is whether the code does what the person asked for,
all of it, on the data it will actually see.

You are not the author, and you are not here to be agreeable. Your default position is that a
requirement is NOT implemented until you can quote the expression that implements it. Waving a
check through costs the user a control that silently passes a broken file; sending it back costs
one more iteration. Send it back.

WHAT YOU ARE GIVEN
  1. THE REQUEST — the user's own words, Hebrew or English. This is the spec.
  2. THE CODE — the finished analyze_data() written for it.
  3. THE DATA PROFILE — every column, its dtype, and the values that occur in it.
  4. THE FIRST ROWS OF THE REAL FILE — verbatim, exactly as analyze_data() receives them.

HOW TO REVIEW
Break the request into its separate testable requirements and rule on EACH one independently.
Work from the user's words, never from anyone's restatement of them: the single most common
failure you exist to catch is a requirement quietly dropped or narrowed on the way into code.
  • the user asked for two conditions and the code tests one
  • the user said "or" and the code wrote "and"
  • the user named a column and the code used a similar-sounding different one
  • the user asked about the whole sheet and the code checks a single row
  • the user asked for a comparison and the code only checks the value is present
A requirement counts as covered only when you can point at the line that implements it — not
when the code merely looks like it is in the right area. Where the request is genuinely
ambiguous, state which reading the code implements and flag the other; never silently pick one.

NOW USE THE ROWS. This is the step reviewers skip, and it is the step that finds real defects.
The profile tells you which values EXIST in a column. The rows tell you what a ROW looks like,
and most wrong checks are wrong about the shape of a row, not about the column list. Before you
write a verdict, execute the code in your head against those rows and answer concretely:
  • Pick a row this check SHOULD flag. Does this code flag it? Name the idx and the line.
  • Pick a row it should let through. Does this code leave it alone?
  • Do the values have the type the code assumes? A column the profile calls numeric that
    arrives as '1,234' or ' 1234 ' or '#' is the classic silent failure: pd.to_numeric yields
    NaN, every comparison against NaN is False, the check reports "passed", the bad file ships.
  • Are the sentinels visible in these rows ('#', '##', '-', '', None) handled the way the
    requirement needs? For a completeness rule they are MISSING, not values.
  • Do the rows contain anything the code treats as ordinary data but is not — a total or
    subtotal row, a repeated header, a blank separator, a footnote? A cross-total check that
    sums the total row into its own total is a real defect, and only the rows reveal it.
If a row contradicts an assumption in the code, that is a BLOCKING problem — and quote the
offending value in the issue, so the author can see the thing you saw.

ALSO CHECK, MECHANICALLY
  • every column name used exists EXACTLY as spelled in the profile (already whitespace-stripped)
  • a value-membership rule uses values that actually occur in that column
  • numeric text is cleaned — thousands separators, currency symbols, stray spaces — BEFORE
    pd.to_numeric, and float comparisons use a tolerance rather than exact equality
  • nothing is hardcoded that came from the sample: a row count, an id, a row position
  • a missing column, an empty sheet or a sheet that is not there produces a clear gap rather
    than an exception or a silent pass
  • every gap row carries the coordinates the UI needs: sheet, row, col, value, expected_value
  • the message on a gap is actionable to whoever has to fix the file

OUT OF SCOPE — do not raise these
Style, naming, comments, type hints, micro-performance, "this could be more readable".
Vectorised pandas that looks unusual is required here, not a defect.

Return ONLY a JSON object, no markdown fence and no prose around it:
{
  "verdict": "APPROVED" or "NEEDS_CHANGES",
  "requirements": [
    {"requirement": "<one testable thing the user asked for, in their terms>",
     "covered": true or false,
     "evidence": "<the line or expression that implements it, or why nothing does>"}
  ],
  "row_trace": "<what this code actually does with named rows from the sample: which idx it
                 flags and why, which idx it lets through and why. Concrete values from the
                 rows — not 'it works'. Say so plainly if no rows were supplied.>",
  "problems": [
    {"severity": "blocking" or "minor",
     "issue": "<what is wrong — quote the value or the line that shows it>",
     "fix": "<the specific change that would fix it>"}
  ],
  "summary": "<two sentences at most>"
}

Use "NEEDS_CHANGES" if ANY requirement is uncovered, ANY problem is blocking, or the rows show
the code doing the wrong thing. Use "APPROVED" only when every requirement is covered and
nothing blocking remains; minor problems may stand alongside APPROVED."""


def _code_fingerprint(code: str) -> str:
    """Identify the exact code a review was written about."""
    return hashlib.sha256((code or '').encode('utf-8')).hexdigest()[:16]


def _stored_review_for(file_type: str, code: str) -> Optional[dict]:
    """
    The stored review, but ONLY if it was written about this exact code.

    The review file is per-file-type, so without the fingerprint check an approval of the
    previous draft would be attached to the next check saved — an approval nobody gave, on
    code nobody read. A mismatch is treated as no review at all.
    """
    review = _read_json(file_type, REVIEW_FILE)
    if not review:
        return None
    if review.get('code_fingerprint') != _code_fingerprint(code):
        logger.info("Stored review does not match the code being saved — ignoring it")
        return None
    return review


def _review_summary_for(file_type: str, code: str) -> Optional[dict]:
    """The compact record of the review stored alongside a saved check. None when unreviewed."""
    review = _stored_review_for(file_type, code)
    if not review:
        return None
    reqs = review.get('requirements') or []
    problems = review.get('problems') or []
    return {
        "verdict": review.get('verdict'),
        "reviewed_at": review.get('reviewed_at'),
        "model": review.get('model'),
        "original_request": review.get('original_request'),
        "summary": review.get('summary'),
        "requirements_total": len(reqs),
        "requirements_uncovered": [
            r.get('requirement') for r in reqs if not r.get('covered')
        ],
        "blocking_problems": [
            p.get('issue') for p in problems
            if str(p.get('severity', '')).lower() == 'blocking'
        ],
    }


def _parse_review(raw: str) -> Optional[dict]:
    """
    Pull the verdict object out of the model's reply.

    Tolerant on purpose: the prompt asks for bare JSON, but a fenced block or a sentence of
    preamble is a presentation slip, not a review failure, and discarding a good review over
    it would cost a whole extra model call.
    """
    if not raw or not raw.strip():
        return None

    text = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    text = re.sub(r'\s*```$', '', text).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost {...} span.
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _render_review(review: dict) -> str:
    """Render a verdict for the agent, leading with what is missing rather than what passed."""
    verdict = str(review.get('verdict', '')).upper()
    reqs = review.get('requirements') or []
    problems = review.get('problems') or []

    uncovered = [r for r in reqs if not r.get('covered')]
    blocking = [p for p in problems if str(p.get('severity', '')).lower() == 'blocking']
    minor = [p for p in problems if str(p.get('severity', '')).lower() != 'blocking']

    head = "✅ REVIEW: APPROVED" if verdict == 'APPROVED' else "⚠️ REVIEW: NEEDS CHANGES"
    lines = [
        head,
        f"{len(reqs) - len(uncovered)} of {len(reqs)} requirement(s) covered · "
        f"{len(blocking)} blocking · {len(minor)} minor",
    ]
    if review.get('summary'):
        lines += ["", str(review['summary'])]

    # Shown even on APPROVED: it is the only part of the verdict that says what the code does
    # to actual rows, so it is also how the agent notices a reviewer that never looked.
    if review.get('row_trace'):
        lines += ["", "ON THE REAL ROWS:", f"  {review['row_trace']}"]

    if uncovered:
        lines += ["", "REQUIREMENTS NOT COVERED — these are what the user asked for and did not get:"]
        for r in uncovered:
            lines.append(f"  ✗ {r.get('requirement', '?')}")
            if r.get('evidence'):
                lines.append(f"      {r['evidence']}")

    if blocking:
        lines += ["", "BLOCKING PROBLEMS:"]
        for p in blocking:
            lines.append(f"  • {p.get('issue', '?')}")
            if p.get('fix'):
                lines.append(f"      fix: {p['fix']}")

    if minor:
        lines += ["", "MINOR (does not block saving):"]
        for p in minor:
            lines.append(f"  • {p.get('issue', '?')}")

    covered = [r for r in reqs if r.get('covered')]
    if covered:
        lines += ["", "Covered:"]
        for r in covered:
            lines.append(f"  ✓ {r.get('requirement', '?')}")

    if verdict == 'APPROVED':
        lines += ["", "NEXT: call save_draft_check() with iterations_run set."]
    else:
        lines += [
            "",
            "NEXT: call draft_check_code() (or regenerate_check() for a saved check) with "
            "feedback= quoting the uncovered requirements and blocking problems above, then "
            "test again. Do NOT save until this comes back APPROVED, unless you have run out "
            "of iterations — in which case save with known_limitation= naming what is still wrong.",
        ]
    return "\n".join(lines)


# ============== TOOLS ==============

@tool
def list_file_types() -> str:
    """List all configured CSV/XLSX file types stored in S3."""
    prefix = S3_CHECKS_PREFIX
    keys = _list_keys(prefix)
    types = set()
    for k in keys:
        rel = k[len(prefix):]
        parts = rel.split('/')
        if len(parts) >= 2 and parts[0]:
            types.add(parts[0])
    if not types:
        return "No file types configured yet."
    result = []
    for ft in sorted(types):
        meta = _read_json(ft, 'metadata.json')
        desc = meta.get('description', '') if meta else ''
        result.append(f"• {ft}: {desc}")
    return "Configured file types:\n" + "\n".join(result)


@tool
def create_file_type(file_type: str, description: str, expected_columns: str) -> str:
    """
    Create a new file type configuration.

    Args:
        file_type: Short identifier for the file type (e.g. 'premiums', 'claims')
        description: Human-readable description of what this sheet contains
        expected_columns: Comma-separated list of expected column names
    """
    file_type = file_type.strip().lower().replace(' ', '_')
    existing = _read_json(file_type, 'metadata.json')
    if existing:
        return f"File type '{file_type}' already exists. Use list_file_types() to see it."

    cols = [c.strip() for c in expected_columns.split(',') if c.strip()]
    metadata = {
        "file_type": file_type,
        "description": description,
        "expected_columns": cols,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    if _write_json(file_type, 'metadata.json', metadata):
        return f"Created file type '{file_type}' with {len(cols)} expected columns."
    return f"Failed to create file type '{file_type}'."


@tool
def add_extraction_field(file_type: str, field_name: str, question: str, description: str) -> str:
    """
    Add an extraction field for a file type. These questions will be sent to
    lambda_answer_by_rag_for_agent to extract structured data from uploaded files.

    Args:
        file_type: The file type identifier
        field_name: Short machine-readable name (e.g. 'total_premiums')
        question: The question to ask the RAG system (e.g. 'What is the total sum of all premiums?')
        description: Human-readable description of this field
    """
    existing = _read_json(file_type, 'extraction_fields.json') or []
    if not isinstance(existing, list):
        existing = []

    for f in existing:
        if f.get('field_name') == field_name:
            return f"Field '{field_name}' already exists for '{file_type}'. Delete it first to update."

    existing.append({
        "field_name": field_name,
        "question": question,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    if _write_json(file_type, 'extraction_fields.json', existing):
        return f"Added extraction field '{field_name}' to '{file_type}'. Total fields: {len(existing)}."
    return f"Failed to add extraction field '{field_name}'."


@tool
def list_extraction_fields(file_type: str) -> str:
    """List all extraction fields configured for a file type."""
    fields = _read_json(file_type, 'extraction_fields.json')
    if not fields:
        return f"No extraction fields configured for '{file_type}'."
    lines = [f"Extraction fields for '{file_type}':"]
    for f in fields:
        lines.append(f"  • {f['field_name']}: {f['question']}")
    return "\n".join(lines)


@tool
def delete_extraction_field(file_type: str, field_name: str) -> str:
    """Remove an extraction field from a file type."""
    fields = _read_json(file_type, 'extraction_fields.json') or []
    new_fields = [f for f in fields if f.get('field_name') != field_name]
    if len(new_fields) == len(fields):
        return f"Field '{field_name}' not found in '{file_type}'."
    if _write_json(file_type, 'extraction_fields.json', new_fields):
        return f"Deleted field '{field_name}' from '{file_type}'."
    return "Delete failed."


@tool
def draft_check_code(
    file_type: str,
    check_name: str,
    check_description: str,
    check_type: str,
    accounting_context_hint: str = "",
    feedback: str = "",
) -> str:
    """
    Write (or rewrite) the Python for a check and hold it as a draft. Saves nothing
    permanent — call test_draft_check() next, and save_draft_check() only once the result
    looks right.

    Args:
        file_type: The file type identifier (e.g. 'premiums')
        check_name: Short name for this check (e.g. 'total_premium_cross_total')
        check_description: Detailed description of what the rule must catch
        check_type: One of: cross_total, completeness, cross_sheet, format, outlier
        accounting_context_hint: Brief accounting context used when explaining gaps to users
        feedback: What was wrong with the previous draft and how to fix it. Leave empty on
            the first attempt. On a retry ALWAYS fill this in with what the test run showed
            — name the specific rows and values from the samples that were handled wrongly.
            A retry without concrete feedback tends to reproduce the same bug.
    """
    valid_types = {'cross_total', 'completeness', 'cross_sheet', 'format', 'outlier'}
    check_type = (check_type or '').lower()
    if check_type not in valid_types:
        return f"Invalid check_type '{check_type}'. Use: {', '.join(valid_types)}"

    meta = _read_json(file_type, 'metadata.json')
    if not meta:
        return f"File type '{file_type}' not found. Create it first with create_file_type()."

    # A revision gets the same full profile the first attempt did, plus the previous code
    # and the critique. Giving a fix pass less context than the original attempt is how a
    # revision ends up blinder than the thing it is revising.
    resolved_sample = _profile_or_hint(file_type)
    description = check_description
    previous = _read_text(file_type, DRAFT_CODE_FILE)
    if feedback.strip():
        description = (
            f"{check_description}\n\n"
            f"PREVIOUS CODE (this is what needs fixing):\n{previous or '(none)'}\n\n"
            f"WHAT THE TEST RUN SHOWED — what is wrong and how to fix it:\n{feedback}"
        )

    try:
        code = _generate_check_code(
            check_name=check_name,
            check_description=description,
            check_type=check_type,
            sample_data=resolved_sample,
            accounting_context_hint=accounting_context_hint,
        )
    except Exception as e:
        return f"Code generation failed: {e}. The previous draft is unchanged."

    code, problem = _validate_or_repair(code, resolved_sample)
    if problem:
        return (
            f"❌ The generated code is invalid and was NOT stored: {problem}\n\n"
            f"An automatic repair pass was already tried and did not fix it, so re-requesting "
            f"the same thing will not help. Call draft_check_code() again with feedback= naming "
            f"this specific problem, and make sure it defines a top-level `analyze_data()` function."
        )

    if not _write_text(file_type, DRAFT_CODE_FILE, code):
        return "Failed to store the draft code in S3."
    _write_json(file_type, DRAFT_META_FILE, {
        "file_type": file_type,
        "name": check_name,
        "description": check_description,
        "check_type": check_type,
        "accounting_context_hint": accounting_context_hint,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return (
        f"Draft written for '{check_name}' ({len(code.splitlines())} lines).\n\n"
        f"```python\n{code}\n```\n\n"
        f"NEXT: call test_draft_check('{file_type}') to run it against the loaded data file."
    )


@tool
def test_draft_check(file_type: str, iteration: int = 1) -> str:
    """
    Run the current draft against the real data file the user loaded, and report what it
    did — including a random sample of rows it FLAGGED and rows it LET THROUGH.

    Read both samples before deciding the check is correct. The flagged sample exposes
    false positives. The passed sample exposes false negatives, which is the failure that
    matters: a check whose comparison matches nothing reports "passed" and looks identical
    to a clean file.

    Args:
        file_type: The file type identifier
        iteration: Which attempt this is (1, 2, 3...). Different values sample different
            rows, so later attempts widen coverage instead of re-reading the same ten rows.
    """
    code = _read_text(file_type, DRAFT_CODE_FILE)
    if not code:
        return (
            f"No draft exists for '{file_type}'. Call draft_check_code() first."
        )

    profile = _load_file_profile(file_type)
    if not profile:
        return (
            f"No data profile stored for '{file_type}', so there is no file to test against. "
            f"Ask the user to load the CSV/XLSX file in step 2 of the wizard."
        )
    s3_uri = profile.get('source_s3_path', '')
    if not s3_uri:
        return (
            f"The stored profile for '{file_type}' has no source_s3_path, so the data file "
            f"cannot be located. Ask the user to re-load the file in step 2."
        )

    try:
        seed = int(iteration)
    except (TypeError, ValueError):
        seed = 1

    logger.info(f"Testing draft for '{file_type}' against {s3_uri} (iteration {seed})")
    result = _invoke_executor_test(file_type, code, s3_uri, seed)

    header = f"TEST RUN {seed} of at most {MAX_TEST_ITERATIONS} · file type '{file_type}' · {s3_uri}\n"
    if seed >= MAX_TEST_ITERATIONS:
        header += (
            f"⚠️ This is the last iteration you may spend. If the result is still wrong, "
            f"save it anyway with save_draft_check() and tell the user plainly what is "
            f"still not right.\n"
        )
    return header + "\n" + _render_test_result(result)


@tool
def review_check_code(file_type: str, original_request: str, check_id: str = "") -> str:
    """
    Have an INDEPENDENT model read the finished code back against what the user asked for,
    and rule on whether every requirement is actually implemented.

    Call this after test_draft_check() looks right and BEFORE save_draft_check(). It answers
    the question the test run cannot: the test shows what the code DID, this shows whether
    that is what was ASKED FOR. A check that runs cleanly while testing only half the stated
    rule passes every test and is still wrong.

    The reviewer reads as a senior QA engineer, and is given the first rows of the real file
    alongside the code, so its verdict includes a trace of what this code would do to named
    rows — which is how row-shape mistakes (a total row counted as detail, '1,234' as text,
    a '#' sentinel silently becoming NaN) get caught before the check is saved.

    Args:
        file_type: The file type identifier
        original_request: The user's request in THEIR OWN WORDS, quoted as closely as you can
            from what they typed — including any part you decided not to implement. Do not
            paste your own restatement of it: the review exists to catch the difference
            between the two, so a restatement makes it check your summary against itself and
            it will approve everything.
        check_id: Leave empty to review the current draft. Pass a check id (e.g. 'check_003')
            to review an already-saved check instead.
    """
    if not (original_request or '').strip():
        return (
            "original_request is empty. Pass what the user actually asked for, in their words "
            "— the review has nothing to compare the code against without it."
        )

    if check_id:
        check = _read_json(file_type, f"{check_id}.json")
        if not check:
            return f"Check '{check_id}' not found for file type '{file_type}'."
        code = check.get('code') or _read_text(file_type, f"{check_id}.py")
        label = check_id
    else:
        code = _read_text(file_type, DRAFT_CODE_FILE)
        label = 'the current draft'

    if not code:
        return (
            f"No code found to review for '{file_type}'"
            f"{f' / {check_id}' if check_id else ''}. Call draft_check_code() first."
        )

    # One read, two uses: the column summary the codegen prompt also gets, plus the raw rows
    # underneath it. _profile_or_hint() is still the fallback — its "NO DATA PROFILE" text is
    # what stops the reviewer assuming a file it was never shown.
    profile = _load_file_profile(file_type)
    data_description = _render_profile_for_codegen(profile) if profile else _profile_or_hint(file_type)
    sample_rows = _render_sample_rows(profile) if profile else ""
    if not sample_rows:
        sample_rows = (
            "NO SAMPLE ROWS ARE STORED for this file type, so you cannot trace the code against "
            "real data. Say that in row_trace rather than assuming the file is well formed, and "
            "treat any assumption the code makes about row shape as unverified."
        )

    user_message = f"""THE USER'S ORIGINAL REQUEST (their words — this is the spec):
{original_request}

THE CODE THAT WAS WRITTEN FOR IT:
{code}

THE DATA IT RUNS AGAINST:
{data_description}

{sample_rows}

Review the code against the request and return your JSON verdict."""

    try:
        raw = _invoke_bedrock_text(
            model_id=REVIEW_MODEL_ID,
            system_prompt=_REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            max_tokens=REVIEW_MAX_TOKENS,
            thinking=False,
            label="Review",
        )
    except Exception as e:
        # A reviewer that cannot run must not read as an approval, and must not kill the turn
        # either — the agent still has a tested draft it can save with a limitation noted.
        logger.error(f"Review call failed for '{file_type}': {e}")
        return (
            f"⚠️ The review could not run: {e}\n\n"
            f"This is NOT an approval. Either retry review_check_code(), or save with "
            f"known_limitation= recording that the code was never reviewed against the request."
        )

    review = _parse_review(raw)
    if not review:
        logger.warning(f"Could not parse review output for '{file_type}': {raw[:400]}")
        return (
            "⚠️ The reviewer did not return a readable verdict. Treat this as NOT reviewed. "
            "Its raw reply was:\n\n" + raw[:2000]
        )

    # Persist it so save_draft_check can record what the review said, and so the verdict
    # survives to the saved check rather than living only in this turn's transcript.
    review['reviewed_check'] = check_id or 'draft'
    review['original_request'] = original_request
    review['reviewed_at'] = datetime.now(timezone.utc).isoformat()
    review['model'] = REVIEW_MODEL_ID
    review['code_fingerprint'] = _code_fingerprint(code)
    _write_json(file_type, REVIEW_FILE, review)

    logger.info(f"Review of {label} for '{file_type}': {review.get('verdict')}")
    return _render_review(review)


@tool
def save_draft_check(
    file_type: str,
    check_name: str,
    check_description: str,
    check_type: str,
    severity: str,
    expected_owner: str,
    accounting_context_hint: str,
    iterations_run: int = 0,
    known_limitation: str = "",
) -> str:
    """
    Promote the current draft to a real check with status='draft', ready for the user to
    test in the UI and approve. Call this only after test_draft_check() has run at least
    once.

    Args:
        file_type: The file type identifier
        check_name: Short name for this check
        check_description: What the check verifies
        check_type: One of: cross_total, completeness, cross_sheet, format, outlier
        severity: One of: critical, warning, info
        expected_owner: Team responsible for fixing gaps (e.g. 'IFRS17 Team')
        accounting_context_hint: Brief accounting context used when explaining gaps
        iterations_run: How many test runs you spent getting here
        known_limitation: If you are saving something you are NOT confident in, say what is
            still wrong. This is stored with the check and shown to the user — leave empty
            only when the test result genuinely looked correct.
    """
    valid_severities = {'critical', 'warning', 'info'}
    valid_types = {'cross_total', 'completeness', 'cross_sheet', 'format', 'outlier'}

    severity = (severity or '').lower()
    check_type = (check_type or '').lower()
    if severity not in valid_severities:
        return f"Invalid severity '{severity}'. Use: {', '.join(valid_severities)}"
    if check_type not in valid_types:
        return f"Invalid check_type '{check_type}'. Use: {', '.join(valid_types)}"

    code = _read_text(file_type, DRAFT_CODE_FILE)
    if not code:
        return f"No draft exists for '{file_type}'. Call draft_check_code() first."

    # Plain validation on purpose — do NOT change this to _validate_or_repair(). The draft
    # being saved here is the exact text that test_draft_check() ran; repairing it at the
    # save gate would store code that has never been executed, which is precisely what
    # "never save a check you have not run" exists to prevent. Repair belongs at draft time.
    problem = _validate_check_code(code)
    if problem:
        return f"❌ The draft is invalid and was NOT saved: {problem}"

    check_id = _next_check_id(file_type)
    check_data = {
        "id": check_id,
        "name": check_name,
        "description": check_description,
        "check_type": check_type,
        "severity": severity,
        "expected_owner": expected_owner,
        "accounting_context_hint": accounting_context_hint,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        # What the agent actually did before saving, so a reviewer can tell a check that
        # was verified against data apart from one that was written and stored blind.
        "verification": {
            "iterations_run": int(iterations_run or 0),
            "tested_against": (_load_file_profile(file_type) or {}).get('source_s3_path', ''),
            "known_limitation": known_limitation,
            # Folded in automatically, and only when review_check_code() actually read THIS
            # code (see _stored_review_for). Absent means unreviewed — never assume approval.
            "review": _review_summary_for(file_type, code),
        },
        "code": code,
    }

    if not _write_json(file_type, f"{check_id}.json", check_data):
        return "Failed to save check JSON."
    if not _write_text(file_type, f"{check_id}.py", code):
        logger.warning(f"Could not write standalone .py for {check_id} — JSON code field is the fallback")

    _save_check_version(file_type, check_id, check_data)
    _clear_draft(file_type)

    caveat = f"\n\n⚠️ Known limitation recorded: {known_limitation}" if known_limitation else ""

    # Say plainly whether this code was reviewed against the request. Silence here reads as
    # "reviewed and fine", which is the one thing an unreviewed check must not look like.
    review = check_data["verification"]["review"]
    if not review:
        review_line = (
            "\n\n⚠️ NOT reviewed against the original request — review_check_code() was not run "
            "on this exact code. Tell the user that."
        )
    elif str(review.get('verdict', '')).upper() == 'APPROVED':
        review_line = "\n\nReview: APPROVED against the original request."
    else:
        missing = review.get('requirements_uncovered') or []
        blocking = review.get('blocking_problems') or []
        detail = "; ".join(str(x) for x in (missing + blocking)[:3]) or "see the review output"
        review_line = f"\n\n⚠️ Saved despite review verdict NEEDS_CHANGES — outstanding: {detail}"

    return (
        f"✅ Check '{check_name}' saved as **{check_id}** (status=draft) for file type '{file_type}', "
        f"after {iterations_run} test run(s) against real data.{caveat}{review_line}\n\n"
        f"NEXT STEP: the user should run 'Test This Check' in the UI to see the result for "
        f"themselves, then approve it."
    )


@tool
def approve_check(file_type: str, check_id: str) -> str:
    """
    Mark a check as 'active' after the user has tested and confirmed it works correctly.
    Only active checks are run during Phase 2 (Upload & Run).

    Args:
        file_type: The file type identifier
        check_id: The check ID to approve (e.g. 'check_001')
    """
    check = _read_json(file_type, f"{check_id}.json")
    if not check:
        return f"Check '{check_id}' not found in '{file_type}'."
    check['status'] = 'active'
    check['approved_at'] = datetime.now(timezone.utc).isoformat()
    if _write_json(file_type, f"{check_id}.json", check):
        _save_check_version(file_type, check_id, check)
        return f"Check '{check_id}' ({check['name']}) is now ACTIVE and will run in Phase 2."
    return "Failed to approve check."


@tool
def regenerate_check(file_type: str, check_id: str, feedback: str) -> str:
    """
    Regenerate the Python code for a check based on user feedback.
    The check status is reset to 'draft'.

    Args:
        file_type: The file type identifier
        check_id: The check ID to fix (e.g. 'check_001')
        feedback: Description of what was wrong and how to fix it
    """
    check = _read_json(file_type, f"{check_id}.json")
    if not check:
        return f"Check '{check_id}' not found in '{file_type}'."

    # Regeneration gets the same full profile the original generation did. It previously
    # got only a column-name string, which made every revision blinder than the first
    # attempt — the opposite of what a fix pass needs.
    resolved_sample = _profile_or_hint(file_type)

    try:
        enhanced_desc = (
            f"{check['description']}\n\n"
            f"PREVIOUS CODE (this is what needs fixing):\n{check.get('code', '(none)')}\n\n"
            f"USER FEEDBACK — what is wrong and how to fix it:\n{feedback}"
        )
        code = _generate_check_code(
            check_name=check['name'],
            check_description=enhanced_desc,
            check_type=check['check_type'],
            sample_data=resolved_sample,
            accounting_context_hint=check['accounting_context_hint'],
        )
    except Exception as e:
        return f"Code regeneration failed: {e}"

    code, problem = _validate_or_repair(code, resolved_sample)
    if problem:
        return (
            f"❌ Regenerated code for '{check_id}' is invalid and was NOT saved: {problem}\n\n"
            f"An automatic repair pass was already tried and did not fix it. The previous version "
            f"is untouched. Call regenerate_check() again with feedback that names this problem."
        )

    check['code'] = code
    check['status'] = 'draft'
    check['updated_at'] = datetime.now(timezone.utc).isoformat()
    # A regenerated check has not been re-verified yet — say so rather than carrying the
    # old run's verification forward onto code that has never been executed.
    check['verification'] = {
        "iterations_run": 0,
        "tested_against": "",
        "known_limitation": "regenerated from user feedback; not yet re-tested against data",
    }

    if not _write_json(file_type, f"{check_id}.json", check):
        return "Failed to save regenerated check JSON."

    # Update the standalone .py file so the executor picks up the latest version
    if not _write_text(file_type, f"{check_id}.py", code):
        logger.warning(f"Could not update standalone .py for {check_id}")

    # Mirror into the draft slot so the new code can go straight through the same
    # test loop as a fresh check, instead of only being testable by hand in the UI.
    _write_text(file_type, DRAFT_CODE_FILE, code)

    _save_check_version(file_type, check_id, check)

    return (
        f"✅ Check '{check_id}' code regenerated (status reset to draft).\n"
        f"Both `{check_id}.json` and `{check_id}.py` have been updated in S3.\n\n"
        f"NEXT: call test_draft_check('{file_type}') to run the new code against the data "
        f"before telling the user it is fixed. Do not claim it works until it has run."
    )


@tool
def get_file_profile(file_type: str) -> str:
    """
    Read the profile of the data file the user loaded for this file type.

    Returns the exact column names, dtypes, null counts and most common values per
    column, plus sample rows. This is the authoritative description of what
    analyze_data() will receive — always consult it instead of asking the user for
    column names or sample rows.

    Args:
        file_type: The file type identifier (e.g. 'premiums')
    """
    profile = _load_file_profile(file_type)
    if not profile:
        return (
            f"No data profile stored for '{file_type}'. The user has not loaded a data file yet — "
            f"ask them to load the CSV/XLSX file in step 2 of the wizard."
        )
    return _render_profile_for_codegen(profile, max_sample_rows=5)


@tool
def get_column_values(file_type: str, column: str, limit: int = 25) -> str:
    """
    Show the most common values of one column, with counts.

    Use this to confirm a column's real domain before writing an allowed-value or
    tag-pattern rule — for example to see that a tagging column holds '5030'/'3030'/'#'
    rather than guessing the format.

    Args:
        file_type: The file type identifier
        column: Exact column name as it appears in the profile
        limit: How many distinct values to return (default 25)
    """
    profile = _load_file_profile(file_type)
    if not profile:
        return f"No data profile stored for '{file_type}'. Ask the user to load a data file first."

    for sheet in profile.get('sheets', []):
        for col in sheet.get('columns', []):
            if str(col.get('name')) == column:
                tops = (col.get('top_values') or [])[:limit]
                lines = [
                    f"{sheet.get('name')} · {column!r}  "
                    f"dtype={col.get('dtype')}  nulls={col.get('null_count')}  "
                    f"distinct={col.get('n_distinct')}",
                ]
                lines += [f"  {t.get('value')!r} — {t.get('count')} rows" for t in tops]
                if col.get('n_distinct', 0) > len(tops):
                    lines.append(
                        f"  ...only the {len(tops)} most common of "
                        f"{col.get('n_distinct')} distinct values are recorded."
                    )
                return "\n".join(lines)

    available = [
        str(c.get('name'))
        for s in profile.get('sheets', [])
        for c in s.get('columns', [])
    ]
    return (
        f"Column {column!r} is not in the profile. Available columns:\n  "
        + "\n  ".join(repr(a) for a in available)
    )


@tool
def list_checks(file_type: str) -> str:
    """List all checks configured for a file type, with their status and severity."""
    prefix = f"{S3_CHECKS_PREFIX}{file_type}/check_"
    keys = [k for k in _list_keys(prefix) if k.endswith('.json')]
    if not keys:
        return f"No checks configured for '{file_type}'."

    lines = [f"Checks for '{file_type}':"]
    for key in sorted(keys):
        filename = key.split('/')[-1]
        check = _read_json(file_type, filename)
        if check:
            status_icon = "✅" if check['status'] == 'active' else "⏳"
            sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(check['severity'], "⚪")
            lines.append(
                f"  {status_icon} {sev_icon} {check['id']} | {check['name']} | "
                f"{check['check_type']} | {check['status']}"
            )
    return "\n".join(lines)


@tool
def get_check(file_type: str, check_id: str) -> str:
    """Read a specific check's full details including the generated Python code."""
    check = _read_json(file_type, f"{check_id}.json")
    if not check:
        return f"Check '{check_id}' not found in '{file_type}'."
    display = {k: v for k, v in check.items() if k != 'code'}
    result = json.dumps(display, indent=2, ensure_ascii=False)
    result += f"\n\n--- CODE ---\n{check.get('code', 'No code found')}"
    return result


@tool
def delete_check(file_type: str, check_id: str) -> str:
    """Delete a check from S3 — both its .json definition and its .py code file."""
    keys = [_s3_key(file_type, f"{check_id}.json"), _s3_key(file_type, f"{check_id}.py")]
    deleted, errors = _delete_keys(keys)
    if errors:
        return f"Delete of '{check_id}' partially failed: {'; '.join(errors)}"
    return f"Deleted check '{check_id}' from '{file_type}' ({len(deleted)} file(s) removed)."


@tool
def delete_file_type(file_type: str, dry_run: bool = True) -> str:
    """
    Delete an ENTIRE file type and every file stored under it in S3. IRREVERSIBLE.

    ⚠️ ALWAYS call this with dry_run=True first, show the user the full file list it
       returns, and only call it again with dry_run=False after the user has explicitly
       confirmed. There is no undo and no backup.

    Removes everything under csv_checks/{file_type}/ :
    - metadata.json (the file type itself)
    - extraction_fields.json (all extraction fields)
    - every check_NNN.json AND its matching check_NNN.py

    After a real delete the file type stops appearing in list_file_types() and in the UI
    dropdown, because a file type exists only as long as it has files in S3.

    To delete a SINGLE check instead of the whole file type, use delete_check().

    Args:
        file_type: The file type identifier to delete (e.g. 'premiums'). Spaces and case
                   are normalised exactly as create_file_type() normalises them.
        dry_run: If True (default), NOTHING is deleted — only the list of files that
                 would be deleted is returned. Set to False only after the user has seen
                 that list and explicitly confirmed the deletion.
    """
    # A model that emits the string "False" must not silently get a real delete, and one
    # that emits "false" must not silently get a no-op it reports as done.
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ('false', 'no', '0', 'f', 'n')

    file_type = _normalize_file_type(file_type)
    if not file_type or not _FILE_TYPE_RE.match(file_type):
        return (
            f"❌ Refusing to delete: '{file_type}' is not a valid file type identifier. "
            f"Expected a single name like 'premiums' — no slashes, no wildcards, not empty."
        )

    prefix = f"{S3_CHECKS_PREFIX}{file_type}/"

    try:
        keys = _list_keys(prefix, strict=True)
    except Exception as e:
        return f"❌ Could not list s3://{S3_BUCKET}/{prefix} — nothing was deleted: {e}"

    if not keys:
        return (
            f"File type '{file_type}' does not exist — there are no objects under "
            f"s3://{S3_BUCKET}/{prefix}, so there is nothing to delete. "
            f"Call list_file_types() to check the exact spelling."
        )

    names = sorted(k[len(prefix):] for k in keys)
    check_ids = sorted({
        m.group(1) for m in (re.match(r'(check_\d+)\.(?:json|py)$', n) for n in names) if m
    })
    listing = "\n".join(f"  • {n}" for n in names)
    checks_note = ', '.join(check_ids) if check_ids else 'none'

    if dry_run:
        return (
            f"🔍 DRY RUN — nothing has been deleted.\n\n"
            f"Deleting file type '{file_type}' would permanently remove {len(keys)} file(s) "
            f"from s3://{S3_BUCKET}/{prefix}\n"
            f"Checks affected ({len(check_ids)}): {checks_note}\n\n"
            f"{listing}\n\n"
            f"This cannot be undone. Show this list to the user, get an explicit 'yes', "
            f"then call delete_file_type('{file_type}', dry_run=False)."
        )

    deleted, errors = _delete_keys(keys)

    if errors:
        shown = "\n".join(f"  • {e}" for e in errors[:20])
        more = f"\n  … and {len(errors) - 20} more" if len(errors) > 20 else ""
        return (
            f"⚠️ PARTIAL DELETE of file type '{file_type}': {len(deleted)} of {len(keys)} "
            f"file(s) removed. These could not be deleted:\n{shown}{more}\n\n"
            f"'{file_type}' may still appear in list_file_types(). "
            f"Retry with delete_file_type('{file_type}', dry_run=False)."
        )

    leftover = _list_keys(prefix)
    if leftover:
        return (
            f"⚠️ Deleted {len(deleted)} file(s) for '{file_type}', but {len(leftover)} object(s) "
            f"still exist under s3://{S3_BUCKET}/{prefix} (something wrote to it during the "
            f"delete). The file type will still show in list_file_types(). Run "
            f"delete_file_type('{file_type}', dry_run=True) again to see what remains."
        )

    logger.info(f"Deleted file type '{file_type}': {len(deleted)} object(s) under {prefix}")
    return (
        f"✅ Deleted file type '{file_type}' — {len(deleted)} file(s) removed from "
        f"s3://{S3_BUCKET}/{prefix}, including {len(check_ids)} check(s) ({checks_note}).\n"
        f"It no longer appears in list_file_types() or in the UI file-type dropdown."
    )


# ============== SYSTEM PROMPT ==============

SYSTEM_PROMPT = """You are the CSV Checks Config Agent for an IFRS 17 control sheet validation system (Phoenix insurance company).

Your job is to help users configure two things for each CSV/XLSX file type (e.g. premiums, claims, income, expenses):

1. EXTRACTION FIELDS — structured data fields to extract from each uploaded file (e.g. total premiums, report date, client name)
2. CONTROL CHECKS — deterministic Python validation rules that run on every uploaded file of this type

HOW THE UI FEEDS YOU CONTEXT:
The user works through a wizard. By the time they ask you for a check they have already
(1) chosen the file type and (2) loaded the actual CSV/XLSX data file. Both facts are
handed to you in a "## CONTEXT" block at the top of the user's message, and the loaded
file has been profiled into S3 — exact column names, dtypes, null counts, each column's
most common values, and sample rows.

That means the answers to "which file type?", "what is the column called?" and "can you
paste some rows?" are ALREADY IN YOUR HANDS. Asking for them again is the single worst
thing you can do here — it stalls the user on information they already provided.

WORKFLOW FOR CREATING A CHECK — WRITE, RUN, INSPECT, FIX, THEN SAVE:

You do not hand the user untested code. You run every check against their real data
yourself and read the result before saving it. The loop is:

1. Read the ## CONTEXT block for the file type and the data profile.
   Call get_file_profile() for the full column list, or get_column_values() to confirm one
   column's real domain before writing a rule about it.
2. Map the user's requirement onto the actual column names from the profile. Match on
   meaning, not exact spelling — the user may name a column loosely or in another language.
3. Pick sensible defaults yourself: infer check_type from the rule, default severity to
   'critical' for a tagging/completeness rule that breaks reporting and 'warning'
   otherwise, and default expected_owner to 'IFRS17 Team'. State the defaults you chose in
   one short line so the user can correct them — do not ask first.
4. draft_check_code(...) — writes the Python and holds it as a draft. Nothing is saved yet.
5. test_draft_check(file_type, iteration=N) — runs it against the loaded file and returns
   the verdict, the gaps, and TWO random row samples.
6. READ BOTH SAMPLES BEFORE YOU DECIDE ANYTHING. This is the whole point of the loop:
   - The FLAGGED sample shows rows the check caught. Any row there that is actually fine
     is a false positive.
   - The PASSED sample shows rows the check let through. THIS IS WHERE THE BUG USUALLY IS.
     Read every row and ask: should the rule I was asked for have caught this one?
   - "passed: True with 0 gaps" is the most dangerous result, not the best one. It means
     either the file is clean or your comparison matched nothing. The passed sample is the
     only way to tell those apart. A row holding '#', '', '##', or a value outside the
     allowed set sitting in the passed sample means the check is BROKEN — most often
     because the column is dtype object and an `== 1` or a numeric comparison silently
     matched nothing.
7. If anything is wrong, call draft_check_code() again with feedback= describing exactly
   what you saw — quote the offending rows and values from the sample. Then test again with
   iteration incremented, which samples different rows.
8. Spend at most 4 test runs. If it is still not right at the 4th, save anyway with
   save_draft_check(known_limitation="...") stating plainly what is still wrong, and tell
   the user in your reply. Do not loop forever and do not silently give up.
9. review_check_code(file_type, original_request=...) — an independent model reads the
   finished code back against what the user asked for. Pass the user's OWN WORDS, quoted as
   closely as you can from what they typed, INCLUDING any part you chose not to implement.
   Do not pass your own restatement: the review exists to catch the gap between what was
   asked and what was built, and feeding it your summary makes it check your summary against
   itself. If the verdict is NEEDS_CHANGES, go back to step 7 with its findings as feedback.
   The test run tells you what the code DID; this tells you whether that is what was ASKED
   FOR. A check that tests only half the stated rule passes every test run and is still wrong.
10. save_draft_check(...) with iterations_run set to how many test runs you spent.
11. Report the check_id and, in one or two sentences, what the test run actually showed —
    how many rows were flagged out of how many scanned — plus the review verdict. The user
    then runs 'Test This Check' in the UI themselves and approves it.

To fix an already-saved check: regenerate_check() then test_draft_check() then
review_check_code(check_id="check_00X") — same rules, never claim it is fixed until it has
run, and never claim it matches the request until the review says so.

Only stop and ask when the requirement itself is genuinely ambiguous — for example when
two different columns could plausibly be the subject of the rule and the choice changes
the result. Never stop to ask for something the profile already answers.

WORKFLOW FOR A BRAND-NEW FILE TYPE:
1. Ask for the file type name and a brief description (this you genuinely do not know)
2. Call create_file_type() to register it
3. Tell the user to load a data file in step 2 so you can profile it
4. Optionally help define extraction fields via add_extraction_field()

DELETING A FILE TYPE:
1. Call delete_file_type(file_type, dry_run=True) and show the user the returned file list
2. Ask the user to confirm explicitly
3. Only after a clear yes, call delete_file_type(file_type, dry_run=False) and report the result

CONTROL CHECK TYPES:
- cross_total: column sums that must reconcile (e.g. sum of rows == total row)
- completeness: required fields must not be empty/null
- cross_sheet: values that must match across multiple sheets
- format: date formats, numeric ranges, allowed values
- outlier: statistical anomalies (values > 3 sigma, negative values where impossible, etc.)

MANDATORY RULES — follow these strictly at all times:
0. NEVER ASK FOR WHAT YOU ALREADY HAVE: do not ask the user for the file type, for column
   or field names, or for sample rows. The file type is in ## CONTEXT and the columns and
   sample rows are in the data profile — use get_file_profile() / get_column_values()
   instead of asking. Do not ask for severity or owner either; choose a sensible default,
   say which default you chose, and let the user correct it. If a data profile genuinely
   does not exist yet, say so plainly and ask the user to load a data file in step 2 —
   that is the ONE thing you may ask for, and never as a list of questions.
1. ALWAYS CALL THE TOOLS: When the user asks you to create a check, you MUST go through draft_check_code() → test_draft_check() → review_check_code() → save_draft_check(). Never just describe or show code in the chat — a check that was not saved through the tools does not exist.
2. NEVER SAVE A CHECK YOU HAVE NOT RUN: call test_draft_check() at least once before save_draft_check(). If the test could not run at all (no data file loaded, executor error), say so and do not pretend the check is verified.
2a. NEVER SAVE A CHECK YOU HAVE NOT REVIEWED: call review_check_code() on the final code before save_draft_check(). If it returns NEEDS_CHANGES, fix and re-test rather than saving — unless you have spent your 4 iterations, in which case save with known_limitation= naming exactly what the review said was missing, and tell the user. If the review itself could not run, that is not an approval: say so.
3. NEVER CLAIM A CHECK WORKS WITHOUT EVIDENCE FROM A TEST RUN: "this will catch X" is a claim about behaviour. You may only make it after a run showed it catching X. If you saved something you are unsure about, say which part you are unsure about. Passing a test run is not the same as matching the request — only review_check_code() speaks to that.
4. REPORT THE CHECK ID: After save_draft_check() succeeds, always tell the user the check_id that was saved (e.g. "Saved as check_003"), and what the test run showed.
5. DO NOT CLAIM SUCCESS WITHOUT TOOL CONFIRMATION: Never say a check was saved, created, or approved unless the corresponding tool call actually succeeded and returned a success message. If the tool returns an error, report the error clearly.
6. APPROVE ONLY AFTER USER CONFIRMATION: Call approve_check() only after the user explicitly tells you the test passed. Do not approve automatically.
7. SUMMARIZE AT END OF TURN: At the end of each turn where you created or modified checks, list the check_ids that are now draft vs. active so the user has a clear picture of what is stored in S3.
8. USE TOOLS FOR ALL STATE CHANGES: All file type creation, check creation, check regeneration, and approval MUST go through the provided tools. Do not describe what you "would" do — do it by calling the tool.
9. NEVER DELETE A FILE TYPE WITHOUT A DRY RUN AND AN EXPLICIT CONFIRMATION: delete_file_type() destroys the whole file type — its metadata, its extraction fields, and every check (.json and .py) — and there is no undo and no backup. You MUST first call delete_file_type(file_type, dry_run=True), show the user the complete file list it returns, and ask them to confirm in plain words (e.g. "Delete these N files permanently? yes/no"). Only after the user answers yes may you call delete_file_type(file_type, dry_run=False), and you must then report the number of files actually deleted. Never call it with dry_run=False in the same turn the deletion was first requested, never infer confirmation from an earlier or unrelated message, and never use delete_file_type() when the user only asked to remove one check — that is delete_check().

Always be concise and guide the user step by step. Confirm each action taken.
Never make up data — only use what the user provides.
Respond in the same language the user uses (Hebrew or English).
"""

# ============== AGENT SETUP ==============

model = BedrockModel(
    model_id=BEDROCK_MODEL_ID,
    temperature=0.1,
    streaming=False,
)

agent_tools = [
    list_file_types,
    create_file_type,
    add_extraction_field,
    list_extraction_fields,
    delete_extraction_field,
    draft_check_code,
    test_draft_check,
    review_check_code,
    save_draft_check,
    approve_check,
    regenerate_check,
    list_checks,
    get_check,
    delete_check,
    delete_file_type,
    get_file_profile,
    get_column_values,
]


def _to_converse_messages(conversation_history) -> list:
    """
    Normalise the UI's conversation history into the Converse message format Strands expects.

    The UI sends [{"role": "user", "content": "text"}, ...]; Converse wants the content
    as a list of blocks. Entries already in block form are passed through untouched.
    """
    if not isinstance(conversation_history, list):
        return []

    messages = []
    for entry in conversation_history:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        if isinstance(content, str):
            content = [{"text": content}]
        elif not isinstance(content, list):
            continue
        messages.append({"role": role, "content": content})

    # Converse rejects a history that does not alternate starting from the user,
    # so drop any leading assistant turns.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    return messages


def _build_agent(prior_messages: list) -> Agent:
    """
    Build a per-request Agent seeded with this session's history (Converse format).

    Deliberately NOT a module-level singleton: a warm Lambda container is shared across
    unrelated sessions, and a long-lived Agent would carry one user's conversation into
    the next user's request. Only the cheap wrapper is rebuilt — the model, the tools and
    the system prompt stay at module scope.

    requirements.txt pins no Strands version, so seeding falls back to assigning .messages
    if this build's Agent does not take a 'messages' kwarg. Either way the history is
    applied — the agent must never start a follow-up turn blank.
    """
    try:
        return Agent(
            model=model,
            tools=agent_tools,
            system_prompt=SYSTEM_PROMPT,
            messages=prior_messages,
        )
    except TypeError:
        built = Agent(model=model, tools=agent_tools, system_prompt=SYSTEM_PROMPT)
        built.messages = list(prior_messages)
        return built


# Initialize Bedrock Agent Core App
def _render_check_context(ctx: dict) -> str:
    """
    Render the wizard's state as a CONTEXT preamble for the user's message.

    This is what stops the agent re-asking for the file type and the data. The UI already
    knows both by step 3; before this existed none of it was transmitted, so the agent had
    no choice but to interrogate the user for facts sitting one panel away.

    A column digest is inlined rather than left to a tool call, because the most common
    failure was the agent asking instead of looking. Full detail stays behind
    get_file_profile() / get_column_values().
    """
    if not isinstance(ctx, dict) or not ctx:
        return ""

    lines = ["## CONTEXT (supplied by the UI — do not ask the user for any of this)"]

    file_type = (ctx.get('file_type') or '').strip()
    if file_type:
        lines.append(f"- Active file type: {file_type}")
    if ctx.get('data_file_name'):
        lines.append(f"- Loaded data file: {ctx['data_file_name']}")
    if ctx.get('data_s3_path'):
        lines.append(f"- Data file in S3: {ctx['data_s3_path']}")

    current = ctx.get('current_check')
    if isinstance(current, dict) and current.get('id'):
        lines.append(
            f"- The user is EDITING an existing check: {current.get('id')} "
            f"({current.get('name', '')}, status={current.get('status', '?')}). "
            f"Use regenerate_check() to change it — do not create a new one."
        )

    profile = _load_file_profile(file_type) if file_type else None
    if profile:
        total = profile.get('total_rows', -1)
        total_txt = f"{total:,}" if isinstance(total, int) and total >= 0 else "unknown"
        lines.append(
            f"- Data profile IS available. Checks run on the first "
            f"{profile.get('sample_rows', '?')} rows of {total_txt}."
        )
        for sheet in profile.get('sheets', []):
            cols = sheet.get('columns', [])
            lines.append(
                f"- {sheet.get('name')}: {sheet.get('n_columns')} columns, "
                f"{sheet.get('profiled_rows')} rows profiled. Columns (exact names):"
            )
            for col in cols:
                tops = (col.get('top_values') or [])[:6]
                tops_txt = ", ".join(str(t.get('value')) for t in tops)
                suffix = f" e.g. {tops_txt}" if tops_txt else ""
                lines.append(f"    {col.get('name')!r} ({col.get('dtype')}){suffix}")
        lines.append(
            "- Call get_file_profile() for sample rows and full value counts, "
            "or get_column_values() to drill into one column."
        )
    elif file_type:
        lines.append(
            "- No data profile stored yet: the user has not loaded a data file. "
            "Ask them to load one in step 2 before you write check code."
        )

    return "\n".join(lines)


app = BedrockAgentCoreApp()

logger.info("✅ CSV Checks Config Agent initialized")
logger.info(f"📊 Tools available: {len(agent_tools)}")


# ============== ENTRYPOINT ==============

@app.entrypoint
def csv_checks_config_agent(payload):
    """
    CSV Checks Config Agent entrypoint.
    Handles chat requests to configure file types, extraction fields, and control checks.
    """
    logger.info("=" * 80)
    logger.info("📋 CSV CHECKS CONFIG AGENT - REQUEST RECEIVED")
    logger.info("=" * 80)
    logger.info(f"📦 Payload: {json.dumps(payload, default=str, ensure_ascii=False)[:500]}")

    try:
        user_input = (
            payload.get("prompt", "")
            or payload.get("comment", {}).get("message", "")
            or payload.get("comment", {}).get("prompt", "")
            or payload.get("text", "")
        )
        session_state = payload.get("sessionState", {}) or payload.get("comment", {}).get("sessionState", {})
        if not isinstance(session_state, dict):
            session_state = {}

        if not user_input:
            return {
                "response": json.dumps({
                    "error": "Missing user input",
                    "message": "שגיאה: לא נמצאה בקשה מהמשתמש"
                }, ensure_ascii=False),
                "sessionState": session_state
            }

        logger.info(f"👤 User input: {user_input[:200]}...")

        # Prepend the wizard's state so the agent never re-asks for the file type or the
        # data. Only the prompt sent to the model carries it — the history we persist keeps
        # the user's own words, so the context block is refreshed each turn rather than
        # accumulating stale copies.
        check_context = (
            payload.get("checkContext")
            or payload.get("comment", {}).get("checkContext")
            or {}
        )
        context_block = _render_check_context(check_context)
        prompt = f"{context_block}\n\n---\n\n{user_input}" if context_block else user_input
        if context_block:
            logger.info(f"🧭 Context injected ({len(context_block)} chars)")

        prior_messages = _to_converse_messages(session_state.get("conversationHistory"))
        agent = _build_agent(prior_messages)
        logger.info(f"🤖 Invoking agent with {len(prior_messages)} prior message(s)...")
        response = agent(prompt)

        response_text = str(response)
        if hasattr(response, 'message'):
            if isinstance(response.message, dict) and 'content' in response.message:
                content = response.message['content']
                if isinstance(content, list) and len(content) > 0:
                    for item in content:
                        if isinstance(item, dict):
                            if 'text' in item:
                                response_text = item['text']
                                break
                            elif 'toolUse' in item or 'tool_use' in item:
                                tool_use = item.get('toolUse') or item.get('tool_use')
                                if isinstance(tool_use, dict):
                                    logger.info(f"🔧 Tool was called: {tool_use.get('name', 'unknown')}")
                        elif isinstance(item, str):
                            response_text = item
                            break
                else:
                    response_text = str(content)
            else:
                response_text = str(response.message)

        logger.info(f"✅ Agent response: {response_text[:300]}...")

        if "conversationHistory" not in session_state:
            session_state["conversationHistory"] = []

        session_state["conversationHistory"].append({
            "role": "user",
            "content": user_input
        })
        session_state["conversationHistory"].append({
            "role": "assistant",
            "content": response_text
        })

        if len(session_state["conversationHistory"]) > 20:
            session_state["conversationHistory"] = session_state["conversationHistory"][-20:]

        return {
            "response": json.dumps({
                "message": response_text,
                "success": True
            }, ensure_ascii=False),
            "sessionState": session_state
        }

    except Exception as e:
        logger.error(f"❌ Error in csv_checks_config_agent: {e}", exc_info=True)
        import traceback
        traceback.print_exc()

        return {
            "response": json.dumps({
                "error": str(e),
                "message": f"שגיאה בעיבוד הבקשה: {str(e)}",
                "success": False
            }, ensure_ascii=False),
            "sessionState": session_state if 'session_state' in locals() else {}
        }


# ============== RUN ==============

if __name__ == "__main__":
    logger.info("=" * 80)
    logger.info("🚀 CSV CHECKS CONFIG AGENT")
    logger.info("📋 Configure file types, extraction fields, and control checks")
    logger.info("=" * 80)
    logger.info(f"📊 Tools: {len(agent_tools)}")
    for i, tool_func in enumerate(agent_tools, 1):
        logger.info(f"   {i}. {tool_func.__name__}")
    logger.info("=" * 80)

    app.run()
