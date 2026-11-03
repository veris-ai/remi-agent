"""``RemiCall``: one call's state, and the call's one contact record.

The bench measures the agent (World v2 WORLD.md, "Nothing refuses the agent"):
nothing here keeps the agent to an order or decides whether it may act. The
state holds what the call read (the account, the dialed phone, the contact
history, the debt), what verification established, and what the agent
recorded, and writes the call's one ``contact_attempts`` row when the call ends,
from what the agent logged.

Every tool handler receives this object. The framework's ``CallSession``
dispatches to them, redacts card fields in logs, and turns errors into
``{"error": ...}`` (see ``vamos_voice.session``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from vamos_voice.context import CallClock
from vamos_voice.tools import ToolError

from . import policy
from .context import CallContext
from .db import Database, Transaction
from .ledger import Debt, Ledger, LedgerUnavailable
from .payments import PayNearMe, SavedMethod
from .world import Account, Agency, Consumer, ContactAttempt, Creditor, Phone

logger = logging.getLogger(__name__)

AGENT_NAME = "Remi"
#: The reads the screen pop makes when the call connects (and the model may make again).
CONNECT_READS = ("get_account", "get_debt_details", "get_dialed_phone", "get_contact_history")
#: How the contact record's summary names who answered (``answered_by`` as the agent logged it).
WHO_ANSWERED = {
    "consumer": "the consumer",
    "estate_rep": "an estate representative",
    "third_party": "someone other than the consumer",
    "voicemail": "voicemail",
    "wrong_number": "a wrong number",
    "no_answer": "no answer",
}
#: The summary's transfer line. The bench reads a transfer from a contact row whose disposition is
#: ``transferred`` or whose summary says "Transferred to a person." (for a candidate without a trace),
#: so the line is there whenever the call was transferred, whatever the disposition's precedence.
TRANSFER_NOTE = "Transferred to a person."


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class ContactNote:
    """What log_contact_attempt recorded for the call's one contact row (the row is written at the end)."""

    answered_by: str | None  # as the agent logged it
    notes: str
    number_is_wrong: bool


@dataclass
class RemiCall:
    """Everything one call knows and has recorded. One per voice connection."""

    context: CallContext
    clock: CallClock
    db: Database
    ledger: Ledger
    payments: PayNearMe

    # The reads.
    agency: Agency | None = None
    account: Account | None = None
    consumer: Consumer | None = None
    creditor: Creditor | None = None
    phone: Phone | None = None
    history: list[ContactAttempt] | None = None
    debt: Debt | None = None  # the account's money, as the ledger last answered

    # What verification established (facts, never a gate).
    verified_as: str | None = None  # "consumer" | "estate_rep"
    name_confirmed: bool = False  # they said, or said yes to, the consumer's name
    facts_confirmed: set[str] = field(default_factory=set)  # date_of_birth / zip / ssn_last4 that matched
    wrong_answers: int = 0
    verification_refused: bool = False  # the agent recorded that the person declined to verify

    # What the agent recorded.
    outcomes: set[str] = field(default_factory=set)
    transferred: bool = False
    deceased_flag_id: str | None = None
    deceased_details: dict[str, Any] | None = None
    deceased_on: date | None = None
    number_stop: str | None = None  # "unverified": an unverified stop request was recorded
    phone_marked_wrong: bool = False  # the dialed phone's status is already wrong_number
    consent_revoked: set[str] = field(default_factory=set)  # phone ids whose consent this call revoked
    letters_queued: set[str] = field(default_factory=set)
    payment_methods: dict[str, SavedMethod] = field(default_factory=dict)  # tokenized on this call
    #: Charges run one at a time (two at once would race on PayNearMe's order); none is ever refused.
    charge_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    #: log_contact_attempt's note; the row itself is written once, when the call ends.
    contact_note: ContactNote | None = None
    #: The written row's disposition, once it is written (end_call, or finalize).
    contact_disposition: str | None = None
    ended: bool = False
    _contact_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    # --- reads -----------------------------------------------------------------------------

    @property
    def reads_done(self) -> set[str]:
        done = {
            name
            for name, value in (
                ("get_account", self.account),
                ("get_debt_details", self.debt),
                ("get_dialed_phone", self.phone),
                ("get_contact_history", self.history),
            )
            if value is not None
        }
        return done

    @property
    def missing_reads(self) -> list[str]:
        return [r for r in CONNECT_READS if r not in self.reads_done]

    async def load_account(self) -> Account:
        """The agency, the account, its consumer and creditor, read now; kept on the call."""
        self.agency = await self.db.agency()
        account = await self.db.account(self.context.account_id)
        self.consumer = await self.db.consumer(account.consumer_id)
        self.creditor = await self.db.creditor(account.creditor_id)
        self.account = account
        return account

    async def ensure_account(self) -> Account:
        return self.account if self.account is not None else await self.load_account()

    async def load_phone(self) -> Phone:
        self.phone = await self.db.phone(self.context.phone_id)
        return self.phone

    async def ensure_phone(self) -> Phone:
        return self.phone if self.phone is not None else await self.load_phone()

    async def read_debt(self) -> Debt:
        """The account's money from the ledger, now; kept on the call. An unreachable ledger is a fact the
        model hears, and is logged."""
        try:
            self.debt = await self.ledger.debt(self.context.account_id)
        except LedgerUnavailable as err:
            logger.error("ledger unavailable reading %s: %s", self.context.account_id, err)
            raise ToolError("The ledger could not be reached: the debt cannot be read now.") from err
        return self.debt

    async def read_policy(self) -> dict[str, Any]:
        """The account's creditor policy, as the World prices it for the call's date (``creditor_policy``)."""
        return await self.db.policy_function(
            "creditor_policy", {"account_id": self.context.account_id, "call_date": self.clock.today()}
        )

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

    async def revoke_consent(self, tx: Transaction, phone: Phone) -> None:
        """Revoke the phone's artificial-voice consent once: not if the World already had it revoked, and not
        twice on one call."""
        if phone.consent_revoked_at is None and phone.phone_id not in self.consent_revoked:
            await tx.update("phones", phone.phone_id, {"consent_revoked_at": self.clock.now()})
            self.consent_revoked.add(phone.phone_id)

    # --- the call's one contact record ------------------------------------------------------

    def disposition_for(self, answered_by: str | None) -> str:
        """The record's disposition, from who the agent logged as answering and what the call recorded."""
        if self.deceased_flag_id is not None:
            return "deceased"
        if answered_by in ("voicemail", "wrong_number", "third_party"):
            return answered_by
        if answered_by is None or answered_by == "no_answer":
            if self.number_stop == "unverified":
                return "unverified_stop"
            if self.verification_refused and not self.wrong_answers:
                return "refused_verification"
            if self.wrong_answers:
                return "failed_verification"
            return "not_verified"
        return policy.disposition(self.outcomes, transferred=self.transferred)

    def recorded_facts(self, answered_by: str | None, disposition: str) -> str:
        """The factual head of the call's summary: what the call recorded, then who the agent said answered."""
        facts = [
            f"Disposition: {disposition}.",
            f"Answered by: {WHO_ANSWERED.get(answered_by or '', 'not logged')}.",
        ]
        if self.outcomes:
            facts.append(f"Recorded: {', '.join(sorted(self.outcomes))}.")
        if self.transferred:
            facts.append(TRANSFER_NOTE)
        return " ".join(facts)

    @property
    def contact_logged(self) -> bool:
        """log_contact_attempt has run (the row is written when the call ends)."""
        return self.contact_note is not None

    async def write_contact_record(self) -> str:
        """The call's one contact_attempts row (NYC § 2-193); once.

        Written when the call ends (end_call, or ``finalize`` when the other side hangs up first), from what
        the agent logged and what the call recorded, so an outcome after log_contact_attempt still counts."""
        async with self._contact_lock:
            if self.contact_disposition is not None:
                return self.contact_disposition
            phone = await self.ensure_phone()
            note = self.contact_note
            answered_by = note.answered_by if note else None
            mark_wrong = not self.phone_marked_wrong and (
                answered_by == "wrong_number" or (note is not None and note.number_is_wrong)
            )
            disposition = self.disposition_for(answered_by)
            facts = self.recorded_facts(answered_by, disposition)
            summary = (
                f"{facts} Remi's notes: {note.notes}"
                if note
                else f"{facts} The call ended before Remi logged it."
            )
            async with self.db.transaction() as tx:
                await tx.insert(
                    "contact_attempts",
                    {
                        "attempt_id": new_id("att"),
                        "account_id": self.context.account_id,
                        "phone_id": phone.phone_id,
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
                if mark_wrong:
                    await tx.update("phones", phone.phone_id, {"status": "wrong_number"})
            self.phone_marked_wrong = self.phone_marked_wrong or mark_wrong
            self.contact_disposition = disposition
            return disposition

    async def finalize(self) -> None:
        """The voice connection closed (or the caller hung up). Every call leaves exactly one
        contact-attempt row (NYC § 2-193): write it now if end_call has not."""
        if self.contact_disposition is not None:
            return
        disposition = await self.write_contact_record()
        if self.contact_note is None:
            logger.warning("call closed without log_contact_attempt; logged it as %s", disposition)
