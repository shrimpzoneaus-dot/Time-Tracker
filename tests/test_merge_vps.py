"""Merging the VPS snapshot back into the local legacy database.

The bot moved to a Vultr VPS on 2026-06-11 and kept recording there. On
2026-08-20 its database was copied to this machine, and edits were then made
HERE that the VPS never saw: three shifts were corrected by hand and shift 19
was deleted on owner instruction. Meanwhile the VPS carried on recording.

So neither file is a superset. The owner's ruling (2026-08-22) is that the
local edits win and the deletion stands, while the VPS supplies the shifts
recorded after the copy. That makes the local file the base and the snapshot a
source of *new rows only* -- which is exactly what these tests pin.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_vps_snapshot as merge  # noqa: E402

SCHEMA = """
CREATE TABLE users (user_id INTEGER PRIMARY KEY, full_name TEXT, role TEXT,
                    hourly_rate_cents INTEGER);
CREATE TABLE timesheets (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT,
                         in_time TEXT, out_time TEXT, break_start TEXT,
                         total_break_seconds INTEGER, status TEXT);
CREATE TABLE advances (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT,
                       amount_cents INTEGER, note TEXT, created_at TEXT);
"""


def build(path: Path, shifts, users=((1, "V Kai", "ADMIN", 1800),), advances=()):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO users VALUES (?,?,?,?)", users)
    conn.executemany("INSERT INTO timesheets VALUES (?,?,?,?,?,?,?,?)", shifts)
    conn.executemany("INSERT INTO advances VALUES (?,?,?,?,?,?)", advances)
    conn.commit()
    conn.close()
    return path


def shift(i, day="2026-08-19", start="09:00:00", end="17:00:00", brk=0, status="FINISHED"):
    return (i, 1, day, f"{day} {start}", f"{day} {end}" if end else None, None, brk, status)


def shifts_in(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    rows = {r[0]: r for r in conn.execute("SELECT * FROM timesheets")}
    conn.close()
    return rows


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "local.db", tmp_path / "snapshot.db", tmp_path / "merged.db"


def test_shifts_recorded_after_the_copy_are_brought_in(paths):
    local, snap, out = paths
    build(local, [shift(1), shift(2)])
    build(snap, [shift(1), shift(2), shift(3, day="2026-08-21"), shift(4, day="2026-08-22")])

    summary = merge.merge(local, snap, out)

    assert set(shifts_in(out)) == {1, 2, 3, 4}
    assert summary["added"] == 2


def test_a_deleted_shift_is_not_resurrected(paths):
    """Shift 19 was deleted here on owner instruction; the VPS still has it.
    Copying the snapshot naively would silently undo that decision."""
    local, snap, out = paths
    build(local, [shift(1)])
    build(snap, [shift(1), shift(19, day="2026-06-09")])

    summary = merge.merge(local, snap, out, skip_ids={19})

    assert 19 not in shifts_in(out)
    assert summary["skipped"] == [19]


def test_hand_corrected_times_are_not_overwritten_by_the_raw_ones(paths):
    """The whole point of the owner's ruling: a shift that exists in both keeps
    the local version, because that is the corrected one."""
    local, snap, out = paths
    build(local, [shift(103, end="18:40:00")])
    build(snap, [shift(103, end="20:07:25")])

    summary = merge.merge(local, snap, out)

    assert shifts_in(out)[103][4].endswith("18:40:00")
    assert summary["kept_local_edits"] == 1


def test_a_new_employee_on_the_vps_comes_across(paths):
    local, snap, out = paths
    build(local, [shift(1)])
    build(snap, [shift(1)], users=((1, "V Kai", "ADMIN", 1800), (2, "New Hire", "EMPLOYEE", 1600)))

    merge.merge(local, snap, out)

    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_a_changed_pay_rate_is_refused_rather_than_guessed(paths):
    """Rates decide money. If the two copies disagree on one, a merge cannot
    pick a winner on its own."""
    local, snap, out = paths
    build(local, [shift(1)], users=((1, "V Kai", "ADMIN", 1800),))
    build(snap, [shift(1)], users=((1, "V Kai", "ADMIN", 2000),))

    with pytest.raises(SystemExit, match="users"):
        merge.merge(local, snap, out)


def test_neither_source_file_is_modified(paths):
    local, snap, out = paths
    build(local, [shift(1)])
    build(snap, [shift(1), shift(2)])
    before = local.read_bytes(), snap.read_bytes()

    merge.merge(local, snap, out)

    assert (local.read_bytes(), snap.read_bytes()) == before


def test_the_merge_refuses_to_clobber_an_existing_output(paths):
    local, snap, out = paths
    build(local, [shift(1)])
    build(snap, [shift(1)])
    out.write_bytes(b"something already here")

    with pytest.raises(SystemExit, match="exists"):
        merge.merge(local, snap, out)
