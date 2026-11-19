"""Validate nonsecret PSY instance settings before enabling Telegram dispatch.

The rules are those of psy_common.instance() in common/psy_common.py, which the upload side (also
inside this process) and the ops scripts load from /usr/local/lib/psy-common. This module keeps its
own copy only until the gateway's offline acceptance can load that file too;
gateway/tests/test_instance_rules.py holds the two to the same decisions and values."""
import os
import re
from urllib.parse import urlsplit


def telegram_id(key, negative=False):
    raw = os.environ.get(key, "")
    if not re.fullmatch(r"-[1-9][0-9]{0,15}" if negative else r"[1-9][0-9]{0,15}", raw) or abs(int(raw)) >= 2**52:
        raise ValueError(f"Invalid {key}")
    return int(raw)


def https_origin(raw):
    """'https://host[:port]' from an origin with at most one trailing slash and nothing else."""
    error = ValueError("PSY_PUBLIC_URL must be an HTTPS origin")
    if not raw.isascii() or any(c <= " " or c == "\x7f" or c in "\\#?" for c in raw):
        raise error
    try:
        parts = urlsplit(raw)
        port = parts.port   # malformed or out-of-range ports raise
    except ValueError:
        raise error from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment or port == 0):
        raise error
    return "https://" + parts.netloc


OWNER = telegram_id("PSY_OWNER_ID")
GROUP = telegram_id("PSY_GROUP_ID", negative=True)
BOT = telegram_id("PSY_BOT_ID")
if OWNER == BOT:
    raise ValueError("The configured bot and owner must be distinct")
BOT_USERNAME = os.environ.get("PSY_BOT_USERNAME", "")
if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]", BOT_USERNAME):
    raise ValueError("Invalid PSY_BOT_USERNAME")
PUBLIC_URL = https_origin(os.environ.get("PSY_PUBLIC_URL", ""))


def require_bot(actual):
    if actual != BOT:
        raise RuntimeError("Connected Telegram bot does not match PSY_BOT_ID")
