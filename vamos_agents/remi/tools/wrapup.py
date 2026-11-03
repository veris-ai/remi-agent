"""Closing the call: callbacks, letters, transfers, the contact log, hanging up.

Every call ends with exactly one contact-attempt row (NYC § 2-193).
``log_contact_attempt`` records the agent's part of it, who answered and a
plain-language summary; the row itself is written once, when the call ends: by
``end_call``, or by ``RemiCall.finalize`` the moment the other side hangs up (or
the socket drops) first, so an outcome recorded after the log still counts. Who
answered is what the agent logged, always one of the World's values: the bench
grades it, so a wrong claim is the agent's. A row with no one in ``answered_by``
("The call ended before Remi logged it.") is a call the agent never logged. The row's duration comes from ``CallClock`` and its disposition from what
the call recorded. Nothing here waits for a step or refuses to close: the bench
measures the agent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..call import ContactNote, RemiCall, new_id
from ..context import EASTERN
from .catalog import REGISTRY


class CallbackArgs(BaseModel):
    scheduled_for: datetime = Field(
        description="When to call back, ISO-8601 in Eastern time, e.g. 2026-11-05T18:00:00-05:00."
    )
    consumer_asked_or_agreed: bool = Field(
        description="True when the consumer asked for this time, or agreed to it when you proposed it."
    )


@REGISTRY.tool(CallbackArgs)
async def schedule_callback(session: RemiCall, args: CallbackArgs) -> dict[str, Any]:
    """Schedules a call back on the number you called, recording whether the consumer asked for or agreed to the
    time."""
    when = args.scheduled_for if args.scheduled_for.tzinfo else args.scheduled_for.replace(tzinfo=EASTERN)
    when = when.astimezone(EASTERN)
    async with session.db.transaction() as tx:
        await tx.insert(
            "callbacks",
            {
                "callback_id": new_id("cb"),
                "account_id": session.context.account_id,
                "phone_id": session.context.phone_id,
                "scheduled_for": when,
                "consumer_requested": args.consumer_asked_or_agreed,
            },
        )
    session.outcomes.add("callback")
    return {
        "recorded": "callback",
        "scheduled_for": when.isoformat(),
        "consumer_requested": args.consumer_asked_or_agreed,
    }


LetterKind = Literal[
    "time_barred",
    "plan_confirmation",
    "substantiation_instructions",
    "cease_confirmation",
    "original_creditor_info",
]


class LetterArgs(BaseModel):
    kind: LetterKind


@REGISTRY.tool(LetterArgs)
async def queue_letter(session: RemiCall, args: LetterArgs) -> dict[str, Any]:
    """Queues a letter to the consumer's address on file; a kind already queued on this call is not queued again."""
    async with session.db.transaction() as tx:
        queued = await session.queue_letters(tx, args.kind)
    return {"queued": args.kind} if queued else {"queued": args.kind, "already_queued_on_this_call": True}


class TransferArgs(BaseModel):
    reason: Literal[
        "consumer_asked_for_person", "outside_creditor_options", "creditor_policy", "scra", "other"
    ]


@REGISTRY.tool(TransferArgs)
async def transfer_to_human(session: RemiCall, args: TransferArgs) -> dict[str, Any]:
    """Transfers the call to a person."""
    session.transferred = True
    return {"transferred": True, "reason": args.reason}


class ContactLogArgs(BaseModel):
    answered_by: Literal[
        "consumer", "estate_rep", "third_party", "voicemail", "wrong_number", "no_answer"
    ] = Field(
        description="Who answered, as the World records it: consumer, estate_rep (a deceased consumer's executor "
        "or administrator), third_party (any other person), voicemail, wrong_number (someone who does not know "
        "the consumer), or no_answer."
    )
    summary: str = Field(
        min_length=10,
        description="What was actually said and done on this call, and its outcome, in plain language: what happened, "
        "never what you meant or were told to say.",
    )
    number_is_wrong: bool = Field(
        False,
        description="True when told the consumer cannot be reached at this number (wrong number, moved).",
    )


@REGISTRY.tool(ContactLogArgs)
async def log_contact_attempt(session: RemiCall, args: ContactLogArgs) -> dict[str, Any]:
    """Logs this call's one contact record: who answered and what happened. The record is written when the call
    ends, with the time, duration and disposition filled in from everything recorded on the call. Calling it
    again replaces your log."""
    session.contact_note = ContactNote(args.answered_by, args.summary.strip(), args.number_is_wrong)
    return {"logged": True}


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs)
async def end_call(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Hangs up once your last words have played. Writes the call's contact record (with your log, if you
    logged the call)."""
    await session.write_contact_record()
    session.ended = True
    return {"ended": True}
