"""Remi's instructions: RULES.md distilled into a template, rendered per call.

The template lives in ``instructions.md`` (``string.Template``; ``$$`` is a
literal dollar). Rendering takes only the ``CallContext`` and the agency row,
so every vendor sends byte-identical instructions (tests/test_parity.py) and
the date the model sees is always the call's date, never wall time.
"""

from __future__ import annotations

from importlib.resources import files
from string import Template

from ..context import CallContext
from ..session import AGENT_NAME
from ..world import Agency

_TEMPLATE = Template(files(__package__).joinpath("instructions.md").read_text(encoding="utf-8"))


def _spoken_digits(number: str) -> str:
    digits = [d for d in number if d.isdigit()]
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return number
    return f"{' '.join(digits[:3])}, {' '.join(digits[3:6])}, {' '.join(digits[6:])}"


def _clock_time(context: CallContext) -> str:
    hour = context.call_time.hour % 12 or 12
    meridiem = "a.m." if context.call_time.hour < 12 else "p.m."
    return f"{hour}:{context.call_time.minute:02d} {meridiem}"


def render_instructions(context: CallContext, agency: Agency) -> str:
    """The system instructions for this call."""
    when = context.call_time
    return _TEMPLATE.substitute(
        agent_name=AGENT_NAME,
        business_name=agency.name.removesuffix(", LLC"),
        call_date_long=f"{when:%A}, {when:%B} {when.day}, {when.year}",
        call_time_long=f"{_clock_time(context)} on {when:%A}, {when:%B} {when.day}, {when.year}",
        callback_number_spoken=_spoken_digits(agency.callback_number),
        callback_contact=agency.callback_contact,
    )
