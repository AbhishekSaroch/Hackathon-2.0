"""
send(): the one function every message goes through.

  1. config.plan(channel) decides: mock (nothing leaves), live (goes to the test recipient only) or blocked (kill switch, not configured)
  2. nothing is sent twice for the same recommendation and purpose
  3. the attempt is written to the send log whatever happens (sent, mock_sent, blocked, failed)
In live mode the text is prefixed with who it is really for, because the recipient is always the tester, not the customer.
"""
from __future__ import annotations

from . import config, log, providers


def _tag(intended_for: str | None) -> str:
    return f"[PortfolioIQ demo, for {intended_for}] " if intended_for else "[PortfolioIQ demo] "


def send(tenant: str, channel: str, body: str, *, subject: str | None = None, rec_id: int | None = None, account_id: str | None = None,
         purpose: str | None = None, intended_for: str | None = None) -> dict:
    if rec_id is not None and purpose and log.already_done(tenant, rec_id, purpose):
        return {"status": "already_done", "mode": None, "channel": channel, "provider_ref": None, "error": None}
    mode_now, why = config.plan(channel)
    common = dict(channel=channel, rec_id=rec_id, account_id=account_id, purpose=purpose, intended_for=intended_for)
    if mode_now == "blocked":
        log.record(tenant, mode=config.mode(), status="blocked", error=why, message=body, **common)
        return {"status": "blocked", "mode": config.mode(), "channel": channel, "provider_ref": None, "error": why}
    if mode_now == "mock":
        hint = config.mask(config.get("test_email" if channel == "email" else "test_phone"))
        log.record(tenant, mode="mock", status="mock_sent", recipient_hint=hint, message=body, **common)
        return {"status": "mock_sent", "mode": "mock", "channel": channel, "provider_ref": None, "error": None}
    text = _tag(intended_for) + body
    hint = config.mask(config.recipient_for(channel))
    try:
        if channel == "email":
            subj = subject or "message"
            ref = providers.send_email(subj if subj.startswith("[PortfolioIQ") else "[PortfolioIQ demo] " + subj, text)
        elif channel == "whatsapp":
            ref = providers.send_whatsapp(text)
        else:
            ref = providers.send_sms(text)
    except Exception as e:                                                     # fail closed: log it, tell the caller, never raise out of a batch
        err = config.scrub(str(e))
        log.record(tenant, mode="live", status="failed", recipient_hint=hint, error=err, message=text, **common)
        return {"status": "failed", "mode": "live", "channel": channel, "provider_ref": None, "error": err}
    log.record(tenant, mode="live", status="sent", recipient_hint=hint, provider_ref=ref, message=text, **common)
    return {"status": "sent", "mode": "live", "channel": channel, "provider_ref": ref, "error": None}
