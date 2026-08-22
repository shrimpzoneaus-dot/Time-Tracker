"""Money arithmetic, including the two defects it fixes."""

from __future__ import annotations

from datetime import date

import pytest

from app.domain import payroll


def test_parse_money_handles_symbols_and_separators():
    assert payroll.parse_money_to_cents("$1,234.56") == 123456
    assert payroll.parse_money_to_cents(" 100 ") == 10000
    assert payroll.parse_money_to_cents("0.05") == 5


def test_parse_money_is_exact_where_float_is_not():
    """float(2.675) * 100 == 267.49999... and rounds to 267."""
    assert payroll.parse_money_to_cents("2.675") == 268
    assert payroll.parse_money_to_cents("8.615") == 862


def test_parse_money_refuses_rubbish_rather_than_guessing():
    with pytest.raises(payroll.MoneyError):
        payroll.parse_money_to_cents("")
    with pytest.raises(payroll.MoneyError):
        payroll.parse_money_to_cents("twenty dollars")
    with pytest.raises(payroll.MoneyError):
        payroll.parse_money_to_cents("-50")


def test_format_money_round_trips():
    assert payroll.format_money(123456) == "$1,234.56"
    assert payroll.format_money(-500) == "-$5.00"


def test_gross_rounds_half_up_not_to_even():
    """The live divergence: 311097s at $18/h is exactly 155548.5c.

    round() is banker's rounding and gave 155548. Half-up gives the half cent
    to the employee.
    """
    assert payroll.gross_cents(311097, 1800) == 155549
    assert round(311097 * 1800 / 3600) == 155548


def test_gross_is_exact_for_whole_hours():
    assert payroll.gross_cents(3600, 1800) == 1800
    assert payroll.gross_cents(8 * 3600, 1600) == 12800


# --- rate history ----------------------------------------------------------

JAN = payroll.RatePeriod(hourly_rate_cents=1600, effective_from=date(2026, 1, 1))
JULY = payroll.RatePeriod(hourly_rate_cents=1800, effective_from=date(2026, 7, 1))


def test_resolve_rate_picks_the_period_in_force():
    assert payroll.resolve_rate([JAN, JULY], date(2026, 6, 30)) == 1600
    assert payroll.resolve_rate([JAN, JULY], date(2026, 7, 1)) == 1800
    assert payroll.resolve_rate([JAN, JULY], date(2026, 8, 20)) == 1800


def test_rate_before_any_period_is_zero_not_an_error():
    assert payroll.resolve_rate([JULY], date(2026, 1, 1)) == 0


def test_a_raise_does_not_reprice_past_shifts():
    """The retroactive-rate defect.

    June work must stay priced at June's rate after a July raise.
    """
    june = [(date(2026, 6, 10), 8 * 3600)]
    seconds, gross = payroll.gross_for_shifts(june, [JAN, JULY])
    assert seconds == 8 * 3600
    assert gross == 8 * 1600  # not 8 * 1800


def test_single_seeded_period_reproduces_month_level_rounding():
    """How every existing employee is migrated: one period from 1970.

    Summing seconds then rounding once must equal the legacy behaviour.
    """
    seeded = [payroll.RatePeriod(hourly_rate_cents=1800, effective_from=date(1970, 1, 1))]
    month = [(date(2026, 7, day), 11111) for day in range(1, 6)]

    seconds, gross = payroll.gross_for_shifts(month, seeded)
    assert seconds == 55555
    assert gross == payroll.gross_cents(55555, 1800)


def test_mixed_rates_round_once_per_rate_group():
    month = [(date(2026, 6, 30), 3600), (date(2026, 7, 1), 3600)]
    seconds, gross = payroll.gross_for_shifts(month, [JAN, JULY])
    assert seconds == 7200
    assert gross == 1600 + 1800


def test_net_subtracts_advances():
    assert payroll.net_cents(155549, 10000) == 145549
    assert payroll.net_cents(5000, 8000) == -3000  # advances may exceed pay
