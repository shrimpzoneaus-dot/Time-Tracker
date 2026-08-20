"""Mint a one-tap sign-in link without going through Telegram.

Useful before cutover: the bot has no webhook registered yet, so it cannot hand
out links, but the deployed web app is perfectly usable. This creates (or
reuses) a user and prints a URL that signs them in.

    python scripts/make_signin_link.py --user-id 6393446109 --name "Owner" --admin

Reads DATABASE_URL and PUBLIC_URL from .env. The link is single-use and expires
in 15 minutes.
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

from app.db import repo  # noqa: E402
from app.db.models import User  # noqa: E402


def build_engine(url: str):
    from sqlalchemy import create_engine

    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    connect_args = {}
    host = re.search(r"@([^/?:]+)", url)
    if host and "neon.tech" in host.group(1):
        # IPv6 to Neon hangs from some networks; pin the IPv4 address so a
        # local run cannot stall on a dead AAAA record.
        try:
            connect_args["hostaddr"] = socket.getaddrinfo(
                host.group(1), 5432, socket.AF_INET, socket.SOCK_STREAM
            )[0][4][0]
        except OSError:
            pass

    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True, help="Telegram user id")
    parser.add_argument("--name", default="", help="display name (new users only)")
    parser.add_argument("--admin", action="store_true", help="grant the ADMIN role")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set (.env)")
    if not public_url:
        raise SystemExit("PUBLIC_URL is not set (.env)")

    from sqlalchemy.orm import Session as OrmSession

    with OrmSession(build_engine(database_url)) as session:
        user = repo.upsert_user(session, args.user_id, args.name or str(args.user_id))
        if args.admin and user.role != repo.ROLE_ADMIN:
            user.role = repo.ROLE_ADMIN
        token = repo.create_magic_link(session, args.user_id)
        role = user.role
        name = user.full_name
        session.commit()

    print(f"user   : {name} ({args.user_id}) — {role}")
    print("expires: 15 minutes, single use")
    print()
    print(f"{public_url}/auth/{token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
