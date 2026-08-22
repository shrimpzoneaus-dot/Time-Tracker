"""Pay arithmetic. The only place in the codebase that turns hours into money.

Two rules protect the existing figures:

1. Seconds are summed across the month and the money is rounded ONCE, which is
   what the legacy code did and therefore what every payslip already issued
   reflects. Rounding per shift would shift historical totals by a few cents.
2. When an employee has more than one rate in a month, shifts are grouped by
   the rate that applied and each group is rounded once. With a single rate -
   which is every employee today, since rate_history is seeded at 1970-01-01 -
   this is arithmetically identical to rule 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SECONDS_PER_HOUR = 3600


class MoneyError(ValueError):
    """A money string we refuse to guess at."""


@dataclass(frozen=True)
class RatePeriod:
    hourly_rate_cents: int
    effective_from: date


@dataclass
class EmployeeMonth:
    user_id: int
    full_name: str
    work_seconds: int = 0
    gross_cents: int = 0
    advance_cents: int = 0
    net_cents: int = 0
    shifts: int = 0
    incomplete_shifts: int = 0
    exceptions: list[str] = field(default_factory=list)


def parse_money_to_cents(raw_amount: str) -> int:
    """'$1,234.56' -> 123456.

    Decimal rather than float: float(2.675) * 100 rounds to 267, not 268.
    """
    cleaned = str(raw_amount).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned:
        raise MoneyError("Amount is empty.")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as error:
        raise MoneyError(f"'{raw_amount}' is not an amount.") from error
    if amount < 0:
        raise MoneyError("Amount cannot be negative.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(int(cents)) / 100:,.2f}"


def gross_cents(work_seconds: int, hourly_rate_cents: int) -> int:
    """Exact rational arithmetic, rounded half-up at the end."""
    exact = (Decimal(int(work_seconds)) * Decimal(int(hourly_rate_cents))) / Decimal(
        SECONDS_PER_HOUR
    )
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def resolve_rate(periods: list[RatePeriod], on_date: date) -> int:
    """The rate in force on a given day.

    Fixes the retroactive-raise defect: pay was previously hours times the
    employee's CURRENT rate, so a raise silently re-priced every past month and
    an old payslip could not be reproduced.
    """
    applicable = [p for p in periods if p.effective_from <= on_date]
    if not applicable:
        return 0
    return max(applicable, key=lambda p: p.effective_from).hourly_rate_cents


def gross_for_shifts(
    shift_seconds: list[tuple[date, int]],
    periods: list[RatePeriod],
) -> tuple[int, int]:
    """(total work seconds, gross cents) for one employee over any span."""
    by_rate: dict[int, int] = {}
    total_seconds = 0

    for work_date, seconds in shift_seconds:
        rate = resolve_rate(periods, work_date)
        by_rate[rate] = by_rate.get(rate, 0) + seconds
        total_seconds += seconds

    total_gross = sum(gross_cents(seconds, rate) for rate, seconds in by_rate.items())
    return total_seconds, total_gross


def net_cents(gross: int, advances: int) -> int:
    return gross - advances
