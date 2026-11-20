"""
The three real senders: email (SMTP, Gmail app password), WhatsApp and SMS (Twilio REST API, no SDK).

A provider function takes only the TEXT. The recipient is never a parameter: it is config.recipient_for(channel), i.e. the test recipient.
Nothing here decides whether to send: sender.py does that (mode, kill switch, logging). Network access goes through two small functions
(`http_post`, `smtp_connect`) that tests replace, so the tests never touch the internet.
"""
from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config

TIMEOUT = 20
TWILIO = "https://api.twilio.com/2010-04-01/Accounts"


class ProviderError(Exception):
    pass


# --------------------------------------------------------------------------------------- network (replaced in tests)
def _basic(auth: tuple[str, str]) -> str:
    return "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()


def http_post(url: str, data: dict, auth: tuple[str, str]) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    req.add_header("Authorization", _basic(auth))
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _open(req)


def http_get(url: str, auth: tuple[str, str]) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", _basic(auth))
    return _open(req)


def _open(req) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"Twilio HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except Exception as e:
        raise ProviderError(f"{type(e).__name__}: {e}")


def smtp_connect(host: str, port: int):
    return smtplib.SMTP(host, port, timeout=TIMEOUT)


# --------------------------------------------------------------------------------------- senders
def send_email(subject: str, body: str) -> str:
    to, user = config.recipient_for("email"), config.get("smtp_user")
    password = (config.get("smtp_password") or "").replace(" ", "")          # Google shows app passwords in groups of four with spaces
    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))
    msg = EmailMessage()
    # A Date header and a Message-ID on the sender's own domain: mail without them, or with a made-up domain like ".local", is often sent to spam or
    # quarantine by company mail filters even though Gmail's server accepted it.
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    msg.set_content(body)
    try:
        with smtp_connect(host, port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)
    except Exception as e:
        raise ProviderError(f"email: {type(e).__name__}: {e}")
    return msg["Message-ID"]


def _twilio(channel: str, body: str) -> str:
    sid, token = config.get("twilio_sid"), config.get("twilio_token")
    to = config.recipient_for(channel)
    if channel == "whatsapp":
        frm, to = config.get("whatsapp_from"), "whatsapp:" + to
    else:
        frm = config.get("sms_from")
    template = config.get("whatsapp_content_sid" if channel == "whatsapp" else "sms_content_sid")
    data = {"To": to, "From": frm}
    if template:                                   # Content Template mode: our text is the single variable {{1}} (WhatsApp variables cannot hold line breaks)
        data["ContentSid"] = template
        data["ContentVariables"] = json.dumps({"1": " ".join(body.split())})
    else:
        data["Body"] = body
    try:
        res = http_post(f"{TWILIO}/{sid}/Messages.json", data, (sid, token))
    except ProviderError as e:
        if not template and ("21654" in str(e) or "572006" in str(e)):
            name = "TWILIO_WHATSAPP_CONTENT_SID" if channel == "whatsapp" else "TWILIO_SMS_CONTENT_SID"
            raise ProviderError(f"{e} | HINT: this Twilio account only sends Content Templates. Create a Text template whose body is just {{{{1}}}} "
                                f"in the Twilio console (Content Template Builder) and set {name}=HX...")
        raise
    if res.get("error_code") or res.get("status") in ("failed", "undelivered"):
        raise ProviderError(f"Twilio rejected the message: {res.get('error_code')} {res.get('error_message')}")
    return res.get("sid") or ""


def send_whatsapp(body: str) -> str:
    return _twilio("whatsapp", body)


def send_sms(body: str) -> str:
    return _twilio("sms", body)


def list_templates() -> list[dict]:
    """The Content Templates this Twilio account can use (read-only): sid, name, types, variables and a body preview. Never prints a secret."""
    sid, token = config.get("twilio_sid"), config.get("twilio_token")
    r = http_get("https://content.twilio.com/v1/Content?PageSize=50", (sid, token))
    out = []
    for c in r.get("contents", []):
        body = ""
        for t in (c.get("types") or {}).values():
            body = (t or {}).get("body") or body
        out.append({"sid": c.get("sid"), "name": c.get("friendly_name"), "types": sorted((c.get("types") or {}).keys()), "variables": c.get("variables"), "body": body})
    return out


def twilio_status(message_sid: str, wait: float = 5.0) -> dict:
    """What Twilio says happened to a message a few seconds later (delivered, failed, undelivered ... plus its error code)."""
    time.sleep(wait)
    sid, token = config.get("twilio_sid"), config.get("twilio_token")
    r = http_get(f"{TWILIO}/{sid}/Messages/{message_sid}.json", (sid, token))
    return {"status": r.get("status"), "error_code": r.get("error_code"), "error_message": config.scrub(r.get("error_message"))}
