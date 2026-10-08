"""``RemiCall``: one call's state, and the opening-order rules its gates enforce.

RULES.md fixes the order of a call's opening: read the account, the dialed
phone and the contact history; decide whether the call should have been
placed; confirm the right party; give the recording disclosure and the
mini-Miranda (and, when due, the time-barred disclosure and the
alternative-format statement); only then the debt. Calls are in English only:
Remi has no language step and never writes a language preference. The
prompt asks the model to keep that order. ``RemiCall.gate`` computes how far
the call has got, and ``vamos_agents.remi.gates`` refuses any tool that arrives
early with an error that says what must happen first. Nothing reaches the
database out of order, however the model behaves.

Every tool handler receives this object. The framework's ``CallSession``
dispatches to them, redacts card fields in logs, and turns refusals and faults
into ``{"error": ...}`` (see ``vamos_voice.session``).
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
from .gates import Gate
from .ledger import Debt, Ledger, LedgerUnavailable
from .payments import SavedMethod
from .world import Account, Agency, Consumer, ContactAttempt, Creditor, Phone

logger = logging.getLogger(__name__)

AGENT_NAME = "Remi"
CONNECT_READS = ("get_account", "get_dialed_phone", "get_contact_history")
MAX_VERIFICATION_ATTEMPTS = 3
#: How the contact log names who answered (``answered_by`` as stored).
WHO_ANSWERED = {
    "consumer": "the verified consumer",
    "estate_rep": "a verified estate representative",
    "third_party": "someone other than the consumer (not verified)",
    "voicemail": "voicemail",
    "wrong_number": "a wrong number",
}
#: The summary's transfer line. The bench reads a transfer from a contact row whose disposition is
#: ``transferred`` or whose summary says "transferred to a person." (for a candidate without a trace),
#: so the line is there whenever the call was transferred, whatever the disposition's precedence.
TRANSFER_NOTE = "Transferred to a person."


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass
class Assessment:
    """The connect-time decision: may this call proceed, and what does its opening owe?"""

    should_not_call_reasons: tuple[str, ...]
    first_conversation: bool
    time_barred: bool

    @property
    def proceed(self) -> bool:
        return not self.should_not_call_reasons


@dataclass(frozen=True)
class ContactNote:
    """What log_contact_attempt recorded for the call's one contact row (the row is written at the end)."""

    answered_by: str | None  # as the model established it, already checked against the call
    notes: str
    number_is_wrong: bool


@dataclass
class RemiCall:
    """Everything one call knows and has done. One per voice connection."""

    context: CallContext
    clock: CallClock
    db: Database
    ledger: Ledger

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
    verification_failures: int = 0  # wrong answers (a wrong name or fact)
    name_confirmed: bool = False  # they said, or said yes to, the consumer's name
    facts_confirmed: set[str] = field(default_factory=set)  # date_of_birth / zip / ssn_last4 that matched
    verification_refused: bool = False  # the person declined to verify (not a failure)
    debt_unlocked: bool = False
    debt: Debt | None = None  # the account's money, as the ledger last answered (get_debt_details on)
    disclosures_attested: tuple[str, ...] = ()  # what get_debt_details was told had been said

    # What the call produced.
    collection_stopped_by: str | None = None
    outcomes: set[str] = field(default_factory=set)
    transferred: bool = False
    hardship_recorded: bool = False
    deceased_flag_id: str | None = None
    deceased_details: dict[str, Any] | None = None  # the death flag's details as last written
    deceased_on: date | None = None  # the date of death written on this call
    #: An unverified person asked the agency to stop calling this number, or said it is not the
    #: consumer's: "unverified" (a flag for review, disposition unverified_stop) or "wrong_number".
    number_stop: str | None = None
    phone_marked_wrong: bool = False  # the dialed phone's status is already wrong_number
    consent_revoked: set[str] = field(default_factory=set)  # phone ids whose consent this call revoked
    scra_recorded: bool = False
    letters_queued: set[str] = field(default_factory=set)
    payment_methods: dict[str, SavedMethod] = field(default_factory=dict)  # tokenized on this call
    declined_methods: set[str] = field(default_factory=set)
    #: log_contact_attempt's note; the row itself is written once, when the call ends.
    contact_note: ContactNote | None = None
    #: The written row's disposition, once it is written (end_call, or finalize).
    contact_disposition: str | None = None
    ended: bool = False
    _contact_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
        """Why a tool needing ``needed`` may not run yet: the rule, not words to say."""
        if self.assessment is None:
            return f"The connect-time reads come first: {', '.join(self.missing_reads)}."
        if not self.assessment.proceed:
            return (
                "This call should not have been placed ("
                + ", ".join(self.assessment.should_not_call_reasons)
                + "): nothing about why you called may be said, and the only tools left are "
                "log_contact_attempt (answered_by 'none') and end_call."
            )
        if self.verification_failures >= MAX_VERIFICATION_ATTEMPTS:
            return "Verification failed three times: nothing may be disclosed, and the call ends."
        if needed >= Gate.VERIFIED and self.verified_as is None:
            return (
                "Not verified yet: the debt, and anything that depends on it, waits until verify_right_party "
                "confirms this is the consumer."
            )
        return (
            "The debt comes only after the required disclosures and get_debt_details: the recording "
            "disclosure, the mini-Miranda, and when due the time-barred disclosure and the alternative-format "
            "statement."
        )

    def require_consumer(self) -> None:
        if self.verified_as != "consumer":
            raise ToolError("Only the verified consumer can do this; it is not available to anyone else.")

    async def read_debt(self) -> Debt:
        """The account's money from the ledger, now; kept on the call. An unreachable ledger is a refusal
        the model can act on (the balance cannot be stated), and is logged."""
        try:
            self.debt = await self.ledger.debt(self.context.account_id)
        except LedgerUnavailable as err:
            logger.error("ledger unavailable reading %s: %s", self.context.account_id, err)
            raise ToolError(
                "The account's ledger cannot be reached right now, so the balance cannot be read: offer a "
                "callback or a person (transfer_to_human)."
            ) from err
        return self.debt

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
        """Revoke each phone's artificial-voice consent once: not if the World already had it revoked,
        and not twice on one call (a wrong number that then says stop, an opt-out then a cease)."""
        for phone in phones:
            if phone.consent_revoked_at is None and phone.phone_id not in self.consent_revoked:
                await tx.update("phones", phone.phone_id, {"consent_revoked_at": self.clock.now()})
                self.consent_revoked.add(phone.phone_id)

    def disposition_for(self, answered_by: str | None) -> str:
        """WORLD.md's disposition: who answered first, then the first outcome in precedence."""
        if self.assessment is not None and not self.assessment.proceed:
            return "should_not_call"
        if self.deceased_flag_id is not None:
            return "deceased"
        if answered_by in ("voicemail", "wrong_number", "third_party"):
            return answered_by
        if answered_by is None:
            if self.number_stop == "unverified":
                return "unverified_stop"
            if self.verification_refused and not self.verification_failures:
                return "refused_verification"
            return "failed_verification"
        return policy.disposition(self.outcomes, transferred=self.transferred)

    def recorded_answerer(self, claimed: str | None) -> str | None:
        """``answered_by`` as WORLD.md defines it: what the call established, whatever the model claims.
        Null when nobody was confirmed: a refusal to verify, or a stop request from someone unverified.
        Someone unverified who reported the consumer's death is a third party."""
        if self.assessment is not None and not self.assessment.proceed:
            return None
        if self.verified_as is not None:
            return self.verified_as
        if self.number_stop == "wrong_number":
            return "wrong_number"
        if self.number_stop == "unverified" or self.verification_refused:
            return None
        if self.deceased_flag_id is not None:
            return "third_party"
        return claimed

    def recorded_facts(self, answered_by: str | None, disposition: str) -> str:
        """The factual part of the call's summary, built from what the call recorded rather than
        from the model's account of it (which can describe what it meant to say, not what it said)."""
        facts = [f"Disposition: {disposition}."]
        a = self.assessment
        if a is not None and not a.proceed:
            facts.append(
                f"The call should not have been placed ({', '.join(a.should_not_call_reasons)}): "
                "no one was verified and the debt was not read."
            )
            return " ".join(facts)
        facts.append(f"Answered by: {WHO_ANSWERED.get(answered_by or '', 'no one verified')}.")
        if self.debt_unlocked:
            facts.append(f"Debt read after attesting: {', '.join(self.disclosures_attested)}.")
        else:
            facts.append("The debt was not read.")
        if self.outcomes:
            facts.append(f"Recorded outcomes: {', '.join(sorted(self.outcomes))}.")
        if self.transferred:
            facts.append(TRANSFER_NOTE)
        return " ".join(facts)

    @property
    def contact_logged(self) -> bool:
        """log_contact_attempt has run (the row is written when the call ends)."""
        return self.contact_note is not None

    async def write_contact_record(self) -> str:
        """The call's one contact_attempts row (NYC § 2-193), plus its end-of-call effects; once.

        Written when the call ends (end_call, or ``finalize`` when the other side hangs up first), from
        everything the call recorded, so an outcome after log_contact_attempt still counts. Its summary
        is the recorded facts, then the model's notes (none: it never logged the call)."""
        async with self._contact_lock:
            if self.contact_disposition is not None:
                return self.contact_disposition
            if self.account is None or self.phone is None:
                raise ToolError("The connect-time reads come first.")
            note = self.contact_note
            answered_by = self.recorded_answerer(note.answered_by if note else self.verified_as)
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
                if mark_wrong:
                    await tx.update("phones", self.context.phone_id, {"status": "wrong_number"})
                if answered_by == "consumer" and policy.time_barred_notice_requeue(
                    self.account, self.clock.today()
                ):
                    await self.queue_letters(tx, "time_barred")
            self.phone_marked_wrong = self.phone_marked_wrong or mark_wrong
            self.contact_disposition = disposition
            return disposition

    async def finalize(self) -> None:
        """The voice connection closed (or the caller hung up). Every call leaves exactly one
        contact-attempt row (NYC § 2-193): write it now if end_call has not."""
        if self.contact_disposition is not None or self.assessment is None:
            return
        disposition = await self.write_contact_record()
        if self.contact_note is None:
            logger.warning("call closed without log_contact_attempt; logged it as %s", disposition)
