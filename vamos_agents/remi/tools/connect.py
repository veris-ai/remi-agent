"""Connect-time reads: the account, the dialed phone, the contact history.

RULES.md step 1, and the grader's first check: the tool trace must show these
three reads before Remi asks for anyone by name. They are separate tools on
purpose, so each read is visible in the trace on its own. Once all three have
run, the result of the last one carries the call's ``call_assessment``:
whether the call may proceed and what its opening owes.

``get_account`` returns nothing a third party could learn the debt from and
none of the verification secrets (date of birth, ZIP, SSN, street): the
verifier compares those server-side, so the model cannot read them out.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from .. import policy
from ..call import AGENT_NAME, Assessment, RemiCall
from ..gates import Gate
from .catalog import REGISTRY


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")


@REGISTRY.tool(NoArgs, requires=Gate.NONE)
async def get_account(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Connect-time read 1 of 3. Reads the account being called (from the dialer's context): the consumer's
    name, the account status and whether the debt is time-barred. Call
    get_account, get_dialed_phone and get_contact_history before saying anything past a greeting."""
    db, ctx = session.db, session.context
    session.agency = await db.agency()
    account = await db.account(ctx.account_id)
    session.consumer = await db.consumer(account.consumer_id)
    session.creditor = await db.creditor(account.creditor_id)
    session.account = account
    consumer, agency = session.consumer, session.agency
    result: dict[str, Any] = {
        "account_id": account.account_id,
        "consumer_full_name": consumer.full_name,
        "account_status": account.status,
        "time_barred": policy.time_barred(account, session.clock.today()),
        "medical_debt": session.creditor.kind == "medical",
        "you": {
            "agent_name": AGENT_NAME,
            "business_name": agency.name.removesuffix(", LLC"),
            "callback_number": agency.callback_number,
            "callback_contact": agency.callback_contact,
        },
    }
    return _with_assessment(session, result)


@REGISTRY.tool(NoArgs, requires=Gate.NONE)
async def get_dialed_phone(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Connect-time read 2 of 3. Reads the phone number the dialer called: its source, its artificial-voice
    consent and whether it is marked wrong number or do-not-call."""
    phone = await session.db.phone(session.context.phone_id)
    session.phone = phone
    result = {
        "phone_id": phone.phone_id,
        "line_type": phone.line_type,
        "source": phone.source,
        "artificial_voice_consent": phone.artificial_voice_consent and phone.consent_revoked_at is None,
        "status": phone.status,
    }
    return _with_assessment(session, result)


@REGISTRY.tool(NoArgs, requires=Gate.NONE)
async def get_contact_history(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """Connect-time read 3 of 3. Reads the account's contact history: earlier calls, voicemails and letters,
    the attempts that count toward the 3-in-7-days cap, and whether the consumer has been reached before."""
    history = await session.db.contact_history(session.context.account_id)
    session.history = history
    now = session.clock.now()
    result = {
        "attempts": [
            {
                "at": a.at.isoformat(),
                "medium": a.medium,
                "direction": a.direction,
                "answered_by": a.answered_by,
                "consumer_responded": a.consumer_responded,
                "disposition": a.disposition,
                "summary": a.summary,
            }
            for a in history
        ],
        "counted_attempts_last_7_days": policy.counted_attempts(history, now),
        "consumer_responded_last_7_days": policy.responded_in_window(history, now),
        "consumer_reached_before": not policy.first_conversation(history),
    }
    return _with_assessment(session, result)


def _with_assessment(session: RemiCall, result: dict[str, Any]) -> dict[str, Any]:
    """Once all three reads are in, decide the call and attach the decision."""
    if session.missing_reads:
        result["next"] = f"Also call: {', '.join(session.missing_reads)}."
        return result
    if session.assessment is None:
        assert session.account and session.phone and session.consumer and session.history is not None
        today = session.clock.today()
        session.assessment = Assessment(
            should_not_call_reasons=policy.should_not_call_reasons(
                session.account, session.phone, session.history, session.clock.now()
            ),
            first_conversation=policy.first_conversation(session.history),
            time_barred=policy.time_barred(session.account, today),
        )
    a = session.assessment
    result["call_assessment"] = {
        "proceed": a.proceed,
        "should_not_call_reasons": list(a.should_not_call_reasons),
        "first_conversation": a.first_conversation,
        "time_barred": a.time_barred,
        "next": (
            "Wait for the person to speak, then greet and ask for the consumer by full name."
            if a.proceed
            else "This call should not have been placed. When the person speaks, give only your name, the "
            "business name and that you are a virtual assistant, then a courteous goodbye: no purpose, no "
            "debt, no verification questions. Then log_contact_attempt with answered_by 'none' and end_call."
        ),
    }
    return result
