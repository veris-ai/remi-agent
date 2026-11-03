"""Remi's screen pop: the account on the collector's screen when the dialer connects the call.

When the call connects, Remi makes its connect-time reads itself: the account
(``get_account``), the debt from the ledger (``get_debt_details``), the dialed
phone (``get_dialed_phone``) and the contact history (``get_contact_history``),
running the tools' own handlers on the call's ``RemiCall`` before any vendor
session opens, so the SELECTs land in the World's SQL log as Remi's before anyone
speaks. The tools stay available for the model to re-read.

What the model sees is the reads' own results, the same dicts the tools return:
the time to sue's end, the itemization since its basis, the loan's and the
notices' dates, a phone account's balance parts, an auto deficiency's car and
sale. No verdict: nothing here says whether the call may continue. No date of
birth, ZIP, SSN or street: verification stays server-side in
``verify_right_party``. A read that fails shows as its error; the rest still show.
"""

from __future__ import annotations

import json
from typing import Any

from vamos_voice.screen_pop import ScreenPop
from vamos_voice.tools import ToolError

from .call import RemiCall
from .tools.connect import NoArgs, get_account, get_contact_history, get_dialed_phone
from .tools.identity import get_debt_details

HEADING = "Account on screen"


async def screen_pop(call: RemiCall) -> ScreenPop:
    """Run the connect-time reads on ``call`` and render what they returned."""
    results: dict[str, dict[str, Any]] = {"get_account": await get_account(call, NoArgs())}
    for name, read in (
        ("get_debt_details", get_debt_details),
        ("get_dialed_phone", get_dialed_phone),
        ("get_contact_history", get_contact_history),
    ):
        try:
            results[name] = await read(call, NoArgs())
        except ToolError as err:
            results[name] = {"error": str(err)}
    return ScreenPop(HEADING, render(results))


def render(results: dict[str, dict[str, Any]]) -> str:
    """The section's text: who is being called, then each read's result."""
    account = results["get_account"]
    reads = ", ".join(results)
    lines = [
        f"The dialer ran the connect-time reads ({reads}) when the call connected, before anyone spoke. Their "
        "results are below; the same tools re-read them.",
        "",
        f"- You are calling {account['consumer_full_name']}, the consumer on account {account['account_id']}. "
        f"{account['you']['callback_contact']} is the agency's callback contact for people who call back, not "
        "the person you are calling.",
        "",
    ]
    for name, result in results.items():
        lines.append(f"{name}: {json.dumps(result, default=str, ensure_ascii=False)}")
    return "\n".join(lines)
