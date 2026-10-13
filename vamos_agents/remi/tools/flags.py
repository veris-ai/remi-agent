"""Signals the consumer gives: disputes, ceases, attorneys, bankruptcy, death, SCRA, hardship.

Each tool writes exactly the rows WORLD.md and RULES.md's "After the call"
table require for that signal (the flag, its letter, the account status, the
phone's consent), in one transaction, and stops collection on this call when
the rule says so. The session then refuses every payment tool, so a model
that keeps collecting after a dispute gets a tool error, not a payment.

Before verification two signals change the dialed number, never the account
(RULES.md, "Stop requests before verification", "Wrong number"): a wrong
number marks the phone ``wrong_number`` and revokes its consent only if the
person also asked to stop calling it; a stop request from anyone else revokes
consent and leaves an ``unverified_stop_request`` flag for review.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vamos_voice.tools import ToolError

from ..call import RemiCall
from ..gates import Gate
from ..world import Phone
from .catalog import REGISTRY

DisputeReason = Literal[
    "not_mine",
    "wrong_amount",
    "already_paid",
    "identity_theft",
    "verification_request",
    "insurance",
    "medical_financial_assistance",
]


class DisputeArgs(BaseModel):
    reason: DisputeReason = Field(
        description="not_mine, wrong_amount, already_paid, identity_theft, verification_request, insurance "
        "(insurance should have paid it), or medical_financial_assistance (they applied for or qualify for the "
        "provider's financial assistance)."
    )


@REGISTRY.tool(DisputeArgs, requires=Gate.VERIFIED)
async def log_dispute(session: RemiCall, args: DisputeArgs) -> dict[str, Any]:
    """Record a dispute the first time the consumer says the debt is not theirs, is the wrong amount, was
    already paid, is identity theft, should have been paid by insurance, asks for verification, or (medical
    debt) says they applied for or are eligible for financial assistance. Asking how the balance was calculated
    or what makes it up is not a dispute (give the itemization) unless they also say the amount is wrong.
    Queues the substantiation letter and stops collection on this call."""
    session.require_consumer()
    async with session.db.transaction() as tx:
        if args.reason == "medical_financial_assistance":
            await session.insert_flag(tx, "medical_financial_assistance", {})
        else:
            await session.insert_flag(tx, "dispute", {"reason": args.reason})
        await session.queue_letters(tx, "substantiation_instructions")
        await tx.update("accounts", session.context.account_id, {"status": "disputed"})
    session.stop_collection("dispute")
    session.outcomes.add("dispute")
    return {
        "recorded": "dispute",
        "allowed_now": "Collection has stopped for this call: no amount, offer or payment request. A dispute never "
        "has to be in writing. What is true now, for your own plain words: they can ask in writing for proof of the "
        "debt, and a letter explaining how is being mailed to them.",
    }


FlagKind = Literal[
    "cease",
    "channel_optout",
    "attorney",
    "bankruptcy",
    "deceased",
    "scra",
    "hardship",
    "original_creditor_request",
]


class FlagArgs(BaseModel):
    kind: FlagKind
    channel: Literal["call", "sms", "email"] | None = Field(
        None, description="channel_optout: the channel to stop."
    )
    this_number_only: bool | None = Field(
        None,
        description="channel_optout: true when they only mean the number you called (e.g. 'not my cell').",
    )
    attorney_name: str | None = Field(None, description="attorney: the attorney's name.")
    attorney_phone: str | None = Field(None, description="attorney: the attorney's phone number, digits.")
    chapter: Literal["7", "11", "13"] | None = Field(None, description="bankruptcy: the chapter.")
    case_number: str | None = Field(None, description="bankruptcy: the case number.")
    court: str | None = Field(None, description="bankruptcy: the court.")
    filed_on: date | None = Field(None, description="bankruptcy: the filing date, YYYY-MM-DD.")
    reported_by: str | None = Field(
        None, description="deceased: the name the person reporting the death gave (ask for it)."
    )
    relationship: str | None = Field(
        None, description="deceased: their relationship to the consumer, e.g. spouse, child, sibling (ask)."
    )
    died_on: date | None = Field(
        None,
        description="deceased: the date of death the person said, YYYY-MM-DD (ask for it). Leave it out if "
        "they did not give one; never today's date or a guess.",
    )
    branch: str | None = Field(None, description="scra: the military branch they serve in (ask if not said).")
    hardship_reason: Literal["job_loss", "medical", "bereavement", "disability", "income"] | None = Field(
        None,
        description="hardship: the reason, as the consumer stated it in their own words; never inferred.",
    )


#: Words a model puts in a field it never asked about: not something the person said.
PLACEHOLDERS = frozenset(
    {
        "unknown",
        "not given",
        "not provided",
        "not stated",
        "n/a",
        "na",
        "none",
        "caller",
        "the caller",
        "person",
        "the person",
        "the person on the line",
        "someone",
        "unnamed",
        "anonymous",
        "tbd",
    }
)


def _given(value: object) -> bool:
    return value is not None and str(value).strip().strip(".").lower() not in PLACEHOLDERS | {""}


def _need(args: FlagArgs, *names: str) -> None:
    missing = [n for n in names if not _given(getattr(args, n))]
    if missing:
        raise ToolError(f"{args.kind} needs {', '.join(missing)}, which the person has not given yet.")


@REGISTRY.tool(FlagArgs, requires=Gate.PROCEEDING)
async def record_flag(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    """Record what the consumer told you, the first time they say it: cease (stop contacting me, any words),
    channel_optout (stop one channel, e.g. 'don't call my cell', 'only email me'; one call per channel),
    attorney (name and phone), bankruptcy (chapter, case number, court, filing date), deceased (who reported
    it, relationship, date of death; may be recorded before verification), scra (active military duty),
    hardship (what the consumer said makes paying hard, in their own words; never one you inferred or
    suggested), original_creditor_request (they ask for the original
    creditor's name or address; returns it). Only for the verified consumer, except deceased; a stop request
    from anyone else goes to stop_calling_this_number. Recording deceased again with details you learned later
    (a name, the date of death) completes the record."""
    if args.kind in ("cease", "channel_optout") and session.verified_as is None:
        raise ToolError(
            "This person is not verified, so their request cannot close the account; an unverified request to "
            "stop calling is stop_calling_this_number."
        )
    if args.kind != "deceased":
        session.require_consumer()
    handler = _HANDLERS[args.kind]
    return await handler(session, args)


class WrongNumberArgs(BaseModel):
    asked_to_stop_calling: bool = Field(
        description="True only when the person themselves asked you to stop calling this number ('stop calling', "
        "'take this number off your list'). Saying it is the wrong number is not, on its own, a request to stop."
    )


@REGISTRY.tool(WrongNumberArgs, requires=Gate.PROCEEDING)
async def report_wrong_number(session: RemiCall, args: WrongNumberArgs) -> dict[str, Any]:
    """The person says this is not the consumer's number (a wrong number, not someone who knows the consumer).
    Marks the dialed number wrong_number; revokes its artificial-voice consent only if they asked you to stop
    calling it. The account is untouched and no letter follows."""
    if session.verified_as is not None:
        raise ToolError("The consumer is verified on this number, so it is not a wrong number.")
    phone = session.phone
    assert phone is not None
    async with session.db.transaction() as tx:
        if not session.phone_marked_wrong:
            await tx.update("phones", phone.phone_id, {"status": "wrong_number"})
        if args.asked_to_stop_calling:
            await session.revoke_consent(tx, [phone])
    session.phone_marked_wrong = True
    session.number_stop = "wrong_number"
    return {
        "recorded": "wrong_number",
        "consent_revoked": args.asked_to_stop_calling,
        "allowed_now": "Nothing more may be asked (no verification) or disclosed about why you called. The "
        "call ends: log_contact_attempt (answered_by wrong_number), goodbye, end_call.",
    }


class StopArgs(BaseModel):
    # No arguments. ``wrong_number`` was retired: a wrong number is ``report_wrong_number``, and the old
    # call shape is refused (nothing written) rather than read as a plain stop request.

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _retired_wrong_number(cls, data: Any) -> Any:
        if isinstance(data, dict) and "wrong_number" in data:
            raise ValueError(
                "stop_calling_this_number takes no wrong_number. If the person says this is the wrong "
                "number, that is report_wrong_number (asked_to_stop_calling true if they asked you to stop); "
                "nothing was recorded."
            )
        return data


@REGISTRY.tool(StopArgs, requires=Gate.PROCEEDING)
async def stop_calling_this_number(session: RemiCall, _: StopArgs) -> dict[str, Any]:
    """A person who has NOT verified (whoever they say they are) asks you to stop calling ('stop calling me',
    'stop calling this number', 'take this number off your list'). Revokes artificial-voice consent on the
    number you dialed and records the request for a person to review. It does not close the account. If they
    say it is the wrong number, that is report_wrong_number (asked_to_stop_calling true) instead. A verified
    consumer's stop request is record_flag cease (or channel_optout)."""
    if session.verified_as is not None:
        raise ToolError(
            "The person is verified: a request to stop contact is record_flag 'cease', or 'channel_optout' "
            "for one channel or number."
        )
    phone = session.phone
    assert phone is not None
    if session.number_stop == "wrong_number":
        # A wrong-number answerer who also asks to stop: the number's consent goes too, nothing else.
        async with session.db.transaction() as tx:
            await session.revoke_consent(tx, [phone])
    elif session.number_stop is None:
        async with session.db.transaction() as tx:
            await session.revoke_consent(tx, [phone])
            await session.insert_flag(tx, "unverified_stop_request", {"phone_id": phone.phone_id})
        session.number_stop = "unverified"
    return {
        "recorded": "wrong_number_stop"
        if session.number_stop == "wrong_number"
        else "unverified_stop_request",
        "allowed_now": "Calls to this number stop. Nothing more may be asked (no verification) or disclosed; this "
        "is not a stop to all contact and no letter follows. The call ends: log_contact_attempt, goodbye, end_call.",
    }


async def _cease(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    assert session.phone is not None
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "cease", {})
        await session.queue_letters(tx, "cease_confirmation")
        await session.revoke_consent(tx, [session.phone])
        await tx.update("accounts", session.context.account_id, {"status": "cease"})
    session.stop_collection("cease")
    session.outcomes.add("cease")
    return {
        "recorded": "cease",
        "allowed_now": "Collection has stopped: no amount, offer or payment request. At most one written "
        "confirmation will be mailed.",
    }


async def _channel_optout(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "channel")
    assert session.phone is not None and session.consumer is not None
    this_only = bool(args.this_number_only)
    details: dict[str, Any] = {"channel": args.channel}
    if this_only and args.channel != "email":
        details["phone_id"] = session.phone.phone_id
    phones: list[Phone] = []
    if args.channel == "call":
        # Read before the transaction: the database lock is not re-entrant, so a read inside it would hang.
        phones = [session.phone] if this_only else await session.db.phones(session.consumer.consumer_id)
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "channel_optout", details)
        await session.revoke_consent(tx, phones)
    session.outcomes.add("channel_optout")
    return {
        "recorded": "channel_optout",
        "allowed_now": "Only that channel stops; it is not a request to stop all contact.",
    }


async def _attorney(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "attorney_name", "attorney_phone")
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "attorney", {"name": args.attorney_name, "phone": args.attorney_phone})
        await tx.update("accounts", session.context.account_id, {"status": "attorney"})
    session.stop_collection("attorney")
    session.outcomes.add("attorney")
    return {"recorded": "attorney", "allowed_now": "Nothing more about the debt; the call can close."}


async def _bankruptcy(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "chapter", "case_number", "court", "filed_on")
    assert args.filed_on is not None
    async with session.db.transaction() as tx:
        await session.insert_flag(
            tx,
            "bankruptcy",
            {
                "chapter": args.chapter,
                "case_number": args.case_number,
                "court": args.court,
                "filed_on": args.filed_on.isoformat(),
            },
        )
        await tx.update("accounts", session.context.account_id, {"status": "bankruptcy"})
    session.stop_collection("bankruptcy")
    session.outcomes.add("bankruptcy")
    return {
        "recorded": "bankruptcy",
        "allowed_now": "No payment of any kind may be asked for, not even a voluntary one; the call can close.",
    }


async def _deceased(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "reported_by", "relationship")
    consumer = session.consumer
    assert consumer is not None
    died_on = _date_of_death(session, args.died_on)
    details = {
        "reported_by": args.reported_by,
        "relationship": args.relationship,
        "is_estate_rep": session.verified_as == "estate_rep",
    }
    async with session.db.transaction() as tx:
        if session.deceased_flag_id is None:
            session.deceased_flag_id = await session.insert_flag(tx, "deceased", details)
            await tx.update("accounts", session.context.account_id, {"status": "deceased"})
        elif details != session.deceased_details:
            await tx.update("account_flags", session.deceased_flag_id, {"details": details})
        if died_on is not None and died_on != session.deceased_on:
            await tx.update("consumers", consumer.consumer_id, {"deceased_on": died_on})
            session.deceased_on = died_on
    session.deceased_details = details
    session.stop_collection("deceased")
    result: dict[str, Any] = {
        "recorded": "deceased",
        "allowed_now": "No relative or heir is responsible, and nothing may imply one is. Nothing about the debt "
        "unless this person is verified as the estate's executor or administrator (verify_right_party role "
        "estate_rep).",
    }
    if session.deceased_on is None:
        result["date_of_death"] = "not recorded: if they give it, record_flag deceased again with died_on"
    return result


def _date_of_death(session: RemiCall, died_on: date | None) -> date | None:
    """A date of death the person gave: not in the future, not the call's own date, not before birth."""
    if died_on is None:
        return None
    today = session.clock.today()
    if died_on >= today:
        raise ToolError(
            f"died_on {died_on.isoformat()} is not a date of death the person gave (today is {today.isoformat()}). "
            "Use the date they said, or leave died_on out if they did not say one."
        )
    consumer = session.consumer
    assert consumer is not None
    if died_on < consumer.date_of_birth:
        raise ToolError("died_on is before the consumer was born: check the date they said.")
    return died_on


async def _scra(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "branch")
    if not session.scra_recorded:
        async with session.db.transaction() as tx:
            await session.insert_flag(tx, "scra", {"branch": args.branch})
        session.scra_recorded = True
    session.stop_collection("scra")
    return {
        "recorded": "scra",
        "allowed_now": "No pressure to pay. The agency will review their Servicemembers Civil Relief Act "
        "protections, and a person takes it from here: transfer_to_human (reason scra) is required before the "
        "call is logged or ended.",
    }


async def _hardship(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "hardship_reason")
    if session.hardship_recorded:
        return {"recorded": "hardship", "note": "Already recorded on this call."}
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "hardship", {"reason": args.hardship_reason})
    session.hardship_recorded = True
    session.outcomes.add("hardship")
    return {
        "recorded": "hardship",
        "allowed_now": "Recorded. Check the creditor's rules (get_payment_options) for what this changes. Never "
        "borrowing, a credit card or selling property, and no pressing for the full balance.",
    }


async def _original_creditor(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    creditor = session.creditor
    assert creditor is not None
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "original_creditor_request", {})
        if creditor.mailing_address is None:
            await session.queue_letters(tx, "original_creditor_info")
    session.outcomes.add("original_creditor_request")
    if creditor.mailing_address is None:
        session.stop_collection("original_creditor_request")
        return {
            "original_creditor": creditor.name,
            "mailing_address": None,
            "allowed_now": "The name may be given; the address is not on hand and will be sent in writing. "
            "Collection stops until it is provided: no amounts, offers or payment requests.",
        }
    return {"original_creditor": creditor.name, "mailing_address": creditor.mailing_address}


_HANDLERS = {
    "cease": _cease,
    "channel_optout": _channel_optout,
    "attorney": _attorney,
    "bankruptcy": _bankruptcy,
    "deceased": _deceased,
    "scra": _scra,
    "hardship": _hardship,
    "original_creditor_request": _original_creditor,
}
