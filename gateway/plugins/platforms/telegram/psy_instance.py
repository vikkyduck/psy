"""Validate nonsecret PSY instance settings before enabling Telegram dispatch."""
import os
import re
from urllib.parse import urlsplit


def telegram_id(key, negative=False):
    raw = os.environ.get(key, "")
    if not re.fullmatch(r"-[1-9][0-9]*" if negative else r"[1-9][0-9]*", raw) or len(raw) > 17:
        raise ValueError(f"Invalid {key}")
    value = int(raw)
    if abs(value) >= 2**52:
        raise ValueError(f"Invalid {key}")
    return value


OWNER = telegram_id("PSY_OWNER_ID")
GROUP = telegram_id("PSY_GROUP_ID", negative=True)
BOT = telegram_id("PSY_BOT_ID")
if OWNER == BOT:
    raise ValueError("The configured bot and owner must be distinct")
BOT_USERNAME = os.environ.get("PSY_BOT_USERNAME", "")
if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]", BOT_USERNAME):
    raise ValueError("Invalid PSY_BOT_USERNAME")
PUBLIC_URL = os.environ.get("PSY_PUBLIC_URL", "").rstrip("/")
parts = urlsplit(PUBLIC_URL)
if (parts.scheme != "https" or not parts.hostname or parts.username is not None
        or parts.password is not None or parts.path or parts.query or parts.fragment
        or re.search(r"[\s\\#?]", PUBLIC_URL)):
    raise ValueError("PSY_PUBLIC_URL must be an HTTPS origin")
# Accessing the port catches malformed or out-of-range ports.
_ = parts.port


def require_bot(actual):
    if actual != BOT:
        raise RuntimeError("Connected Telegram bot does not match PSY_BOT_ID")
