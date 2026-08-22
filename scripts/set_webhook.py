"""Point Telegram at the deployed app, or take it back off.

This is the cutover switch. Until it runs, the rebuilt app receives nothing;
after it runs, every Telegram update goes to
`PUBLIC_URL/tg/<TELEGRAM_WEBHOOK_SECRET>` and the legacy polling bot can no
longer receive anything (Telegram allows a webhook OR polling, never both).

    python scripts/set_webhook.py            # show what is registered now
    python scripts/set_webhook.py --set      # cut over
    python scripts/set_webhook.py --delete   # roll back

Rollback is `--delete` followed by restarting the legacy service on the VPS:

    ssh root@67.219.100.235 'systemctl enable --now time-tracker-bot'

Secrets are read from .env and never printed. The webhook path itself contains
TELEGRAM_WEBHOOK_SECRET, so the registered URL is masked in output too --
anyone holding it can post fake updates to the app.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    # Explicit path: load_dotenv() resolves from the calling file's directory,
    # so a bare call silently finds nothing when run from elsewhere.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")


def _call(method: str, **params) -> dict:
    token = os.environ["BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)


def _mask(url: str) -> str:
    """Hide the secret path segment: it is a credential, not a location."""
    if not url:
        return "(none registered)"
    head, _, _ = url.rpartition("/")
    return f"{head}/<secret>"


def show() -> int:
    info = _call("getWebhookInfo").get("result", {})
    print(f"  webhook         : {_mask(info.get('url', ''))}")
    print(f"  pending updates : {info.get('pending_update_count')}")
    print(f"  last error      : {info.get('last_error_message', '-')}")
    return 0


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set", action="store_true", help="register the webhook (cut over)")
    group.add_argument("--delete", action="store_true", help="remove it (roll back)")
    args = parser.parse_args()

    if args.set:
        secret = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
        public = os.environ["PUBLIC_URL"].strip().rstrip("/")
        result = _call(
            "setWebhook",
            url=f"{public}/tg/{secret}",
            secret_token=secret,
            allowed_updates=json.dumps(["message", "callback_query"]),
        )
        print("setWebhook:", "OK" if result.get("ok") else result.get("description"))
        if not result.get("ok"):
            return 1
    elif args.delete:
        result = _call("deleteWebhook")
        print("deleteWebhook:", "OK" if result.get("ok") else result.get("description"))
        if not result.get("ok"):
            return 1

    return show()


if __name__ == "__main__":
    raise SystemExit(main())
