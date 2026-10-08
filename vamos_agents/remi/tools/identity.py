"""Right-party verification and the debt-details gate.

Verification is decided here, not by the model: the person confirms they are
the consumer by name (the name they said, or said yes to; a first name is
enough) and gives two of date of birth, ZIP and SSN last four, which are
compared against the consumer record server-side. The facts may come one at a
time: each call to ``verify_right_party`` adds what matched, and the call
state keeps the count. The model never holds the secrets, so it cannot "read a
fact out for them to confirm". Results describe the state (what is confirmed,
what is still needed, what is allowed now), never words to say.

``get_debt_details`` is the last step of the opening. It returns the creditor,
the balance and the house limits only once the right party is verified and
the model attests that the disclosures the call owes have been given. The
attestations are tool arguments, so they appear in the trace in order. Money
is returned in dollars ("$1,430.00"), never cents.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import MAX_VERIFICATION_ATTEMPTS, RemiCall
from ..gates import Gate
from .catalog import REGISTRY

#: The verification facts, as the person may give them.
FACTS = ("date_of_birth", "zip", "ssn_last4")
FACTS_NEEDED = 2


class VerifyArgs(BaseModel):
    name: str | None = Field(
        None,
        description="The consumer's name as the person said it or said yes to (e.g. 'this is Marisol', or 'yes' to "
        "'is this Marisol?'). A first name is enough. Pass it with every call once they have said it.",
    )
    date_of_birth: date | None = Field(None, description="Date of birth the person said, as YYYY-MM-DD.")
    zip: str | None = Field(None, description="ZIP code the person said, digits only.")
    ssn_last4: str | None = Field(None, description="Last four SSN digits the person said, digits only.")
    role: Literal["consumer", "estate_rep"] = Field(
        "consumer",
        description="'consumer' for the account holder. 'estate_rep' only when the consumer has died and the "
        "person says they are the executor or administrator of the estate (only their full name is needed).",
    )


def _name(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]+", " ", folded).split())


def _digits(text: str | None) -> str | None:
    return re.sub(r"\D", "", text) if text else None


def _full_names_match(said: str, record: str) -> bool:
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


def _consumer_name_matches(said: str, record: str) -> bool:
    """The consumer's identity: the first name is enough; a surname, if said, must be the record's."""
    said_parts, record_parts = _name(said).split(), _name(record).split()
    if not said_parts or not record_parts or said_parts[0] != record_parts[0]:
        return False
    return len(said_parts) == 1 or said_parts[-1] == record_parts[-1]


def _given(args: VerifyArgs) -> dict[str, Any]:
    values = {
        "date_of_birth": args.date_of_birth,
        "zip": _digits(args.zip),
        "ssn_last4": _digits(args.ssn_last4),
    }
    return {fact: value for fact, value in values.items() if value}


@REGISTRY.tool(VerifyArgs, requires=Gate.PROCEEDING)
async def verify_right_party(session: RemiCall, args: VerifyArgs) -> dict[str, Any]:
    """Check whether the person on the line is the consumer. Call it after each answer: the name they said or
    confirmed (a first name is enough) and whichever of date of birth, ZIP code and SSN last four they have just
    said, exactly as they said it. Facts count as they come; two confirmed facts plus the name verify. The person
    chooses which facts; never insist on SSN digits and never say a value yourself. For an estate executor or
    administrator use role 'estate_rep' with their full name. The result never says which fact did not match."""
    consumer = session.consumer
    assert consumer is not None
    if session.verified_as is not None:
        return {"verified": True, "answered_by": session.verified_as, "note": "Already verified."}
    if session.number_stop is not None:
        raise ToolError(
            "This person asked you to stop calling this number, so verification is closed on this call."
        )
    if session.verification_failures >= MAX_VERIFICATION_ATTEMPTS:
        raise ToolError(
            "Verification is locked after three wrong answers: nothing may be disclosed on this call."
        )

    if args.role == "estate_rep":
        if consumer.estate_rep_name and args.name and _full_names_match(args.name, consumer.estate_rep_name):
            return await _verified(session, "estate_rep")
        return _failed(session, "The name is not the estate's executor or administrator on record.")

    given = _given(args)
    if not given and not args.name:
        raise ToolError(
            "Nothing to check: pass the name they said or confirmed and the fact they just gave. "
            "This was not counted as an attempt."
        )
    if args.name:
        if not _consumer_name_matches(args.name, consumer.full_name):
            return _failed(session, "The name is not the consumer's.")
        session.name_confirmed = True
    record = {"date_of_birth": consumer.date_of_birth, "zip": consumer.zip, "ssn_last4": consumer.ssn_last4}
    wrong = [fact for fact, value in given.items() if value != record[fact]]
    session.facts_confirmed |= {fact for fact in given if fact not in wrong}
    if wrong:
        return _failed(session, "An answer did not match the record.")
    if session.name_confirmed and len(session.facts_confirmed) >= FACTS_NEEDED:
        return await _verified(session, "consumer")
    return _progress(session)


def _progress(session: RemiCall) -> dict[str, Any]:
    """Not verified yet, nothing wrong: what is confirmed and what is still needed."""
    still = []
    if not session.name_confirmed:
        still.append("the name: the one they already said, or their yes to the consumer's first name")
    missing = FACTS_NEEDED - len(session.facts_confirmed)
    if missing > 0:
        still.append(
            f"{missing} more of date of birth, ZIP code, SSN last four (their choice, one at a time)"
        )
    return {
        "verified": False,
        "name_confirmed": session.name_confirmed,
        "facts_confirmed": len(session.facts_confirmed),
        "facts_needed": FACTS_NEEDED,
        "still_needed": still,
        "attempts_left": MAX_VERIFICATION_ATTEMPTS - session.verification_failures,
        "allowed_now": "Nothing about the debt, the creditor, money or an account until verified.",
    }


async def _verified(session: RemiCall, role: str) -> dict[str, Any]:
    session.verified_as = role
    if role == "estate_rep" and session.deceased_flag_id is not None:
        # The death was recorded before the executor was confirmed: the flag says so now.
        details = {**await session.db.flag_details(session.deceased_flag_id), "is_estate_rep": True}
        async with session.db.transaction() as tx:
            await tx.update("account_flags", session.deceased_flag_id, {"details": details})
        session.deceased_details = details
    a = session.assessment
    assert a is not None
    owed = ["the recording disclosure", "the mini-Miranda"]
    if a.time_barred:
        owed.append("the time-barred disclosure")
    if role == "consumer" and a.first_conversation:
        owed.append("the alternative-format statement with the callback number")
    return {
        "verified": True,
        "answered_by": role,
        "owed_before_the_debt": owed,
        "allowed_now": "The disclosures above, in that order and in short turns the person can react to; "
        "the debt only after get_debt_details.",
    }


def _failed(session: RemiCall, what: str) -> dict[str, Any]:
    session.verification_failures += 1
    left = MAX_VERIFICATION_ATTEMPTS - session.verification_failures
    if left <= 0:
        return {
            "verified": False,
            "attempts_left": 0,
            "allowed_now": "Verification failed three times: nothing may be disclosed. The call ends: "
            "log_contact_attempt with answered_by 'none', goodbye, end_call.",
        }
    return {
        "verified": False,
        "result": what,
        "facts_confirmed": len(session.facts_confirmed),
        "attempts_left": left,
        "allowed_now": "Nothing about the debt. They may try again; which detail was wrong stays unsaid.",
    }


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs, requires=Gate.PROCEEDING)
async def record_verification_refused(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """The person will not verify on this call (they decline to give the details, as opposed to giving details
    that do not match). Call it once they have declined; you will not ask a third time. Records the call as a
    refusal to verify, not a failed verification."""
    if session.verified_as is not None:
        raise ToolError("The person is already verified.")
    agency = session.agency
    assert agency is not None
    session.verification_refused = True
    return {
        "recorded": "refused_verification",
        "callback_number": agency.callback_number,
        "allowed_now": "No more asks, arguments or consequences, and nothing about why you called. What they "
        f"can do instead: call {agency.callback_number}, where a person answers; anything in writing goes to the "
        "address on file (never the address itself). The call then ends: log_contact_attempt with "
        "answered_by 'none', goodbye, end_call.",
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
async def get_debt_details(session: RemiCall, args: DebtDetailsArgs) -> dict[str, Any]:
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
    if a.time_barred and not args.time_barred_disclosure_given:
        missing.append("the time-barred disclosure")
    if consumer and a.first_conversation and not args.alternative_format_statement_given:
        missing.append("the alternative-format statement")
    if missing:
        raise ToolError(f"The debt comes only after these have been given: {'; '.join(missing)}.")
    session.debt_unlocked = True
    session.disclosures_attested = tuple(
        name
        for name, given in (
            ("the recording disclosure", args.recording_disclosure_given),
            ("the mini-Miranda", args.mini_miranda_given),
            ("the time-barred disclosure", args.time_barred_disclosure_given),
            ("the alternative-format statement", args.alternative_format_statement_given),
        )
        if given
    )
    debt = await session.read_debt()
    item = debt.itemization
    usd = policy.dollars
    result: dict[str, Any] = {
        "creditor": creditor.name,
        "original_account_last4": account.original_account_last4,
        "current_balance": usd(debt.balance_cents),
        "itemization": {
            "charge_off_balance": usd(item.charge_off_balance_cents),
            "interest_since_charge_off": usd(item.interest_since_charge_off_cents),
            "fees_since_charge_off": usd(item.fees_since_charge_off_cents),
            "paid_since_charge_off": usd(debt.paid_cents),
            "charge_off_on": item.charge_off_on.isoformat(),
        },
        "validation_notice_mailed_on": account.validation_notice_mailed_on.isoformat(),
        "medical_debt": creditor.kind == "medical",
        "time_barred": a.time_barred,
    }
    if consumer:
        today = session.clock.today()
        result["limits"] = {
            "settlement_one_payment_min": usd(policy.settlement_floor_cents(account, debt.balance_cents, 1)),
            "settlement_instalments_min": usd(policy.settlement_floor_cents(account, debt.balance_cents, 2)),
            "settlement_max_instalments": policy.SETTLEMENT_INSTALMENTS_MAX,
            "plan_months": [policy.PLAN_MIN_MONTHS, account.max_plan_months],
            "plan_min_instalment": usd(account.min_instalment_cents),
            "hardship_plan_months_max": policy.HARDSHIP_MAX_MONTHS,
            "hardship_min_instalment": usd(policy.HARDSHIP_MIN_INSTALMENT_CENTS),
            "first_payment_by": (today + timedelta(days=policy.PLAN_FIRST_DUE_MAX_DAYS)).isoformat(),
            "promise_by": (today + timedelta(days=policy.PTP_MAX_DAYS)).isoformat(),
            "today": today.isoformat(),
        }
    else:
        result["note"] = (
            "This is the estate's representative: the debt may be stated, but no payment may be asked for."
        )
    return result
