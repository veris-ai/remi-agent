"""Closing the call: callbacks, letters, transfers, the contact log, hanging up.

Every call ends with exactly one contact-attempt row (NYC § 2-193).
``log_contact_attempt`` records the model's part of it, who answered and a
plain-language summary, before its goodbye; the row itself is written once,
when the call ends: by ``end_call``, or by ``RemiCall.finalize`` the moment the
other side hangs up (or the socket drops) first. So the goodbye can never race
the record, and an outcome recorded after the log still counts. The row's
duration comes from ``CallClock`` and its disposition from what the session
recorded (WORLD.md's precedence). The stored summary leads with the facts the
session recorded (disposition, who answered, whether the debt was read and
after which attested disclosures, outcomes, a transfer), so the row stays true
even when the model's notes describe what it meant to say rather than what it
said; the notes follow, labelled as Remi's. ``end_call`` is refused until the
call is logged, and both wait for a transfer once SCRA was recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import ContactNote, RemiCall, new_id
from ..context import EASTERN
from ..gates import CLOSING, Gate
from .catalog import REGISTRY


class CallbackArgs(BaseModel):
    scheduled_for: datetime = Field(
        description="When to call back, ISO-8601 in Eastern time, e.g. 2027-02-17T10:00:00-05:00."
    )
    consumer_requested: bool = Field(description="True when the consumer named this time themselves.")


@REGISTRY.tool(CallbackArgs, requires=Gate.VERIFIED)
async def schedule_callback(session: RemiCall, args: CallbackArgs) -> dict[str, Any]:
    """Schedule a call back on this number, after 8:00 a.m. and before 9:00 p.m. Eastern. Use it when the
    consumer names a time (consumer_requested true), e.g. after 'I'm at work' or 'not now'."""
    session.require_consumer()
    when = args.scheduled_for if args.scheduled_for.tzinfo else args.scheduled_for.replace(tzinfo=EASTERN)
    when = when.astimezone(EASTERN)
    if when <= session.clock.now():
        raise ToolError("A callback must be in the future.")
    if not policy.in_calling_window(when):
        raise ToolError(
            "Callbacks must be after 8:00 a.m. and before 9:00 p.m. Eastern; that time is outside the window."
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
async def queue_letter(session: RemiCall, args: LetterArgs) -> dict[str, Any]:
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
async def transfer_to_human(session: RemiCall, args: TransferArgs) -> dict[str, Any]:
    """Transfer the call to a person: on the first request for a person, and without being asked when the
    consumer will not bring a settlement up to the minimum after your counter-offer, wants a plan outside the
    house rules, or mentions active military duty. Never loop back to offering your own help first."""
    session.transferred = True
    return {
        "transferred": True,
        "reason": args.reason,
        "allowed_now": "A person takes over from here; the call ends with log_contact_attempt and end_call.",
    }


class ContactLogArgs(BaseModel):
    answered_by: Literal["consumer", "estate_rep", "third_party", "voicemail", "wrong_number", "none"] = (
        Field(
            description="Who you established answered: consumer or estate_rep only if verified; third_party for anyone "
            "else who said who they are; voicemail; wrong_number; none if nobody was confirmed (failed or refused "
            "verification, a stop request from someone unverified) or the call should not have been placed."
        )
    )
    summary: str = Field(
        min_length=10,
        description="One or two plain-language sentences: what was actually said and done on this call, and the "
        "outcome. Only what happened, never what you meant or were told to say. Disposition, who answered and "
        "what was disclosed are recorded for you.",
    )
    number_is_wrong: bool = Field(
        False,
        description="True when told the consumer cannot be reached at this number (wrong number, moved).",
    )


@REGISTRY.tool(ContactLogArgs, requires=Gate.ASSESSED, tags=(CLOSING,))
async def log_contact_attempt(session: RemiCall, args: ContactLogArgs) -> dict[str, Any]:
    """Log this call's one contact record once the call is wrapping up, before your goodbye: who answered and
    what happened. The record is written when the call ends, with the time, duration and disposition filled in
    from everything recorded on the call. Calling it again replaces your summary."""
    if session.contact_disposition is not None:
        raise ToolError("This call's record is already written.")
    answered_by: str | None = None if args.answered_by == "none" else args.answered_by
    assert session.assessment is not None
    if not session.assessment.proceed:
        answered_by = None
    elif answered_by in ("consumer", "estate_rep") and session.verified_as != answered_by:
        raise ToolError(
            f"answered_by '{answered_by}' needs a verified {answered_by}. Use third_party or none."
        )
    session.contact_note = ContactNote(answered_by, args.summary.strip(), args.number_is_wrong)
    return {
        "logged": True,
        "disposition": session.disposition_for(session.recorded_answerer(answered_by)),
        "allowed_now": "The record is written when the call ends; what is recorded before then still counts. "
        "Next: your goodbye, then end_call.",
    }


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs, requires=Gate.ASSESSED, tags=(CLOSING,))
async def end_call(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Hang up. Say your goodbye first; the line closes once your last words have played. Requires
    log_contact_attempt first; writes the call's contact record."""
    if not session.contact_logged:
        raise ToolError("Call log_contact_attempt first.")
    disposition = await session.write_contact_record()
    session.ended = True
    return {"ended": True, "disposition": disposition, "allowed_now": "Nothing more is said."}
