"""Connect-time reads: the account, the dialed phone, the contact history. Facts only.

Remi's screen pop (``vamos_agents.remi.screen_pop``) runs these handlers, and
``get_debt_details``, when the call connects, before any vendor session opens, so
the account is on screen before anyone speaks. They stay tools so the model can
re-read. They answer what the World holds, never a verdict: no tool decides
whether the call may continue, counts contacts against a limit or says what
comes next; the agent decides that from the facts (World v2 WORLD.md, "No
``dialer_may_call``").

Nothing here returns the verification secrets (date of birth, ZIP, SSN, street):
the verifier compares those server-side, so the model cannot read them out.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .. import policy
from ..call import AGENT_NAME, RemiCall
from ..context import EASTERN
from .catalog import REGISTRY


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")


def iso(value: date | datetime | None) -> str | None:
    """A date as ISO; an instant as ISO in Eastern time (the agency's and every consumer's)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(EASTERN).isoformat()
    return value.isoformat()


@REGISTRY.tool(NoArgs)
async def get_account(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Reads the account being called (from the dialer's context): the consumer's name, the account's status
    and creditor, its dates (the loan's date, the end of the time to sue, the notices mailed) and its own facts
    (a phone account's balance parts, a hospital account's treatment date, an auto deficiency's car and sale).
    The dialer already ran it when the call connected (see "Account on screen"); call it again to re-read."""
    account = await session.load_account()
    consumer, creditor, agency = session.consumer, session.creditor, session.agency
    assert consumer is not None and creditor is not None and agency is not None
    result: dict[str, Any] = {
        "account_id": account.account_id,
        "consumer_full_name": consumer.full_name,
        "account_status": account.status,
        "creditor": creditor.name,
    }
    if creditor.store_name:
        result["store_name"] = creditor.store_name
    result |= {
        "original_account_last4": account.original_account_last4,
        "placed_on": iso(account.placed_on),
        "time_to_sue_ends": iso(account.sol_expires_on),
        "time_barred": policy.time_barred(account, session.clock.today()),
        "validation_notice_mailed_on": iso(account.validation_notice_mailed_on),
        "time_barred_notice_mailed_on": iso(account.time_barred_notice_mailed_on),
    }
    if account.balance_phone_cents is not None and account.balance_service_cents is not None:
        result["balance_parts"] = {
            "phone": policy.dollars(account.balance_phone_cents),
            "service": policy.dollars(account.balance_service_cents),
        }
    if account.treatment_on is not None:
        result["treatment_on"] = iso(account.treatment_on)
    if account.car is not None and account.car_sale_price_cents is not None:
        result["car"] = {
            "car": account.car,
            "repossessed_on": iso(account.car_repossessed_on),
            "sold_on": iso(account.car_sold_on),
            "sale_price": policy.dollars(account.car_sale_price_cents),
            "lender_explanation_sent_on": iso(account.deficiency_explanation_sent_on),
        }
    result["you"] = {
        "agent_name": AGENT_NAME,
        "business_name": agency.business_name,
        "callback_number": agency.callback_number,
        "callback_contact": agency.callback_contact,
    }
    return result


@REGISTRY.tool(NoArgs)
async def get_dialed_phone(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Reads the phone number the dialer called: its line type and source, its artificial-voice consent and when
    it was revoked (if it was), and its status (valid, wrong_number, do_not_call)."""
    phone = await session.load_phone()
    return {
        "phone_id": phone.phone_id,
        "line_type": phone.line_type,
        "source": phone.source,
        "artificial_voice_consent": phone.artificial_voice_consent,
        "consent_revoked_at": iso(phone.consent_revoked_at),
        "status": phone.status,
    }


@REGISTRY.tool(NoArgs)
async def get_contact_history(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Reads the account's contact history as recorded: every earlier call, voicemail, text, email and letter,
    when (Eastern time), which way, who answered, whether the consumer responded, and its summary."""
    history = await session.db.contact_history(session.context.account_id)
    session.history = history
    return {
        "attempts": [
            {
                "at": iso(a.at),
                "medium": a.medium,
                "direction": a.direction,
                "answered_by": a.answered_by,
                "consumer_responded": a.consumer_responded,
                "disposition": a.disposition,
                "summary": a.summary,
            }
            for a in history
        ]
    }
