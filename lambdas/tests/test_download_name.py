"""The name the filled document lands under, with no AWS and no PDF.

The failure these exist to prevent: a Hebrew form name reaching a header that
only carries ASCII. Every catalog entry on this product is named in Hebrew, so
the `filename*` half of RFC 6266's pair is not a nicety here — it is the only
half that ever holds the real name, and the ASCII `filename` beside it is a
fallback that will usually read `form-filled.pdf`.

It matters that this is exercised as a string: the value is signed into the
presigned URL as `response-content-disposition`, so a header that is wrong is
wrong at signing time and cannot be adjusted afterwards.
"""
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from functions.api_render import _ascii_stem, _disposition, _stem  # noqa: E402

BACKSLASH = chr(92)


def _quoted(disposition: str) -> str:
    """The plain `filename="..."` half."""
    return disposition.split('"')[1]


def _extended(disposition: str) -> str:
    """The `filename*=UTF-8''...` half, decoded back to the real name."""
    return unquote(disposition.split("UTF-8''")[1])


def test_known_extension_is_stripped_once():
    d = _disposition("annual-report-2024.pdf", "pdf")
    assert _quoted(d) == "annual-report-2024-filled.pdf"
    assert _extended(d) == "annual-report-2024-filled.pdf"


def test_hebrew_name_round_trips_through_the_extended_half():
    name = "טופס 101 - כרטיס עובד"
    d = _disposition(name, "pdf")
    assert _extended(d) == f"{name}-filled.pdf"


def test_whole_header_is_ascii():
    """Header values cannot carry anything else, and this one is signed."""
    _disposition("טופס 101 - כרטיס עובד", "pdf").encode("ascii")


def test_name_with_no_ascii_at_all_falls_back():
    # The ordinary case on this product, not the edge one: Hebrew has no
    # decomposition to ASCII, so NFKD leaves nothing behind.
    assert _ascii_stem(_stem("קבלה")) == ""
    assert _quoted(_disposition("קבלה", "pdf")) == "form-filled.pdf"


def test_missing_and_blank_names_fall_back():
    for raw in (None, "", "   ", "..."):
        assert _quoted(_disposition(raw, "pdf")) == "form-filled.pdf"


def test_quoted_half_cannot_break_out_of_its_quotes():
    d = _disposition(f'a"b{BACKSLASH}c/d', "pdf")
    assert not set(_quoted(d)) & {'"', BACKSLASH, "/"}


def test_docx_keeps_its_extension():
    d = _disposition("x", "docx")
    assert _quoted(d) == "x-filled.docx"
    assert _extended(d) == "x-filled.docx"


def test_a_version_number_is_not_mistaken_for_an_extension():
    """The reason the strip is keyed to *known* extensions rather than to the
    last dot: a catalog name ends in a revision far more often than a suffix."""
    assert _extended(_disposition("טופס 101 מהדורה 2.5", "pdf")) == "טופס 101 מהדורה 2.5-filled.pdf"


def test_a_very_long_name_cannot_blow_up_the_signed_query_string():
    d = _disposition("א" * 500, "pdf")
    assert len(d) < 1024
