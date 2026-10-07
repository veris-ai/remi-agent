"""Right-party verification, the language preference, and the debt-details gate.

Verification is decided here, not by the model: the person must give their
full name and two of date of birth, ZIP and SSN last four, and those are
compared against the consumer record server-side. The model never holds the
secrets, so it cannot "read a fact out for them to confirm".

``get_debt_details`` is the last step of the opening. It returns the creditor,
the balance and the house limits only once the right party is verified and
the model attests that the disclosures the call owes have been given. The
attestations are tool arguments, so they appear in the trace in order.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import policy
from ..session import MAX_VERIFICATION_ATTEMPTS, CallSession
from ..tool_registry import Gate, ToolError
from .catalog import REGISTRY


class VerifyArgs(BaseModel):
    full_name: str = Field(description="The full name the person gave, exactly as they said it.")
    date_of_birth: date | None = Field(None, description="Date of birth the person said, as YYYY-MM-DD.")
    zip: str | None = Field(None, description="ZIP code the person said, digits only.")
    ssn_last4: str | None = Field(None, description="Last four SSN digits the person said, digits only.")
    role: Literal["consumer", "estate_rep"] = Field(
        "consumer",
        description="'consumer' for the account holder. 'estate_rep' only when the consumer has died and the "
        "person says they are the executor or administrator of the estate (only full_name is needed).",
    )


def _name(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]+", " ", folded).split())


def _digits(text: str | None) -> str | None:
    return re.sub(r"\D", "", text) if text else None


def _names_match(said: str, record: str) -> bool:
    """Full name: every recorded name part, in order, at the start and end of what was said
    (a middle name in between is fine)."""
    said_parts, record_parts = _name(said).split(), _name(record).split()
    if not record_parts or len(said_parts) < len(record_parts):
        return False
    return (
        said_parts[0] == record_parts[0]
        and said_parts[-1] == record_parts[-1]
        and (len(record_parts) <= 2 or said_parts[: len(record_parts)] == record_parts)
    )


@REGISTRY.tool(VerifyArgs, requires=Gate.PROCEEDING)
async def verify_right_party(session: CallSession, args: VerifyArgs) -> dict[str, Any]:
    """Check whether the person on the line is the consumer: pass the full name they gave and at least two of
    date of birth, ZIP code and SSN last four, exactly as they said them. Ask for these; never say any value
    yourself. For an estate executor or administrator use role 'estate_rep' with their full name. Never tell
    the person which detail did not match."""
    consumer = session.consumer
    assert consumer is not None
    if session.verified_as is not None:
        return {"verified": True, "answered_by": session.verified_as, "note": "Already verified."}
    if session.verification_failures >= MAX_VERIFICATION_ATTEMPTS:
        raise ToolError(
            "Verification is locked after three failed attempts. Disclose nothing and end the call."
        )

    if args.role == "estate_rep":
        if consumer.estate_rep_name and _names_match(args.full_name, consumer.estate_rep_name):
            return await _verified(session, "estate_rep")
        return _failed(
            session, "That name does not match the estate record. Treat this person as a third party."
        )

    given = {
        "date_of_birth": args.date_of_birth is not None,
        "zip": bool(_digits(args.zip)),
        "ssn_last4": bool(_digits(args.ssn_last4)),
    }
    if sum(given.values()) < 2:
        raise ToolError(
            "Ask for two of: date of birth, ZIP code, last four digits of SSN. This was not counted as an attempt."
        )
    matches = sum(
        (
            args.date_of_birth == consumer.date_of_birth,
            _digits(args.zip) == consumer.zip,
            _digits(args.ssn_last4) == consumer.ssn_last4,
        )
    )
    if _names_match(args.full_name, consumer.full_name) and matches >= 2:
        return await _verified(session, "consumer")
    return _failed(
        session, "The details do not match. Do not say which one; you may ask the person to repeat them."
    )


async def _verified(session: CallSession, role: str) -> dict[str, Any]:
    session.verified_as = role
    if role == "estate_rep" and session.deceased_flag_id is not None:
        # The death was recorded before the executor was confirmed: the flag says so now.
        details = await session.db.flag_details(session.deceased_flag_id)
        async with session.db.transaction() as tx:
            await tx.update(
                "account_flags", session.deceased_flag_id, {"details": {**details, "is_estate_rep": True}}
            )
    a = session.assessment
    assert a is not None
    owed = ["the recording disclosure", "the mini-Miranda"]
    if role == "consumer" and a.language_needed:
        owed.append("ask which language they prefer, then set_language")
    if a.time_barred:
        owed.append("the time-barred disclosure")
    if role == "consumer" and a.first_conversation:
        owed.append("the alternative-format statement with the callback number")
    return {
        "verified": True,
        "answered_by": role,
        "next": f"Now give, in this order: {'; '.join(owed)}. Then call get_debt_details.",
    }


def _failed(session: CallSession, message: str) -> dict[str, Any]:
    session.verification_failures += 1
    left = MAX_VERIFICATION_ATTEMPTS - session.verification_failures
    if left <= 0:
        return {
            "verified": False,
            "attempts_left": 0,
            "next": "Verification failed three times. Disclose nothing, end the call politely, then log_contact_attempt "
            "with answered_by 'none' and end_call.",
        }
    return {"verified": False, "attempts_left": left, "next": message}


class LanguageArgs(BaseModel):
    language: str = Field(
        description="ISO 639-1 code of the language the consumer prefers, e.g. 'en' or 'es'."
    )


@REGISTRY.tool(LanguageArgs, requires=Gate.VERIFIED)
async def set_language(session: CallSession, args: LanguageArgs) -> dict[str, Any]:
    """Record the consumer's preferred language after asking them (only when none is on record)."""
    session.require_consumer()
    code = args.language.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        raise ToolError("Use a two-letter language code such as 'en' or 'es'.")
    assert session.consumer is not None
    async with session.db.transaction() as tx:
        await tx.update("consumers", session.consumer.consumer_id, {"language_preference": code})
    session.language_recorded = True
    if code == "en":
        return {"recorded": code}
    return {
        "recorded": code,
        "next": "Offer to continue in that language or to arrange it. Never give a partial or inaccurate "
        "translation of a required disclosure.",
    }


class DebtDetailsArgs(BaseModel):
    recording_disclosure_given: bool = Field(
        description="You said the call is recorded and may be used in connection with collecting a debt."
    )
    mini_miranda_given: bool = Field(
        description="You said Tallmere Account Services is a debt collector, this is an attempt to collect a debt, and information will be used for that purpose."
    )
    time_barred_disclosure_given: bool = Field(
        False, description="You gave the time-barred disclosure (only if the debt is time-barred)."
    )
    alternative_format_statement_given: bool = Field(
        False,
        description="You said communications are available in alternative formats such as large print and gave the callback number (first conversation only).",
    )


@REGISTRY.tool(DebtDetailsArgs, requires=Gate.VERIFIED)
async def get_debt_details(session: CallSession, args: DebtDetailsArgs) -> dict[str, Any]:
    """After verification and the required disclosures, read the debt: creditor, original account last four,
    balance, itemization, and the settlement and plan limits you may offer. Attest truthfully to what you
    have already said; this is refused until every disclosure the call owes has been given."""
    account, creditor, a = session.account, session.creditor, session.assessment
    assert account is not None and creditor is not None and a is not None
    consumer = session.verified_as == "consumer"
    missing = []
    if not args.recording_disclosure_given:
        missing.append("the recording disclosure")
    if not args.mini_miranda_given:
        missing.append("the mini-Miranda")
    if consumer and a.language_needed and not session.language_recorded:
        missing.append("the language question (then set_language)")
    if a.time_barred and not args.time_barred_disclosure_given:
        missing.append("the time-barred disclosure")
    if consumer and a.first_conversation and not args.alternative_format_statement_given:
        missing.append("the alternative-format statement")
    if missing:
        raise ToolError(f"Give these first: {'; '.join(missing)}.")
    session.debt_unlocked = True
    balance = account.current_balance_cents
    result: dict[str, Any] = {
        "creditor": creditor.name,
        "original_account_last4": account.original_account_last4,
        "current_balance_cents": balance,
        "itemization": {
            "charge_off_balance_cents": account.charge_off_balance_cents,
            "interest_since_charge_off_cents": account.interest_since_charge_off_cents,
            "fees_since_charge_off_cents": account.fees_since_charge_off_cents,
            "paid_since_charge_off_cents": account.paid_since_charge_off_cents,
            "charge_off_on": account.charge_off_on.isoformat(),
        },
        "validation_notice_mailed_on": account.validation_notice_mailed_on.isoformat(),
        "medical_debt": creditor.kind == "medical",
        "time_barred": a.time_barred,
    }
    if consumer:
        today = session.clock.today()
        result["limits"] = {
            "settlement_one_payment_min_cents": policy.settlement_floor_cents(account, 1),
            "settlement_instalments_min_cents": policy.settlement_floor_cents(account, 2),
            "settlement_max_instalments": policy.SETTLEMENT_INSTALMENTS_MAX,
            "plan_months": [policy.PLAN_MIN_MONTHS, account.max_plan_months],
            "plan_min_instalment_cents": account.min_instalment_cents,
            "hardship_plan_months_max": policy.HARDSHIP_MAX_MONTHS,
            "hardship_min_instalment_cents": policy.HARDSHIP_MIN_INSTALMENT_CENTS,
            "first_payment_by": (today + timedelta(days=policy.PLAN_FIRST_DUE_MAX_DAYS)).isoformat(),
            "promise_by": (today + timedelta(days=policy.PTP_MAX_DAYS)).isoformat(),
            "today": today.isoformat(),
        }
    else:
        result["note"] = (
            "You are speaking with the estate's representative: state the debt only; ask for no payment."
        )
    return result
