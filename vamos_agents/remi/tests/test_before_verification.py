"""The prompt: what the rendered instructions say about the opening, verification, push-back and the debt.

These check the reviewed prompt's own words (``prompt/instructions.md``), rendered for a call; the tools'
side of what happens before verification is ``test_tools.py``.
"""

from __future__ import annotations

import re
from datetime import datetime

from vamos_agents.remi.context import CallContext
from vamos_agents.remi.prompt import render_instructions
from vamos_agents.remi.world import Agency


def rendered_prompt() -> str:
    context = CallContext("TAS-261101", "ph_261101_1", datetime.fromisoformat("2026-11-03T11:20:00-05:00"))
    agency = Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")
    return render_instructions(context, agency)


def section(text: str, heading: str) -> str:
    """The text under the heading line ``heading`` (e.g. "## 6. Verification"), up to the next heading."""
    return f"\n{text}".split(f"\n{heading}\n")[1].split("\n#")[0]


def test_the_greeting_uses_a_given_name_and_never_re_asks_it() -> None:
    text = rendered_prompt()
    opening = section(text, "## 4. Opening")
    assert (
        "A person who has already given the consumer's name as their own is not asked for it again."
        in opening
    )
    assert (
        "opens with the collector's name, the agency's name (Tallmere Account Services) and the fact "
        "that the collector is an automated assistant" in opening
    )
    assert (
        "If the call may continue, the collector then asks for the consumer by name; the first name is enough."
        in opening
    )
    # a call that may not continue never asks for the consumer
    assert "does not ask for the consumer" in section(text, "## 3. Whether a call may continue")
    # the manual lets the consumer be named by first or full name before verification
    before = section(text, "## 5. Before the right person is confirmed")
    assert "calling about a personal business matter for the consumer, named by first or full name" in before


def test_the_manual_explains_the_check_and_what_follows_verification() -> None:
    text = rendered_prompt()
    for heading, line in (
        ("# The call", "The agency's callback number, answered by a person, is 2 1 2, 5 5 5, 0 1 0 0;"),
        (
            "## 5. Before the right person is confirmed",
            "says nothing about a debt, the creditor, an amount, or that the call is about money, in any words",
        ),
        ("## 5. Before the right person is confirmed", "whether now is a good time"),
        (
            "## 5. Before the right person is confirmed",
            "why verification details are needed, said before or when it first asks for them",
        ),
        (
            "## 5. Before the right person is confirmed",
            "A wary person is offered a way out no later than their second push-back.",
        ),
        (
            "## 6. Verification",
            "verified by their name, said by them or confirmed when asked for, plus any two of date of birth, "
            "ZIP code and the last four digits of their Social Security number",
        ),
        ("## 6. Verification", "The person chooses which two."),
        ("## 6. Verification", "Three wrong answers end the call with nothing disclosed."),
        (
            "## 8. Required statements, once verified",
            "these come in this order, before the creditor, the account or any amount, in one turn or several",
        ),
        (
            "## 8. Required statements, once verified",
            "that the call is recorded, and the recording may be used in connection with collecting a debt",
        ),
        (
            "## 8. Required statements, once verified",
            "that the agency is a debt collector, this is an attempt to collect a debt, and any information "
            "obtained will be used for that purpose",
        ),
        (
            "## 9. Stating the debt",
            "On request: the itemization from the itemization date the account states",
        ),
        ("## 9. Stating the debt", "(12 CFR 1006.34(b)(3))"),
    ):
        assert line in section(text, heading), line


def test_the_prompt_never_says_the_accounts_are_charged_off() -> None:
    """Not every account is charged off: a hospital account is itemized from its last statement."""
    text = rendered_prompt()
    assert "Creditors place past-due consumer accounts with the agency" in text
    assert re.search(r"charge[d -]*off", text, re.IGNORECASE) is None


def test_push_back_gets_a_reason_and_a_way_out() -> None:
    text = rendered_prompt()
    before = section(text, "## 5. Before the right person is confirmed")
    for line in (
        "why verification details are needed, said before or when it first asks for them",
        "that this is the number the agency has on file for the consumer",
        "A way out is the callback number answered by a person, an offer to call back later or for them to "
        "call that number, or that anything in writing goes to their address on file.",
        "A wary person is offered a way out no later than their second push-back.",
    ):
        assert line in before, line
    opening = section(text, "## 4. Opening")
    assert (
        "Asked whether it is a person, the collector says truthfully that it is an automated assistant."
        in opening
    )
    assert (
        "The callback number is stated during every call, including calls that end before verification."
        in opening
    )
    verification = section(text, "## 6. Verification")
    refusal = verification.split("6.5. ")[1].split("\n6.6. ")[0]
    assert "offers a way out" in refusal and "doesn't argue" in refusal and "records the refusal" in refusal
    # smoke v8: ~30 calls ended when a wary consumer asked for a number or a letter; those are questions
    assert "send me a letter" not in refusal.lower() and "not giving you that" not in refusal
    stop = verification.split("6.6. ")[1]
    assert "calls to this number stop" in stop and "consent on this number is revoked" in stop


def test_no_leaked_example_amount_or_name() -> None:
    text = rendered_prompt()
    # CPLR 214-i, quoted verbatim, cites "section two hundred thirteen-a"; no other "hundred" may appear
    assert text.count("two hundred thirteen-a") == 1
    for leaked in ("185", "hundred", "Marisol", "May I speak with"):
        assert leaked not in text.replace("two hundred thirteen-a", ""), leaked
