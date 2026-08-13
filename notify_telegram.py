"""Tiny Telegram sender shared by master_pi.py and sync/self_checker.py.

Give this app its own bot rather than reusing one you already run elsewhere:
the token is the whole credential, so a shared bot means every script holding
it can post as the others. See README for how to create one.

Never raises — a Telegram outage must not fail the sync or the audit.
"""
import json
import os
import urllib.error
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# TELEGRAM_TOKEN_MONEYBADGER was the name before 1.0; still read so an existing
# .env keeps working. Remove the fallback once yours says TELEGRAM_BOT_TOKEN.
LEGACY_TOKEN_ENV = "TELEGRAM_TOKEN_MONEYBADGER"


def send_telegram(text: str, token_env: str = "TELEGRAM_BOT_TOKEN") -> bool:
    token = os.environ.get(token_env) or os.environ.get(LEGACY_TOKEN_ENV)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"  [telegram] {token_env}/TELEGRAM_CHAT_ID not configured — skipping notification.")
        return False
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token), data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError) as e:
        print(f"  [telegram] send failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    import env_file
    env_file.load()
    ok = send_telegram(" ".join(sys.argv[1:]) or "Money Badger — test notification")
    print("OK" if ok else "FAILED")
