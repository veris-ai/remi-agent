"""``RemiCall``: one call's state, and the opening-order rules its gates enforce.

RULES.md fixes the order of a call's opening: read the account, the dialed
phone and the contact history; decide whether the call should have been
placed; confirm the right party; give the recording disclosure and the
mini-Miranda (and, when due, the language question, the time-barred
disclosure and the alternative-format statement); only then the debt. The
prompt asks the model to keep that order. ``RemiCall.gate`` computes how far
the call has got, and ``vamos_agents.remi.gates`` refuses any tool that arrives
early with an error that says what must happen first. Nothing reaches the
database out of order, however the model behaves.

Every tool handler receives this object. The framework's ``CallSession``
dispatches to them, redacts card fields in logs, and turns refusals and faults
into ``{"error": ...}`` (see ``vamos_voice.session``).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from vamos_voice.context import CallClock
from vamos_voice.tools import ToolError

from . import policy
from .context import CallContext
from .db import Database, Transaction
from .gates import Gate
from .payments import SavedMethod
from .world import Account, Agency, Consumer, ContactAttempt, Creditor, Phone

logger = logging.getLogger(__name__)

AGENT_NAME = "Remi"
CONNECT_READS = ("get_account", "get_dialed_phone", "get_contact_history")
MAX_VERIFICATION_ATTEMPTS = 3


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass
class Assessment:
    """The connect-time decision: may this call proceed, and what does its opening owe?"""

    should_not_call_reasons: tuple[str, ...]
    first_conversation: bool
    time_barred: bool
    language_needed: bool

    @property
    def proceed(self) -> bool:
        return not self.should_not_call_reasons


@dataclass
class RemiCall:
    """Everything one call knows and has done. One per voice connection."""

    context: CallContext
    clock: CallClock
    db: Database

    # Connect-time reads.
    agency: Agency | None = None
    account: Account | None = None
    consumer: Consumer | None = None
    creditor: Creditor | None = None
    phone: Phone | None = None
    history: list[ContactAttempt] | None = None
    assessment: Assessment | None = None

    # Opening progress.
    verified_as: str | None = None  # "consumer" | "estate_rep"
    verification_failures: int = 0
    language_recorded: bool = False
    debt_unlocked: bool = False

    # What the call produced.
    collection_stopped_by: str | None = None
    outcomes: set[str] = field(default_factory=set)
    transferred: bool = False
    hardship_recorded: bool = False
    deceased_flag_id: str | None = None
    letters_queued: set[str] = field(default_factory=set)
    payment_methods: dict[str, SavedMethod] = field(default_factory=dict)  # tokenized on this call
    declined_methods: set[str] = field(default_factory=set)
    contact_logged: bool = False
    ended: bool = False

    # --- the gate ------------------------------------------------------------------------

    @property
    def reads_done(self) -> set[str]:
        done = set()
        if self.account is not None:
            done.add("get_account")
        if self.phone is not None:
            done.add("get_dialed_phone")
        if self.history is not None:
            done.add("get_contact_history")
        return done

    @property
    def missing_reads(self) -> list[str]:
        return [r for r in CONNECT_READS if r not in self.reads_done]

    @property
    def gate(self) -> Gate:
        if self.assessment is None:
            return Gate.NONE
        if not self.assessment.proceed:
            return Gate.ASSESSED
        if self.verified_as is None:
            return Gate.PROCEEDING
        if not self.debt_unlocked:
            return Gate.VERIFIED
        return Gate.DEBT_UNLOCKED

    def refusal_for(self, needed: Gate) -> str:
        """What must happen before a tool needing ``needed`` may run."""
        if self.assessment is None:
            return f"Run the connect-time reads first: {', '.join(self.missing_reads)}."
        if not self.assessment.proceed:
            return (
                "This call should not have been placed ("
                + ", ".join(self.assessment.should_not_call_reasons)
                + "). Say nothing about why you called. Close the call courteously, then call "
                "log_contact_attempt with answered_by 'none', then end_call."
            )
        if self.verification_failures >= MAX_VERIFICATION_ATTEMPTS:
            return "Verification failed three times. Disclose nothing; end the call politely, log it, and end_call."
        if needed >= Gate.VERIFIED and self.verified_as is None:
            return "Confirm the right party first with verify_right_party. Say nothing about the debt until then."
        return (
            "Give the required disclosures first, then call get_debt_details: the recording disclosure, the "
            "mini-Miranda, and when due the language question (set_language), the time-barred disclosure and "
            "the alternative-format statement."
        )

    def require_consumer(self) -> None:
        if self.verified_as != "consumer":
            raise ToolError("Only the verified consumer can do this.")

    def stop_collection(self, reason: str) -> None:
        self.collection_stopped_by = self.collection_stopped_by or reason

    # --- shared writes ---------------------------------------------------------------------

    async def queue_letters(self, tx: Transaction, *kinds: str) -> list[str]:
        """Queue each letter once per call."""
        queued = []
        for kind in kinds:
            if kind in self.letters_queued:
                continue
            await tx.insert(
                "letters",
                {
                    "letter_id": new_id("let"),
                    "account_id": self.context.account_id,
                    "kind": kind,
                    "queued_at": self.clock.now(),
                },
            )
            self.letters_queued.add(kind)
            queued.append(kind)
        return queued

    async def insert_flag(self, tx: Transaction, kind: str, details: dict[str, Any]) -> str:
        flag_id = new_id("flag")
        await tx.insert(
            "account_flags",
            {
                "flag_id": flag_id,
                "account_id": self.context.account_id,
                "kind": kind,
                "details": details,
                "created_at": self.clock.now(),
            },
        )
        return flag_id

    async def revoke_consent(self, tx: Transaction, phones: list[Phone]) -> None:
        for phone in phones:
            if phone.consent_revoked_at is None:
                await tx.update("phones", phone.phone_id, {"consent_revoked_at": self.clock.now()})

    def disposition_for(self, answered_by: str | None) -> str:
        """WORLD.md's disposition: who answered first, then the first outcome in precedence."""
        if self.assessment is not None and not self.assessment.proceed:
            return "should_not_call"
        if self.deceased_flag_id is not None:
            return "deceased"
        if answered_by in ("voicemail", "wrong_number", "third_party"):
            return answered_by
        if answered_by is None:
            return "failed_verification"
        return policy.disposition(self.outcomes, transferred=self.transferred)

    async def write_contact_attempt(
        self, *, answered_by: str | None, summary: str, number_is_wrong: bool = False
    ) -> str:
        """The call's one contact_attempts row (NYC § 2-193), plus its end-of-call effects."""
        if self.account is None or self.phone is None:
            raise ToolError("Run the connect-time reads first.")
        disposition = self.disposition_for(answered_by)
        async with self.db.transaction() as tx:
            await tx.insert(
                "contact_attempts",
                {
                    "attempt_id": new_id("att"),
                    "account_id": self.context.account_id,
                    "phone_id": self.context.phone_id,
                    "at": self.context.call_time,
                    "medium": "voicemail" if answered_by == "voicemail" else "call",
                    "direction": "outbound",
                    "answered_by": answered_by,
                    "consumer_responded": answered_by == "consumer",
                    "duration_s": max(1, round(self.clock.elapsed.total_seconds())),
                    "disposition": disposition,
                    "summary": summary,
                    "agent_name": AGENT_NAME,
                },
            )
            if number_is_wrong:
                await tx.update("phones", self.context.phone_id, {"status": "wrong_number"})
            if answered_by == "consumer" and policy.time_barred_notice_requeue(
                self.account, self.clock.today()
            ):
                await self.queue_letters(tx, "time_barred")
        self.contact_logged = True
        return disposition

    async def finalize(self) -> None:
        """The voice connection closed. If the model never logged the call, log it now:
        every call must leave exactly one contact-attempt row (NYC § 2-193)."""
        if self.contact_logged or self.assessment is None:
            return
        answered_by = self.verified_as if self.assessment.proceed else None
        summary = "The call ended before Remi finished its notes. " + (
            f"Recorded outcomes: {', '.join(sorted(self.outcomes))}."
            if self.outcomes
            else "No commitment was made."
        )
        disposition = await self.write_contact_attempt(answered_by=answered_by, summary=summary)
        logger.warning("call closed without log_contact_attempt; logged it as %s", disposition)
