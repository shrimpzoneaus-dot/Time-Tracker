"""The guard on the destructive reset script.

reset_target_db.py truncates every data table. The only thing standing between
a scratch wipe and deleting the payroll history is this predicate, so it gets
tested rather than trusted.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reset_target_db as reset  # noqa: E402


def test_an_empty_target_is_not_production():
    assert reset.is_production_looking(0, 0) is False


def test_a_handful_of_smoke_test_shifts_is_not_production():
    assert reset.is_production_looking(1, 0) is False
    assert reset.is_production_looking(5, 0) is False


def test_the_real_dataset_is_recognised_as_production():
    """102 timesheets and 6 advances - the live SQLite database."""
    assert reset.is_production_looking(102, 6) is True


def test_a_single_advance_is_enough_to_refuse():
    """Smoke testing never records an advance; an advance is money paid out."""
    assert reset.is_production_looking(0, 1) is True


def test_many_shifts_alone_is_enough_to_refuse():
    assert reset.is_production_looking(11, 0) is True


def test_threshold_boundary_is_exclusive():
    assert reset.is_production_looking(10, 0) is False
    assert reset.is_production_looking(11, 0) is True
