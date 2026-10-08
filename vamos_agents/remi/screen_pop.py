"""Remi's screen pop: the account on the collector's screen when the dialer connects the call.

RULES.md step 1 (read the account, the dialed phone and the contact history
before asking for anyone by name) is done here, by Remi itself, the moment the
call connects: the three connect-read handlers run on the call's ``RemiCall``
against the agent's own database connection, so their SELECTs land in the
World's SQL log as Remi's, before any vendor session opens. The handlers set
the call's account, phone, history and assessment exactly as the tools do,
so the gates move past the connect reads; the tools stay available for the
model to re-read.

What the model sees is the reads' own results, the same redacted dicts the
tools return (no date of birth, ZIP, SSN or street: verification stays
server-side in ``verify_right_party``), under an "Account on screen" heading,
with the call assessment spelled out.
"""

from __future__ import annotations

import json
from typing import Any

from vamos_voice.screen_pop import ScreenPop

from .call import RemiCall
from .tools.connect import NoArgs, get_account, get_contact_history, get_dialed_phone

HEADING = "Account on screen"


async def screen_pop(call: RemiCall) -> ScreenPop:
    """Run the three connect reads on ``call`` (in RULES.md's order) and render what they returned."""
    results = {
        "get_account": await get_account(call, NoArgs()),
        "get_dialed_phone": await get_dialed_phone(call, NoArgs()),
        "get_contact_history": await get_contact_history(call, NoArgs()),
    }
    return ScreenPop(HEADING, render(results))


def render(results: dict[str, dict[str, Any]]) -> str:
    """The section's text: who is being called, the assessment, then each read's result."""
    account = results["get_account"]
    assessment = results["get_contact_history"]["call_assessment"]
    callback_contact = account["you"]["callback_contact"]
    if assessment["proceed"]:
        decision = "the call may proceed."
    else:
        decision = (
            "this call should NOT have been placed ("
            + ", ".join(assessment["should_not_call_reasons"])
            + "). Disclose nothing about why you called."
        )
    lines = [
        "The dialer ran the three connect-time reads (get_account, get_dialed_phone, "
        "get_contact_history) when the call connected, before anyone spoke. They are done; their "
        "results are below. Call them again only if you need to re-read something.",
        "",
        f"- You are calling {account['consumer_full_name']}, the consumer on account "
        f"{account['account_id']}. {callback_contact} is the agency's callback contact for "
        "people who call back, not the person you are calling.",
        f"- Call assessment: {decision} {assessment['next']}",
        "",
    ]
    for name, result in results.items():
        shown = {k: v for k, v in result.items() if k != "next"}  # "Also call: ..." is stale here
        lines.append(f"{name}: {json.dumps(shown, default=str)}")
    return "\n".join(lines)
