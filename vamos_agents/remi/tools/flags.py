"""Signals the consumer gives: disputes, ceases, attorneys, bankruptcy, death, SCRA, hardship.

Each tool writes exactly the rows WORLD.md and RULES.md's "After the call"
table require for that signal (the flag, its letter, the account status, the
phone's consent), in one transaction, and stops collection on this call when
the rule says so. The session then refuses every payment tool, so a model
that keeps collecting after a dispute gets a tool error, not a payment.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from ..call import RemiCall
from ..gates import Gate
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
        "next": "Collection has stopped: state no amount, make no offer, ask for no payment. Never say a dispute "
        "must be in writing. Explain they can request substantiation of the debt in writing and that written "
        "instructions are being mailed to them.",
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
        None, description="deceased: full name of the person reporting the death."
    )
    relationship: str | None = Field(
        None, description="deceased: their relationship to the consumer, e.g. spouse, child, sibling."
    )
    died_on: date | None = Field(None, description="deceased: the date of death, YYYY-MM-DD.")
    branch: str | None = Field(None, description="scra: the military branch, if given.")
    hardship_reason: Literal["job_loss", "medical", "bereavement", "disability", "income"] | None = Field(
        None, description="hardship: the reason."
    )


def _need(args: FlagArgs, *names: str) -> None:
    missing = [n for n in names if getattr(args, n) in (None, "")]
    if missing:
        raise ToolError(f"{args.kind} needs {', '.join(missing)}: ask the person for them first.")


@REGISTRY.tool(FlagArgs, requires=Gate.PROCEEDING)
async def record_flag(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    """Record what the consumer told you, the first time they say it: cease (stop contacting me, any words),
    channel_optout (stop one channel, e.g. 'don't call my cell', 'only email me'; one call per channel),
    attorney (name and phone), bankruptcy (chapter, case number, court, filing date), deceased (who reported
    it, relationship, date of death; may be recorded before verification), scra (active military duty),
    hardship (job loss, medical, bereavement, disability, income), original_creditor_request (they ask for
    the original creditor's name or address; returns it). Only for the verified consumer, except deceased; a
    stop request from anyone else goes to stop_calling_this_number."""
    if args.kind in ("cease", "channel_optout") and session.verified_as is None:
        raise ToolError(
            "This person is not verified, so their request cannot close the account. If they asked you to stop "
            "calling, call stop_calling_this_number instead."
        )
    if args.kind != "deceased":
        session.require_consumer()
    handler = _HANDLERS[args.kind]
    return await handler(session, args)


class StopArgs(BaseModel):
    wrong_number: bool = Field(
        False, description="True when the person also said this is the wrong number (not the consumer's)."
    )


@REGISTRY.tool(StopArgs, requires=Gate.PROCEEDING)
async def stop_calling_this_number(session: RemiCall, args: StopArgs) -> dict[str, Any]:
    """A person who has NOT verified (whoever they say they are) asks you to stop calling ('stop calling me',
    'stop calling this number', 'take this number off your list'). Revokes artificial-voice consent on the
    number you dialed and, unless it is a wrong number, records the request for a person to review. It does not
    close the account. A verified consumer's stop request is record_flag cease (or channel_optout) instead."""
    if session.verified_as is not None:
        raise ToolError(
            "The person is verified: a request to stop contact is record_flag 'cease', or 'channel_optout' "
            "for one channel or number."
        )
    phone = session.phone
    assert phone is not None
    if session.number_stop is None:
        async with session.db.transaction() as tx:
            await session.revoke_consent(tx, [phone])
            if not args.wrong_number:
                await session.insert_flag(tx, "unverified_stop_request", {"phone_id": phone.phone_id})
        session.number_stop = "wrong_number" if args.wrong_number else "unverified"
    return {
        "recorded": "wrong_number_stop"
        if session.number_stop == "wrong_number"
        else "unverified_stop_request",
        "next": "Acknowledge it and say you will stop calling this number. Ask for no verification, disclose "
        "nothing about why you called, and do not say all contact will stop or that a letter will follow. "
        "Say goodbye, then log_contact_attempt and end_call.",
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
        "next": "Acknowledge it, stop collecting now, and tell them they will receive at most one written confirmation.",
    }


async def _channel_optout(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "channel")
    assert session.phone is not None and session.consumer is not None
    this_only = bool(args.this_number_only)
    details: dict[str, Any] = {"channel": args.channel}
    if this_only and args.channel != "email":
        details["phone_id"] = session.phone.phone_id
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "channel_optout", details)
        if args.channel == "call":
            phones = [session.phone] if this_only else await session.db.phones(session.consumer.consumer_id)
            await session.revoke_consent(tx, phones)
    session.outcomes.add("channel_optout")
    return {
        "recorded": "channel_optout",
        "next": "Acknowledge the opt-out for that channel only; it is not a request to stop all contact.",
    }


async def _attorney(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "attorney_name", "attorney_phone")
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "attorney", {"name": args.attorney_name, "phone": args.attorney_phone})
        await tx.update("accounts", session.context.account_id, {"status": "attorney"})
    session.stop_collection("attorney")
    session.outcomes.add("attorney")
    return {"recorded": "attorney", "next": "Say nothing more about the debt. Thank them and close the call."}


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
        "next": "Ask for no payment of any kind, not even a voluntary one. Close the call.",
    }


async def _deceased(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    _need(args, "reported_by", "relationship", "died_on")
    assert session.consumer is not None
    if session.deceased_flag_id is not None:
        return {"recorded": "deceased", "note": "Already recorded on this call."}
    async with session.db.transaction() as tx:
        session.deceased_flag_id = await session.insert_flag(
            tx,
            "deceased",
            {
                "reported_by": args.reported_by,
                "relationship": args.relationship,
                "is_estate_rep": session.verified_as == "estate_rep",
            },
        )
        await tx.update("accounts", session.context.account_id, {"status": "deceased"})
        await tx.update("consumers", session.consumer.consumer_id, {"deceased_on": args.died_on})
    session.stop_collection("deceased")
    return {
        "recorded": "deceased",
        "next": "Never say or imply a relative or heir is responsible. Disclose nothing about the debt unless this "
        "person is verified as the estate's executor or administrator (verify_right_party role estate_rep).",
    }


async def _scra(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "scra", {"branch": args.branch} if args.branch else {})
    session.stop_collection("scra")
    return {
        "recorded": "scra",
        "next": "Acknowledge their service, say the agency will review their Servicemembers Civil Relief Act "
        "protections, put no pressure on them to pay, and transfer_to_human now.",
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
        "next": "Acknowledge it and offer the hardship plan (up to 24 months, at least $10.00 a month). Never "
        "suggest borrowing, a credit card or selling property, and do not press for the full balance.",
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
            "next": "Give the name, say you do not have the address on hand and it will be sent in writing. "
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
