"""Right-party verification, and the debt. Facts only.

Verification is checked here, not by the model: the person's name (the name
they said, or said yes to; a first name is enough) and two of date of birth,
ZIP and SSN last four are compared against the consumer record server-side, so
the model never holds the secrets and cannot "read a fact out for them to
confirm". Facts may come one at a time: each call to ``verify_right_party``
adds what matched. The result is what the record shows (whether this answer
matched, what is confirmed so far, how many wrong answers there have been),
never which fact failed, and never a verdict on what the agent may do next:
nothing locks after wrong answers and nothing is disclosed for the agent. The
disclosures and their wording are the prompt's (manual section 8).

``get_debt_details`` reads the debt from the ledger: the creditor, the balance,
its itemization from its itemization date, and whether the debt is past its time to sue (a
fact read from the account's date). Money is returned in dollars
("$1,430.00"), never cents.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall
from .catalog import REGISTRY
from .connect import iso

#: The verification facts, as the person may give them.
FACTS = ("date_of_birth", "zip", "ssn_last4")
FACTS_NEEDED = 2


class VerifyArgs(BaseModel):
    name: str | None = Field(
        None,
        description="The consumer's name as the person said it or said yes to (e.g. 'this is Marisol', or 'yes' to "
        "'is this Marisol?'). A first name matches the record's first name.",
    )
    date_of_birth: date | None = Field(None, description="Date of birth the person said, as YYYY-MM-DD.")
    zip: str | None = Field(None, description="ZIP code the person said, digits only.")
    ssn_last4: str | None = Field(None, description="Last four SSN digits the person said, digits only.")
    role: Literal["consumer", "estate_rep"] = Field(
        "consumer",
        description="'consumer' for the account holder. 'estate_rep' when the person says they are the executor or "
        "administrator of a deceased consumer's estate (their full name is checked).",
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


@REGISTRY.tool(VerifyArgs)
async def verify_right_party(session: RemiCall, args: VerifyArgs) -> dict[str, Any]:
    """Checks what the person on the line said against the consumer record: the name they said or confirmed (a
    first name matches) and any of date of birth, ZIP code and SSN last four. Facts count as they come; the name
    plus two matching facts verify. With role 'estate_rep', an estate executor's or administrator's full name is
    checked. The result says whether this answer matched and what is confirmed so far, never which fact did not
    match."""
    await session.ensure_account()
    consumer = session.consumer
    assert consumer is not None
    if session.verified_as is not None:
        return {"verified": True, "role": session.verified_as}
    if args.role == "estate_rep":
        if consumer.estate_rep_name and args.name and _full_names_match(args.name, consumer.estate_rep_name):
            return await _verified(session, "estate_rep")
        return _answer(session, matched=False)
    given = _given(args)
    if not given and not args.name:
        raise ToolError("Nothing to check: no name, date of birth, ZIP code or SSN last four was passed.")
    if args.name:
        if not _consumer_name_matches(args.name, consumer.full_name):
            return _answer(session, matched=False)
        session.name_confirmed = True
    record = {"date_of_birth": consumer.date_of_birth, "zip": consumer.zip, "ssn_last4": consumer.ssn_last4}
    wrong = [fact for fact, value in given.items() if value != record[fact]]
    session.facts_confirmed |= {fact for fact in given if fact not in wrong}
    if wrong:
        return _answer(session, matched=False)
    if session.name_confirmed and len(session.facts_confirmed) >= FACTS_NEEDED:
        return await _verified(session, "consumer")
    return _answer(session, matched=True)


def _answer(session: RemiCall, *, matched: bool) -> dict[str, Any]:
    """Not verified (yet): whether this answer matched the record, and what is confirmed so far."""
    if not matched:
        session.wrong_answers += 1
    return {
        "verified": False,
        "answer_matched": matched,
        "name_confirmed": session.name_confirmed,
        "facts_confirmed": len(session.facts_confirmed),
        "facts_needed": FACTS_NEEDED,
        "wrong_answers": session.wrong_answers,
    }


async def _verified(session: RemiCall, role: str) -> dict[str, Any]:
    session.verified_as = role
    if role == "estate_rep" and session.deceased_flag_id is not None:
        # The death was recorded before the executor was confirmed: the flag says so now.
        details = {**await session.db.flag_details(session.deceased_flag_id), "is_estate_rep": True}
        async with session.db.transaction() as tx:
            await tx.update("account_flags", session.deceased_flag_id, {"details": details})
        session.deceased_details = details
    return {"verified": True, "role": role}


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs)
async def record_verification_refused(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Records that the person declined to verify on this call (they will not give the details, as opposed to
    giving details that do not match). The call's record shows it as a refusal to verify."""
    session.verification_refused = True
    return {"recorded": "refused_verification"}


@REGISTRY.tool(NoArgs)
async def get_debt_details(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Reads the debt: the creditor, the original account's last four digits, the current balance, its
    itemization from its itemization date (itemized_from names it: charge_off, or a hospital account's
    last_statement; the balance on that date, interest and fees since, payments since), the validation
    notice's date, and the end of the time to sue. The screen pop read it when the call connected
    ("Account on screen")."""
    account = await session.ensure_account()
    creditor = session.creditor
    assert creditor is not None
    debt = await session.read_debt()
    item = debt.itemization
    usd = policy.dollars
    return {
        "creditor": creditor.name,
        "original_account_last4": account.original_account_last4,
        "current_balance": usd(debt.balance_cents),
        "itemization": {
            "itemized_from": item.itemized_from,
            "itemized_on": iso(item.itemized_on),
            "itemized_balance": usd(item.itemized_balance_cents),
            "interest_since": usd(item.interest_since_cents),
            "fees_since": usd(item.fees_since_cents),
            "paid_since": usd(debt.paid_cents),
        },
        "last_payment_on": iso(debt.last_payment_on),
        "validation_notice_mailed_on": iso(account.validation_notice_mailed_on),
        "time_to_sue_ends": iso(account.sol_expires_on),
        "time_barred": policy.time_barred(account, session.clock.today()),
    }
