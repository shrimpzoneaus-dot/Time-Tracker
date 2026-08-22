"""Merge a VPS snapshot into the local legacy database.

Background: the bot moved to a Vultr VPS (67.219.100.235, hostname `telegram`,
systemd unit `time-tracker-bot.service`) on 2026-06-11 and kept recording
there. On 2026-08-20 its database was copied to this machine, and edits were
then made HERE that the VPS never saw -- three shifts corrected by hand, and
shift 19 deleted on owner instruction. The VPS meanwhile carried on recording.

Neither file is a superset of the other, so this is a merge, not a copy.

Owner ruling 2026-08-22:
  * the local hand-corrections WIN over the raw bot times;
  * the deletion of shift 19 STANDS;
  * the VPS supplies the shifts recorded after the copy was taken.

That makes the local file the base and the snapshot a source of NEW ROWS ONLY.

    # 1. take a consistent snapshot on the VPS and fetch it
    ssh root@67.219.100.235 '/root/time_tracker/.venv/bin/python -c "
    import sqlite3
    s=sqlite3.connect(\"file:/root/time_tracker/time_tracker.db?mode=ro\",uri=True)
    d=sqlite3.connect(\"/tmp/tt_snapshot.db\"); s.backup(d); d.close(); s.close()"'
    scp root@67.219.100.235:/tmp/tt_snapshot.db ./time_tracker_REMOTE.db

    # 2. merge
    .\\.venv\\Scripts\\python.exe scripts\\merge_vps_snapshot.py

Both inputs are opened READ-ONLY and are never modified. The output is a new
file; the script refuses to overwrite one that already exists.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

# Deleted on owner instruction 2026-08-20 (a hand-entered 16.5 h overnight row;
# June for 8865482786 went from $1,008.83 to $711.83). The VPS never saw the
# deletion, so a plain copy would silently resurrect it.
DEFAULT_SKIP_IDS = frozenset({19})

DEFAULT_LOCAL = Path("time_tracker.db")
DEFAULT_SNAPSHOT = Path("time_tracker_REMOTE.db")
DEFAULT_OUT = Path("time_tracker_MERGED.db")

# Keyed tables that must agree wherever they overlap. A disagreement here is a
# pay rate or an advance, which no script should silently pick a winner for.
KEYED_TABLES = {"users": "user_id", "advances": "id"}


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _rows_by_key(conn: sqlite3.Connection, table: str, key: str) -> dict:
    cursor = conn.execute(f"SELECT * FROM {table}")
    columns = [d[0] for d in cursor.description]
    index = columns.index(key)
    return {row[index]: row for row in cursor.fetchall()}, columns


def merge(
    local_path: Path,
    snapshot_path: Path,
    out_path: Path,
    skip_ids: set[int] | frozenset[int] = DEFAULT_SKIP_IDS,
) -> dict:
    """Local file plus the snapshot's new rows. Returns a summary."""
    local_path, snapshot_path, out_path = map(Path, (local_path, snapshot_path, out_path))

    if out_path.exists():
        raise SystemExit(
            f"Refusing to overwrite: {out_path} already exists. "
            "Move it aside, or pass a different --out."
        )
    for path in (local_path, snapshot_path):
        if not path.exists():
            raise SystemExit(f"Not found: {path}")

    # Work on a copy so the local database is never the thing being written to.
    shutil.copy2(local_path, out_path)

    summary = {"added": 0, "skipped": [], "kept_local_edits": 0, "new_rows": []}

    with _read_only(snapshot_path) as snap, sqlite3.connect(out_path) as out:
        # --- the keyed tables must agree where they overlap -----------------
        for table, key in KEYED_TABLES.items():
            snap_rows, columns = _rows_by_key(snap, table, key)
            out_rows, _ = _rows_by_key(out, table, key)
            conflicts = [
                k for k, row in snap_rows.items() if k in out_rows and out_rows[k] != row
            ]
            if conflicts:
                raise SystemExit(
                    f"Cannot merge {table}: {len(conflicts)} row(s) differ between the two "
                    f"copies ({key} {sorted(conflicts)}). These decide money -- reconcile "
                    "them by hand before merging."
                )
            fresh = [row for k, row in snap_rows.items() if k not in out_rows]
            if fresh:
                placeholders = ",".join("?" * len(columns))
                out.executemany(f"INSERT INTO {table} VALUES ({placeholders})", fresh)
                summary.setdefault(f"{table}_added", 0)
                summary[f"{table}_added"] = len(fresh)

        # --- timesheets: new ids only, minus the deliberate deletions -------
        snap_shifts, columns = _rows_by_key(snap, "timesheets", "id")
        out_shifts, _ = _rows_by_key(out, "timesheets", "id")

        summary["kept_local_edits"] = sum(
            1 for i, row in snap_shifts.items() if i in out_shifts and out_shifts[i] != row
        )

        to_add = []
        for shift_id, row in sorted(snap_shifts.items()):
            if shift_id in out_shifts:
                continue  # local wins, by owner ruling
            if shift_id in skip_ids:
                summary["skipped"].append(shift_id)
                continue
            to_add.append(row)
            summary["new_rows"].append(row)

        if to_add:
            placeholders = ",".join("?" * len(columns))
            out.executemany(f"INSERT INTO timesheets VALUES ({placeholders})", to_add)
        summary["added"] = len(to_add)
        out.commit()

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, default=DEFAULT_LOCAL)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    summary = merge(args.local, args.snapshot, args.out)

    print(f"Base    : {args.local}")
    print(f"Snapshot: {args.snapshot}")
    print(f"Merged  : {args.out}")
    print()
    print(f"  shifts brought in from the VPS : {summary['added']}")
    for row in summary["new_rows"]:
        print(f"      #{row[0]:<4} user {row[1]}  {row[2]}  {row[3]} -> {row[4] or '(open)'}")
    print(f"  local hand-corrections kept    : {summary['kept_local_edits']}")
    print(f"  deliberately-deleted, skipped  : {summary['skipped'] or 'none'}")
    for table in KEYED_TABLES:
        if summary.get(f"{table}_added"):
            print(f"  new {table} rows               : {summary[f'{table}_added']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
