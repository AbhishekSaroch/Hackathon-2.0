"""
Outreach settings and safety rails.

Settings come from environment variables (the same backend/.env the Groq client uses; set OUTREACH_NO_DOTENV=1 to skip loading it).
Nothing in this module ever returns or prints a secret value: status() reports only set / not set, the NAME of the variable that was found,
and masked test recipients.

Variables
  OUTREACH_MODE         mock (default) | live        anything else means mock (fail closed)
  OUTREACH_DISABLED     1 = kill switch, nothing is sent in any mode
  OUTREACH_TEST_EMAIL   the ONLY address email messages go to
  OUTREACH_TEST_PHONE   the ONLY number WhatsApp and SMS go to, like +919876543210
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (like whatsapp:+14155238886), TWILIO_SMS_FROM
  SMTP_USER + SMTP_APP_PASSWORD (aliases below), SMTP_HOST (smtp.gmail.com), SMTP_PORT (587)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if os.getenv("OUTREACH_NO_DOTENV", "").strip().lower() not in ("1", "true", "yes"):
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")

CHANNELS = ("email", "whatsapp", "sms")

# a setting can be given under several names; the first one that is set wins (only the NAME is ever reported)
ALIASES = {
    "smtp_user": ["SMTP_USER", "GMAIL_USER", "GMAIL_ADDRESS", "GMAIL_EMAIL", "GMAIL_USERNAME", "EMAIL_USER"],
    "smtp_password": ["SMTP_APP_PASSWORD", "SMTP_PASSWORD", "GMAIL_APP_PASSWORD", "GMAIL_PASSWORD", "EMAIL_PASSWORD"],
    "twilio_sid": ["TWILIO_ACCOUNT_SID"],
    "twilio_token": ["TWILIO_AUTH_TOKEN"],
    "whatsapp_from": ["TWILIO_WHATSAPP_FROM"],
    "sms_from": ["TWILIO_SMS_FROM"],
    # optional: Twilio trial accounts and the WhatsApp sandbox only accept messages that use a Content Template (a SID like HX...).
    # The template body must be a single variable {{1}}; our approved text is passed in as that variable.
    "whatsapp_content_sid": ["TWILIO_WHATSAPP_CONTENT_SID"],
    "sms_content_sid": ["TWILIO_SMS_CONTENT_SID"],
    "test_email": ["OUTREACH_TEST_EMAIL"],
    "test_phone": ["OUTREACH_TEST_PHONE"],
}
SECRETS = ("smtp_password", "twilio_token", "twilio_sid", "smtp_user")        # never shown, scrubbed from error texts
NEEDS = {"email": ["smtp_user", "smtp_password", "test_email"],
         "whatsapp": ["twilio_sid", "twilio_token", "whatsapp_from", "test_phone"],
         "sms": ["twilio_sid", "twilio_token", "sms_from", "test_phone"]}


class OutreachBlocked(Exception):
    """A send was refused by a safety rail (kill switch, not configured, bad test recipient)."""


def found(key: str) -> tuple[str | None, str | None]:
    """(variable name, value) of the first alias that is set and not empty, or (None, None)."""
    for name in ALIASES[key]:
        v = (os.getenv(name) or "").strip()
        if v:
            return name, v
    return None, None


def get(key: str) -> str | None:
    return found(key)[1]


def mode() -> str:
    v = (os.getenv("OUTREACH_MODE") or "mock").strip().lower()
    return v if v in ("mock", "live") else "mock"            # a typo must never turn sending on


def disabled() -> bool:
    return (os.getenv("OUTREACH_DISABLED") or "").strip().lower() in ("1", "true", "yes")


def _valid_email(v: str | None) -> bool:
    return bool(v and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v))


def _valid_phone(v: str | None) -> bool:
    return bool(v and re.fullmatch(r"\+\d{10,15}", v))


def missing(channel: str) -> list[str]:
    """Names of the settings this channel still lacks (the first alias name for each), including an invalid test recipient."""
    out = [ALIASES[k][0] for k in NEEDS[channel] if not get(k)]
    if channel == "email" and get("test_email") and not _valid_email(get("test_email")):
        out.append("OUTREACH_TEST_EMAIL (not a valid email address)")
    if channel in ("whatsapp", "sms") and get("test_phone") and not _valid_phone(get("test_phone")):
        out.append("OUTREACH_TEST_PHONE (use + and country code, like +919876543210)")
    if channel == "whatsapp" and get("whatsapp_from") and not get("whatsapp_from").startswith("whatsapp:+"):
        out.append("TWILIO_WHATSAPP_FROM (must look like whatsapp:+14155238886)")
    return out


def plan(channel: str) -> tuple[str, str]:
    """What would happen to a message on this channel right now: ("mock" | "live" | "blocked", reason)."""
    if channel not in CHANNELS:
        return "blocked", f"unknown channel {channel!r}"
    if disabled():
        return "blocked", "kill switch is on (OUTREACH_DISABLED)"
    if mode() == "mock":
        return "mock", "mock mode: nothing leaves the system"
    gaps = missing(channel)
    if gaps:
        return "blocked", "live mode but not configured: " + ", ".join(gaps)
    return "live", "live: goes to the test recipient only"


def recipient_for(channel: str) -> str:
    """The ONLY address a message on this channel may go to. There is deliberately no parameter to choose another one."""
    if disabled():
        raise OutreachBlocked("kill switch is on")
    key = "test_email" if channel == "email" else "test_phone"
    v = get(key)
    if channel == "email" and not _valid_email(v) or channel in ("whatsapp", "sms") and not _valid_phone(v):
        raise OutreachBlocked(f"no valid test recipient for {channel} (set {ALIASES[key][0]})")
    return v


def mask(value: str | None) -> str | None:
    """a***@gmail.com  /  +91*****750: enough to recognise your own recipient, not enough to leak it."""
    if not value:
        return None
    if "@" in value:
        user, dom = value.split("@", 1)
        return f"{user[:1]}***@{dom}"
    return f"{value[:3]}{'*' * max(len(value) - 6, 3)}{value[-3:]}"


def scrub(text: str | None) -> str:
    """Remove every configured secret value from a text (provider error messages can echo credentials)."""
    out = text or ""
    for key in SECRETS:
        v = get(key)
        if v and len(v) >= 4:
            out = out.replace(v, "***")
    return out


def status() -> dict:
    """Everything a person needs to know about the setup, without a single secret."""
    channels = {}
    for c in CHANNELS:
        gaps = missing(c)
        mode_now, why = plan(c)
        channels[c] = {"configured": not gaps, "missing": gaps, "would_do": mode_now, "why": why}
    return {
        "mode": mode(), "kill_switch": disabled(),
        "test_recipient": {"email": mask(get("test_email")), "phone": mask(get("test_phone"))},
        "channels": channels,
        "settings_found_as": {k: found(k)[0] for k in ALIASES},          # variable NAMES only (None = not set)
    }
