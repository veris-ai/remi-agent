"""Closing the call: callbacks, letters, transfers, the contact log, hanging up.

Every call ends with exactly one contact-attempt row (NYC § 2-193).
``log_contact_attempt`` writes it with the duration from ``CallClock`` and the
disposition derived from what the session recorded (WORLD.md's precedence), so
the model supplies only who answered and a plain-language summary. If the
other side hangs up first, ``CallSession.finalize`` writes the row instead.
``end_call`` is refused until the call is logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import policy
from ..context import EASTERN
from ..session import CallSession, new_id
from ..tool_registry import Gate, ToolError
from .catalog import REGISTRY


class CallbackArgs(BaseModel):
    scheduled_for: datetime = Field(
        description="When to call back, ISO-8601 in Eastern time, e.g. 2027-02-17T10:00:00-05:00."
    )
    consumer_requested: bool = Field(description="True when the consumer named this time themselves.")


@REGISTRY.tool(CallbackArgs, requires=Gate.VERIFIED)
async def schedule_callback(session: CallSession, args: CallbackArgs) -> dict[str, Any]:
    """Schedule a call back on this number, after 8:00 a.m. and before 9:00 p.m. Eastern. Use it when the
    consumer names a time (consumer_requested true), e.g. after 'I'm at work' or 'not now'."""
    session.require_consumer()
    when = args.scheduled_for if args.scheduled_for.tzinfo else args.scheduled_for.replace(tzinfo=EASTERN)
    when = when.astimezone(EASTERN)
    if when <= session.clock.now():
        raise ToolError("A callback must be in the future.")
    if not policy.in_calling_window(when):
        raise ToolError(
            "Callbacks must be after 8:00 a.m. and before 9:00 p.m. Eastern. Ask for another time."
        )
    if not args.consumer_requested and when < session.clock.now() + policy.CAP_WINDOW:
        raise ToolError(
            "The agency may not schedule its own follow-up within 7 days of reaching the consumer."
        )
    async with session.db.transaction() as tx:
        await tx.insert(
            "callbacks",
            {
                "callback_id": new_id("cb"),
                "account_id": session.context.account_id,
                "phone_id": session.context.phone_id,
                "scheduled_for": when,
                "consumer_requested": args.consumer_requested,
            },
        )
    session.outcomes.add("callback")
    return {"scheduled_for": when.isoformat(), "consumer_requested": args.consumer_requested}


LetterKind = Literal[
    "time_barred",
    "plan_confirmation",
    "substantiation_instructions",
    "cease_confirmation",
    "original_creditor_info",
]


class LetterArgs(BaseModel):
    kind: LetterKind


@REGISTRY.tool(LetterArgs, requires=Gate.VERIFIED)
async def queue_letter(session: CallSession, args: LetterArgs) -> dict[str, Any]:
    """Queue a letter. Rarely needed: log_dispute, record_flag, create_arrangement, take_payment and
    log_contact_attempt already queue the letters their rules require, and a letter is queued at most once
    per call. A general 'send me something in writing' needs no new letter (the validation notice was mailed)."""
    session.require_consumer()
    async with session.db.transaction() as tx:
        queued = await session.queue_letters(tx, args.kind)
    return {"queued": args.kind} if queued else {"queued": args.kind, "note": "Already queued on this call."}


class TransferArgs(BaseModel):
    reason: Literal[
        "consumer_asked_for_person", "settlement_below_floor", "plan_outside_rules", "scra", "other"
    ]


@REGISTRY.tool(TransferArgs, requires=Gate.PROCEEDING)
async def transfer_to_human(session: CallSession, args: TransferArgs) -> dict[str, Any]:
    """Transfer the call to a person: on the first request for a person, and without being asked when the
    consumer wants a settlement below the minimum, a plan outside the house rules, or mentions active military
    duty. Never loop back to offering your own help first."""
    session.transferred = True
    return {
        "transferred": True,
        "reason": args.reason,
        "next": "Tell the consumer you are connecting them to a person now, then log_contact_attempt and end_call.",
    }


class ContactLogArgs(BaseModel):
    answered_by: Literal["consumer", "estate_rep", "third_party", "voicemail", "wrong_number", "none"] = (
        Field(
            description="Who you established answered: consumer or estate_rep only if verified; third_party for anyone "
            "else who said who they are; voicemail; wrong_number; none if nobody was confirmed or the call should not "
            "have been placed."
        )
    )
    summary: str = Field(
        min_length=10, description="One or two plain-language sentences: what happened and the outcome."
    )
    number_is_wrong: bool = Field(
        False,
        description="True when told the consumer cannot be reached at this number (wrong number, moved).",
    )


@REGISTRY.tool(ContactLogArgs, requires=Gate.ASSESSED)
async def log_contact_attempt(session: CallSession, args: ContactLogArgs) -> dict[str, Any]:
    """Write this call's one contact record, after your last words and before end_call. The time, duration and
    disposition are filled in for you from what was recorded on the call."""
    if session.contact_logged:
        raise ToolError("This call is already logged. Call end_call.")
    answered_by: str | None = None if args.answered_by == "none" else args.answered_by
    assert session.assessment is not None
    if not session.assessment.proceed:
        answered_by = None
    elif answered_by in ("consumer", "estate_rep") and session.verified_as != answered_by:
        raise ToolError(
            f"answered_by '{answered_by}' needs a verified {answered_by}. Use third_party or none."
        )
    elif answered_by is None and session.verified_as is not None:
        answered_by = session.verified_as
    disposition = await session.write_contact_attempt(
        answered_by=answered_by, summary=args.summary.strip(), number_is_wrong=args.number_is_wrong
    )
    return {
        "logged": True,
        "disposition": disposition,
        "duration_s": int(session.clock.elapsed / timedelta(seconds=1)),
    }


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs, requires=Gate.ASSESSED)
async def end_call(session: CallSession, _: NoArgs) -> dict[str, Any]:
    """Hang up. Say your goodbye first; the line closes once your last words have played. Requires
    log_contact_attempt first."""
    if not session.contact_logged:
        raise ToolError("Call log_contact_attempt first.")
    session.ended = True
    return {"ended": True, "next": "Say nothing more."}
