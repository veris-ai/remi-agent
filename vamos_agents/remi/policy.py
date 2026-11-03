"""The calendar and arithmetic Remi records with, as pure functions. Nothing here decides for the agent.

The bench measures the agent (World v2 WORLD.md, "Nothing refuses the agent"): no
rule here refuses, blocks or steers what the agent does, and no result carries a
verdict. What is here is how Remi writes down what the agent did: a confirmation
letter's due date in business days, a plan's payments by World v2's rounding, an
amount in dollars, whether a debt is past its time to sue (a fact read from a
date), and the one disposition a call's record carries.

Every function takes the World's date explicitly (from ``CallClock``); nothing
here reads a clock. This module must not import the benchmark's grader.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

from .world import Account

CONFIRMATION_LETTER_BUSINESS_DAYS = 5

# One call, one disposition: the first of these the call produced.
DISPOSITION_PRECEDENCE = (
    "bankruptcy",
    "attorney",
    "cease",
    "dispute",
    "settled",
    "paid",
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
        "not_verified",
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


# --- facts read from dates ---------------------------------------------------------------


def time_barred(account: Account, today: date) -> bool:
    """Past its time to sue: the account has one on file and it ended before today."""
    return account.sol_expires_on is not None and account.sol_expires_on < today


# --- business days -----------------------------------------------------------------------


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
    """23 NYCRR 1.5: an agreement's written confirmation goes out within 5 business days."""
    return add_business_days(created_on, CONFIRMATION_LETTER_BUSINESS_DAYS)


# --- money -----------------------------------------------------------------------------


def dollars(cents: int) -> str:
    """An amount as the model sees and says it: "$1,430.00". The World stores cents; tools never return them."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{part:02d}"


def cents(amount: Any) -> int:
    """A dollar string from the World's policy ("945.00") in cents."""
    return int((Decimal(str(amount)) * 100).to_integral_value())


def add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def monthly_due_dates(first_due_on: date, count: int) -> list[date]:
    """One payment in each calendar month from the first's, on its day (or the month's last day)."""
    return [add_months(first_due_on, i) for i in range(count)]


def plan_instalment_cents(total_cents: int, months: int) -> list[int]:
    """World v2's plan payments: equal, each rounded down to the cent, the last taking the remainder, so the
    plan totals exactly what it covers ($576.00 in 7: six of $82.28 and one of $82.32)."""
    each = total_cents // months
    return [each] * (months - 1) + [total_cents - each * (months - 1)]


def disposition(outcomes: set[str], *, transferred: bool) -> str:
    """The first disposition, in precedence, that the call produced."""
    present = outcomes | ({"transferred"} if transferred else set())
    return next((d for d in DISPOSITION_PRECEDENCE if d in present), "no_commitment")
