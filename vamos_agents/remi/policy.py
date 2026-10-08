"""RULES.md's numeric and calendar rules, as pure functions Remi's tools enforce.

The world enforces none of the collections policy (WORLD.md, "What the world
does not enforce"): Postgres accepts a settlement below the floor or a callback
at 11 p.m. Remi refuses those itself, here, so a model that misjudges a number
gets a tool error it can act on instead of writing a violation into the world.

Every function takes the World's date or time explicitly (from ``CallClock``);
nothing here reads a clock. This module is Remi's own reading of RULES.md and
must not import the benchmark's grader.
"""

from __future__ import annotations

import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache

from .context import EASTERN
from .world import CLOSED_STATUSES, Account, ContactAttempt, Phone

# --- house rules and the law, in the order RULES.md states them ---------------
SETTLEMENT_INSTALMENTS_MAX = 3
SETTLEMENT_INSTALMENTS_PREMIUM_PTS = 10
PLAN_MIN_MONTHS = 2
PLAN_FIRST_DUE_MAX_DAYS = 30
HARDSHIP_MAX_MONTHS = 24
HARDSHIP_MIN_INSTALMENT_CENTS = 1_000
PTP_MAX_DAYS = 14
CALL_WINDOW_START = time(8, 0)  # at or after
CALL_WINDOW_END = time(21, 0)  # strictly before
CAP_ATTEMPTS = 3
CAP_WINDOW = timedelta(days=7)
TIME_BARRED_NOTICE_MIN_DAYS = 14
TIME_BARRED_REQUEUE_DAYS = 30
CONFIRMATION_LETTER_BUSINESS_DAYS = 5

# One call, one disposition: the first of these the call produced (WORLD.md).
DISPOSITION_PRECEDENCE = (
    "bankruptcy",
    "attorney",
    "cease",
    "dispute",
    "settled",
    "paid",
    "settlement_plan",
    "hardship_plan",
    "plan",
    "ptp",
    "transferred",
    "original_creditor_request",
    "channel_optout",
    "hardship",
    "callback",
    "no_commitment",
)
DISPOSITIONS = frozenset(
    {
        "should_not_call",
        "failed_verification",
        "refused_verification",
        "unverified_stop",
        "wrong_number",
        "voicemail",
        "third_party",
        "deceased",
    }
    | set(DISPOSITION_PRECEDENCE)
)


# --- time of day and contact frequency ----------------------------------------------


def in_calling_window(at: datetime) -> bool:
    """NYC § 5-77(b)(1)(i): from 8:00 a.m. up to, not including, 9:00 p.m. Eastern."""
    local = at.astimezone(EASTERN).time()
    return CALL_WINDOW_START <= local < CALL_WINDOW_END


def counted_attempts(history: list[ContactAttempt], now: datetime) -> int:
    """Attempts in the trailing 7 days that count toward the NYC cap: outbound
    calls, voicemails, texts and emails that reached someone. Mail,
    consumer-initiated contact and calls that never connected do not count."""
    since = now - CAP_WINDOW
    return sum(
        1
        for a in history
        if since < a.at <= now
        and a.direction == "outbound"
        and a.medium != "letter"
        and a.answered_by != "no_answer"
    )


def responded_in_window(history: list[ContactAttempt], now: datetime) -> bool:
    since = now - CAP_WINDOW
    return any(since < a.at <= now and a.consumer_responded for a in history)


def first_conversation(history: list[ContactAttempt]) -> bool:
    """No earlier attempt reached the consumer: the alternative-format statement is due."""
    return not any(a.answered_by == "consumer" for a in history)


# --- time-barred debt ------------------------------------------------------------------


def time_barred(account: Account, today: date) -> bool:
    return account.sol_expires_on < today


def time_barred_notice_ok(account: Account, today: date) -> bool:
    """The time-barred notice was mailed at least 14 days before today."""
    mailed = account.time_barred_notice_mailed_on
    return mailed is not None and (today - mailed).days >= TIME_BARRED_NOTICE_MIN_DAYS


def time_barred_notice_requeue(account: Account, today: date) -> bool:
    """After a call on a time-barred account, queue the notice again unless one
    went out in the last 30 days (NYC § 5-77(i)(4))."""
    mailed = account.time_barred_notice_mailed_on
    return time_barred(account, today) and (
        mailed is None or (today - mailed).days > TIME_BARRED_REQUEUE_DAYS
    )


# --- at connect: should this call have been placed? ---------------------------------


def should_not_call_reasons(
    account: Account, phone: Phone, history: list[ContactAttempt], now: datetime
) -> tuple[str, ...]:
    """Every reason the dialer should not have placed this call (RULES.md, At connect, step 1)."""
    reasons: list[str] = []
    if account.status in CLOSED_STATUSES:
        reasons.append(f"account_status_{account.status}")
    if phone.consent_revoked_at is not None or not phone.artificial_voice_consent:
        reasons.append("consent_revoked")
    if phone.status != "valid":
        reasons.append(f"phone_{phone.status}")
    if phone.source == "employer":
        reasons.append("employer_phone")
    if counted_attempts(history, now) >= CAP_ATTEMPTS:
        reasons.append("cap_reached")
    if responded_in_window(history, now):
        reasons.append("responded_this_week")
    today = now.astimezone(EASTERN).date()
    if time_barred(account, today) and not time_barred_notice_ok(account, today):
        reasons.append("time_barred_notice_not_mailed_14_days")
    if not in_calling_window(now):
        reasons.append("outside_calling_window")
    return tuple(reasons)


# --- business days -------------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@cache
def federal_holidays(year: int) -> frozenset[date]:
    """US federal holidays as banks observe them (a Saturday holiday moves to Friday,
    a Sunday one to Monday). New Year's Day of the next year may fall in this one."""
    mon, thu = 0, 3
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    days = {_observed(d) for d in fixed} | {_observed(date(year + 1, 1, 1))}
    days |= {
        _nth_weekday(year, 1, mon, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, mon, 3),  # Presidents' Day
        _last_weekday(year, 5, mon),  # Memorial Day
        _nth_weekday(year, 9, mon, 1),  # Labor Day
        _nth_weekday(year, 10, mon, 2),  # Columbus Day
        _nth_weekday(year, 11, thu, 4),  # Thanksgiving
    }
    return frozenset(d for d in days if d.year == year)


def add_business_days(start: date, days: int) -> date:
    current, remaining = start, days
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in federal_holidays(current.year):
            remaining -= 1
    return current


def confirmation_letter_due_by(created_on: date) -> date:
    """23 NYCRR 1.5: an arrangement's confirmation goes out within 5 business days."""
    return add_business_days(created_on, CONFIRMATION_LETTER_BUSINESS_DAYS)


# --- money ---------------------------------------------------------------------------


def dollars(cents: int) -> str:
    """An amount as the model sees and says it: "$1,430.00". The World stores cents; tools never return them."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{part:02d}"


def to_cents(amount: float) -> int | None:
    """Dollars from a tool argument to whole cents; None when it has fractions of a cent."""
    cents = round(amount * 100)
    return cents if abs(amount * 100 - cents) < 1e-6 else None


def pct_of(balance_cents: int, pct: int) -> int:
    """A percentage of a balance in whole cents, rounded up: a floor is never undercut by rounding."""
    return math.ceil(balance_cents * pct / 100)


def settlement_floor_cents(account: Account, balance_cents: int, instalments: int) -> int:
    """The least a settlement may total: the floor for one payment, ten points more in instalments."""
    premium = 0 if instalments == 1 else SETTLEMENT_INSTALMENTS_PREMIUM_PTS
    return pct_of(balance_cents, account.settlement_floor_pct + premium)


def add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def instalment_cents(total_cents: int, count: int) -> list[int]:
    """Equal instalments the way the ledger spreads a balance (RULES.md): each is the total divided by the
    count, rounded half up to the cent, and the last absorbs the difference ($265.00 in 6: five of $44.17
    and one of $44.15)."""
    each = int((Decimal(total_cents) / count).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return [each] * (count - 1) + [total_cents - each * (count - 1)]


def monthly_instalments(total_cents: int, count: int, first_due_on: date) -> list[dict[str, object]]:
    """Equal monthly instalments on the same day each month, rounded as ``instalment_cents``."""
    return [
        {"due_on": add_months(first_due_on, i).isoformat(), "amount_cents": amount}
        for i, amount in enumerate(instalment_cents(total_cents, count))
    ]


# --- refusals: why an offer is outside the house rules, or None --------------------------


@dataclass(frozen=True)
class Refusal:
    """A rule the requested terms break, with what Remi should do instead."""

    code: str
    detail: str


def plan_refusal(
    account: Account, balance: int, months: int, first_due_on: date, today: date, *, hardship: bool
) -> Refusal | None:
    max_months = HARDSHIP_MAX_MONTHS if hardship else account.max_plan_months
    minimum = HARDSHIP_MIN_INSTALMENT_CENTS if hardship else account.min_instalment_cents
    if months < PLAN_MIN_MONTHS:
        return Refusal("term_below_minimum", f"a plan needs at least {PLAN_MIN_MONTHS} monthly payments")
    if months > max_months:
        return Refusal("term_above_maximum", f"this account allows at most {max_months} monthly payments")
    if balance // months < minimum:
        return Refusal("instalment_below_minimum", f"each payment must be at least {dollars(minimum)}")
    return first_due_refusal(first_due_on, today)


def first_due_refusal(first_due_on: date, today: date) -> Refusal | None:
    if not today < first_due_on <= today + timedelta(days=PLAN_FIRST_DUE_MAX_DAYS):
        return Refusal(
            "first_payment_outside_30_days",
            f"the first payment must fall after today and on or before {today + timedelta(days=PLAN_FIRST_DUE_MAX_DAYS)}",
        )
    return None


def settlement_refusal(account: Account, balance: int, total_cents: int, instalments: int) -> Refusal | None:
    if instalments > SETTLEMENT_INSTALMENTS_MAX:
        return Refusal(
            "too_many_settlement_instalments",
            f"a settlement is at most {SETTLEMENT_INSTALMENTS_MAX} payments",
        )
    floor = settlement_floor_cents(account, balance, instalments)
    if total_cents < floor:
        return Refusal(
            "below_floor", f"this settlement must total at least {dollars(floor)}; a person must handle less"
        )
    if total_cents >= balance:
        return Refusal("not_a_settlement", "that is the full balance: use pay in full or a payment plan")
    return None


def ptp_refusal(due_on: date, today: date) -> Refusal | None:
    """A promise is an exact amount on an exact date at most 14 days out."""
    if not today <= due_on <= today + timedelta(days=PTP_MAX_DAYS):
        return Refusal(
            "promise_beyond_14_days",
            f"a promise must fall between {today} and {today + timedelta(days=PTP_MAX_DAYS)}",
        )
    return None


def disposition(outcomes: set[str], *, transferred: bool) -> str:
    """The first disposition, in WORLD.md's precedence, that the call produced."""
    present = outcomes | ({"transferred"} if transferred else set())
    return next((d for d in DISPOSITION_PRECEDENCE if d in present), "no_commitment")
