"""Empty the target database so the migration can run.

Smoke-testing the deployed app writes real rows — a user, a session, a shift.
`migrate_sqlite_to_neon.py` deliberately refuses to run against a target that
already holds timesheets, so those test rows would block the cutover. This
clears them.

    python scripts/reset_target_db.py            # show what is there
    python scripts/reset_target_db.py --yes      # delete it

The schema and alembic_version are left alone; only data is removed.

SAFETY: refuses outright if the target looks like it holds migrated production
data, because at that point it is the payroll history and not a test fixture.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Above these, the target is not a scratch database any more.
PRODUCTION_LOOKS_LIKE_TIMESHEETS = 10
DATA_TABLES = ("sessions", "edit_log", "rate_history", "advances", "timesheets", "users")


def is_production_looking(timesheets: int, advances: int) -> bool:
    """Does the target hold payroll history rather than test rows?

    A single advance is enough: smoke testing never records one, and an advance
    is money already handed to somebody.
    """
    return timesheets > PRODUCTION_LOOKS_LIKE_TIMESHEETS or advances > 0


def build_engine(url: str):
    from sqlalchemy import create_engine

    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    connect_args = {}
    host = re.search(r"@([^/?:]+)", url)
    if host and "neon.tech" in host.group(1):
        try:
            connect_args["hostaddr"] = socket.getaddrinfo(
                host.group(1), 5432, socket.AF_INET, socket.SOCK_STREAM
            )[0][4][0]
        except OSError:
            pass
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete")
    parser.add_argument(
        "--force",
        action="store_true",
        help="override the production-looking guard (you had better be sure)",
    )
    args = parser.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set (.env)")

    from sqlalchemy import text

    engine = build_engine(url)
    print(f"target: {url.split('@')[-1].split('?')[0]}")
    print()

    with engine.connect() as conn:
        counts = {t: conn.execute(text(f"select count(*) from {t}")).scalar() for t in DATA_TABLES}
        for table, n in counts.items():
            print(f"  {table:14} {n}")

        timesheets = counts["timesheets"]
        advances = counts["advances"]

        if not any(counts.values()):
            print("\nAlready empty. Nothing to do.")
            return 0

        looks_live = is_production_looking(timesheets, advances)
        if looks_live and not args.force:
            print(
                f"\nREFUSING: {timesheets} timesheets and {advances} advances is not a scratch "
                "database.\nThis looks like migrated payroll history. Pass --force only if you "
                "are certain."
            )
            return 1

        if not args.yes:
            print("\nDry run. Re-run with --yes to delete the rows above.")
            return 0

        # One statement so foreign keys never see a half-cleared state.
        conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
        conn.commit()

        remaining = {t: conn.execute(text(f"select count(*) from {t}")).scalar() for t in DATA_TABLES}
        print("\nCleared. Now:", remaining)
        print("Schema and alembic_version untouched — ready for the migration.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
