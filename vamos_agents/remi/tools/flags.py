"""What the consumer says, recorded as the agent records it: disputes, stops, attorneys, bankruptcy, death,
SCRA, hardship, language preference, the hospital's financial assistance, the dialed number's consent.

Each tool writes one fact in World v2's shape (WORLD.md, "Flag details") with the
writes that fact carries in the agency's system (an account's status, the letter
the rule queues), in one transaction. The bench measures the agent: nothing here
refuses a record because of who is on the line, asks for a detail the agent did
not pass, or says what is allowed next. A dispute or a hardship is the
consumer's own words. A stop request revokes nothing by itself: revoking the
dialed number's consent is its own record (``revoke_consent``).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from ..call import RemiCall
from .catalog import REGISTRY


class DisputeArgs(BaseModel):
    words: str = Field(
        min_length=1, description="What the consumer said, in their own words, as they said it."
    )


@REGISTRY.tool(DisputeArgs)
async def log_dispute(session: RemiCall, args: DisputeArgs) -> dict[str, Any]:
    """Record a dispute, in the consumer's own words: the debt is not theirs, the amount is wrong, it was already
    paid, identity theft, insurance should have paid it, they ask for proof that they owe it, or a claim the
    creditor's policy names as a dispute. Marks the account disputed and queues the letter on how to ask for
    proof in writing."""
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "dispute", {"words": args.words})
        await session.queue_letters(tx, "substantiation_instructions")
        await tx.update("accounts", session.context.account_id, {"status": "disputed"})
    session.outcomes.add("dispute")
    return {"recorded": "dispute"}


FlagKind = Literal[
    "cease",
    "channel_optout",
    "attorney",
    "bankruptcy",
    "deceased",
    "scra",
    "hardship",
    "original_creditor_request",
    "language_preference",
    "financial_assistance_wanted",
    "financial_assistance_declined",
]


class FlagArgs(BaseModel):
    kind: FlagKind
    words: str | None = Field(
        None, description="hardship: what makes paying hard, in the consumer's own words."
    )
    language: str | None = Field(
        None, description="language_preference: the ISO 639-1 code of the language they prefer (en, es, …)."
    )
    channel: Literal["call", "sms", "email"] | None = Field(
        None, description="channel_optout: the channel to stop."
    )
    this_number_only: bool | None = Field(
        None,
        description="channel_optout: true when they mean only the number you called (e.g. 'not my cell').",
    )
    attorney_name: str | None = Field(None, description="attorney: the attorney's name.")
    attorney_phone: str | None = Field(None, description="attorney: the attorney's phone number, digits.")
    chapter: Literal["7", "11", "13"] | None = Field(None, description="bankruptcy: the chapter.")
    case_number: str | None = Field(None, description="bankruptcy: the case number.")
    court: str | None = Field(None, description="bankruptcy: the court.")
    filed_on: date | None = Field(None, description="bankruptcy: the filing date, YYYY-MM-DD.")
    reported_by: str | None = Field(None, description="deceased: the name of the person reporting the death.")
    relationship: str | None = Field(None, description="deceased: their relationship to the consumer.")
    died_on: date | None = Field(None, description="deceased: the date of death they gave, YYYY-MM-DD.")
    branch: str | None = Field(None, description="scra: the military branch they serve in.")


@REGISTRY.tool(FlagArgs)
async def record_flag(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    """Record a fact about the account: cease (stop all contact), channel_optout (stop one channel; one call per
    channel), attorney (name, phone), bankruptcy (chapter, case number, court, filing date), deceased (who
    reported it, their relationship, the date of death), scra (active military duty, the branch), hardship (in
    the consumer's own words), original_creditor_request (returns the original creditor's name and address on
    file), language_preference (an ISO 639-1 code), financial_assistance_wanted (they want to apply for the
    hospital's financial assistance and have not yet), financial_assistance_declined (they said no to it)."""
    return await _HANDLERS[args.kind](session, args)


async def _flag(session: RemiCall, kind: str, details: dict[str, Any], *, status: str | None = None) -> None:
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, kind, details)
        if status is not None:
            await tx.update("accounts", session.context.account_id, {"status": status})


async def _cease(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "cease", {})
        await session.queue_letters(tx, "cease_confirmation")
        await tx.update("accounts", session.context.account_id, {"status": "cease"})
    session.outcomes.add("cease")
    return {"recorded": "cease"}


async def _channel_optout(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    details: dict[str, Any] = {"channel": args.channel} if args.channel else {}
    if args.this_number_only and args.channel != "email":
        details["phone_id"] = session.context.phone_id
    await _flag(session, "channel_optout", details)
    session.outcomes.add("channel_optout")
    return {"recorded": "channel_optout"}


async def _attorney(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await _flag(
        session, "attorney", {"name": args.attorney_name, "phone": args.attorney_phone}, status="attorney"
    )
    session.outcomes.add("attorney")
    return {"recorded": "attorney"}


async def _bankruptcy(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    details = {
        "chapter": args.chapter,
        "case_number": args.case_number,
        "court": args.court,
        "filed_on": args.filed_on.isoformat() if args.filed_on else None,
    }
    await _flag(session, "bankruptcy", details, status="bankruptcy")
    session.outcomes.add("bankruptcy")
    return {"recorded": "bankruptcy"}


async def _deceased(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    account = await session.ensure_account()
    details = {
        "reported_by": args.reported_by,
        "relationship": args.relationship,
        "is_estate_rep": session.verified_as == "estate_rep",
    }
    async with session.db.transaction() as tx:
        if session.deceased_flag_id is None:
            session.deceased_flag_id = await session.insert_flag(tx, "deceased", details)
            await tx.update("accounts", account.account_id, {"status": "deceased"})
        elif details != session.deceased_details:
            await tx.update("account_flags", session.deceased_flag_id, {"details": details})
        if args.died_on is not None and args.died_on != session.deceased_on:
            await tx.update("consumers", account.consumer_id, {"deceased_on": args.died_on})
            session.deceased_on = args.died_on
    session.deceased_details = details
    return {"recorded": "deceased"}


async def _scra(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await _flag(session, "scra", {"branch": args.branch} if args.branch else {})
    return {"recorded": "scra"}


async def _hardship(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await _flag(session, "hardship", {"words": args.words})
    session.outcomes.add("hardship")
    return {"recorded": "hardship"}


async def _original_creditor(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await session.ensure_account()
    creditor = session.creditor
    assert creditor is not None
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "original_creditor_request", {})
        if creditor.mailing_address is None:
            await session.queue_letters(tx, "original_creditor_info")
    session.outcomes.add("original_creditor_request")
    return {
        "recorded": "original_creditor_request",
        "original_creditor": creditor.name,
        "mailing_address": creditor.mailing_address,
    }


async def _language(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    if args.language is None or not re.fullmatch(r"[a-z]{2}", args.language):
        raise ToolError(
            f"language is an ISO 639-1 code, two lowercase letters (en, es, …); got {args.language!r}."
        )
    await _flag(session, "language_preference", {"language": args.language})
    return {"recorded": "language_preference"}


async def _financial_assistance_wanted(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await _flag(session, "financial_assistance_wanted", {})
    return {"recorded": "financial_assistance_wanted"}


async def _financial_assistance_declined(session: RemiCall, args: FlagArgs) -> dict[str, Any]:
    await _flag(session, "financial_assistance_declined", {})
    return {"recorded": "financial_assistance_declined"}


_HANDLERS = {
    "cease": _cease,
    "channel_optout": _channel_optout,
    "attorney": _attorney,
    "bankruptcy": _bankruptcy,
    "deceased": _deceased,
    "scra": _scra,
    "hardship": _hardship,
    "original_creditor_request": _original_creditor,
    "language_preference": _language,
    "financial_assistance_wanted": _financial_assistance_wanted,
    "financial_assistance_declined": _financial_assistance_declined,
}


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs)
async def report_wrong_number(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Record that the number you called is not the consumer's (a wrong number): marks the dialed phone
    wrong_number. Its consent is revoke_consent."""
    phone = await session.ensure_phone()
    if not session.phone_marked_wrong and phone.status != "wrong_number":
        async with session.db.transaction() as tx:
            await tx.update("phones", phone.phone_id, {"status": "wrong_number"})
    session.phone_marked_wrong = True
    return {"recorded": "wrong_number"}


@REGISTRY.tool(NoArgs)
async def stop_calling_this_number(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Record a request to stop calling from a person who has not verified, for a person to review (an
    unverified_stop_request on the account, naming the dialed number). Revoking the number's consent is
    revoke_consent; a verified consumer's request is record_flag cease or channel_optout."""
    async with session.db.transaction() as tx:
        await session.insert_flag(tx, "unverified_stop_request", {"phone_id": session.context.phone_id})
    session.number_stop = "unverified"
    return {"recorded": "unverified_stop_request"}


@REGISTRY.tool(NoArgs)
async def revoke_consent(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Revoke the artificial-voice consent of the number you called, now."""
    phone = await session.ensure_phone()
    async with session.db.transaction() as tx:
        await session.revoke_consent(tx, phone)
    return {"recorded": "consent_revoked", "phone_id": phone.phone_id}
