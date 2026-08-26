"""What the flat renderer puts on the page, with no AWS and no PDF.

The failure these exist to prevent: a printed tick square receiving a *string*.
The geometry pass recognizes those squares off the page's own content stream and
records it as `backend.mark`, but for a long time nothing read that flag, so the
renderer's only signal was `type` — the ingest model's opinion. When the model
called a square "text", the value was written into a box a few points across as
illegible ink laid over the form's own printing.

`_draw_field` is exercised through a recording canvas rather than a real one: the
question is only which drawing call was made and where, and reportlab's own output
would have to be parsed back out of a PDF to answer it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import schema as sch
from functions.api_render import _draw_field

W, H = 595.0, 842.0
# A tick square on a real 101: about nine thousandths of a page wide.
SQUARE = [0.723, 0.332, 0.732, 0.341]


class Recorder:
    """Stands in for a reportlab canvas, remembering only what was drawn."""

    def __init__(self):
        self.strings = []   # text written along a baseline
        self.ticks = []     # centred marks — what _draw_tick makes

    def setFont(self, *_a, **_kw):
        pass

    def drawString(self, x, y, text):
        self.strings.append(text)

    def drawRightString(self, x, y, text):
        self.strings.append(text)

    def drawCentredString(self, x, y, text):
        self.ticks.append((x, y, text))


def field(**kw) -> sch.FormField:
    base = {"field_id": "f", "label": "l", "type": "text", "bbox": list(SQUARE)}
    return sch.FormField.from_dict({**base, **kw})


def draw(f, value) -> Recorder:
    c = Recorder()
    _draw_field(c, f, value, W, H, "Helvetica")
    return c


def test_a_tick_square_never_receives_text():
    """The flag the geometry pass set is what decides this, not the type. Both
    orderings are checked because either one alone leaves the other open."""
    by_flag = field(type="text", backend={"kind": "overlay", "mark": "checkbox"})
    c = draw(by_flag, "כן")
    assert c.strings == [], "a printed square must never be written into"
    assert [t[2] for t in c.ticks] == ["X"]

    by_type = field(type="checkbox", backend={"kind": "overlay"})
    c = draw(by_type, True)
    assert c.strings == [] and [t[2] for t in c.ticks] == ["X"]


def test_an_ordinary_field_still_gets_its_text():
    """The guard is additive: a field with no square keeps writing strings."""
    wide = field(bbox=[0.2, 0.3, 0.6, 0.32])
    c = draw(wide, "Israel Israeli")
    assert c.strings == ["Israel Israeli"] and c.ticks == []

    # Hebrew goes through the bidi algorithm on the way out, so only its presence
    # is asserted here — test_geometry covers what that reordering produces.
    c = draw(wide, "ישראל ישראלי")
    assert len(c.strings) == 1 and c.strings[0].strip() and c.ticks == []


def test_which_values_mark_a_square():
    """A square should hold a boolean. A field typed wrong upstream reaches the
    renderer holding whatever the agent wrote, and on these forms a negative is
    as often "לא" as it is False — ticking that would read as a deliberate yes."""
    f = field(type="text", backend={"kind": "overlay", "mark": "checkbox"})

    for value in (True, "כן", "yes", "X", "true", "נשוי/אה", 1):
        assert draw(f, value).ticks, f"{value!r} should mark the square"

    for value in (False, None, "", "לא", "no", "false", "0", "off", []):
        c = draw(f, value)
        assert not c.ticks, f"{value!r} should leave the square blank"
        assert not c.strings, f"{value!r} must not be written either"


def test_a_choice_marks_the_square_it_chose():
    """A printed choice carries one box per option, so the mark goes on the chosen
    square — not on the square the field happened to anchor on."""
    single = [0.83, 0.332, 0.839, 0.341]
    married = [0.72, 0.332, 0.729, 0.341]
    f = field(type="select", options=["רווק/ה", "נשוי/אה"], bbox=list(single),
              backend={"kind": "overlay", "mark": "checkbox", "option_boxes": [
                  {"value": "רווק/ה", "bbox": single},
                  {"value": "נשוי/אה", "bbox": married},
              ]})

    c = draw(f, "נשוי/אה")
    assert c.strings == [], "a choice is marked, never written"
    assert len(c.ticks) == 1
    assert c.ticks[0][0] == pytest.approx((married[0] + married[2]) / 2 * W), "wrong square"

    # A value matching no printed choice is left unstamped rather than guessed at.
    assert draw(f, "אלמן/ה").ticks == []
