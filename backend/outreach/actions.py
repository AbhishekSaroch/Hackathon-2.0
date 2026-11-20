"""
Turn an approved BI-02 recommendation into the real action.

  recommendation channel   what happens
  whatsapp  (forgot)       an approved reminder template to the customer (WhatsApp, SMS or email: OUTREACH_CUSTOMER_CHANNEL), after the reminder gate
  dispute_team             a handoff email to the dispute team, and collection calls on the account are paused (collection_holds)
  telecall / field_visit / human   a handoff email to that team (callback task, field visit, human collector)

A recommendation that is blocked (a gate check fails, the channel is not configured) or fails to send stays "pending" so it can be retried.
Everything that happens is in the send log. A disputed account never gets a customer message.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import config, log, messages, sender
import db  # noqa: E402  (log.py put backend/db on the path)

DB = "main"
CUSTOMER_CHANNELS = ("whatsapp", "sms", "email")
CHANNEL_NAME = {"whatsapp": "WhatsApp", "sms": "SMS", "email": "Email"}
OK = ("sent", "mock_sent", "already_done")

SQL_CTX = """
SELECT r.id AS rec_id, r.account_id, r.channel AS rec_channel, r.action AS rec_action, r.reason AS rec_reason, r.status AS rec_status,
       a.label, a.confidence, a.reasoning_summary,
       ac.customer_name, ac.product, ac.branch, ac.dpd, ac.outstanding, ac.emi_amount
FROM recommendations r
LEFT JOIN assessments a ON a.assessment_id = r.source_ref AND a.tenant_id = r.tenant_id
JOIN accounts ac ON ac.account_id = r.account_id AND ac.tenant_id = r.tenant_id
WHERE r.tenant_id = :tenant_id AND r.source = 'bi02'
"""


def customer_channel(requested: str | None = None) -> str:
    v = (requested or os.getenv("OUTREACH_CUSTOMER_CHANNEL") or "whatsapp").strip().lower()
    return v if v in CUSTOMER_CHANNELS else "whatsapp"


def load(tenant: str, rec_id: int) -> dict | None:
    return db.query_one(tenant, SQL_CTX + " AND r.id = :id", {"id": int(rec_id)}, database=DB)


def load_pending(tenant: str, statuses: tuple = ("pending",)) -> list[dict]:
    marks = ", ".join(f":s{i}" for i in range(len(statuses)))
    return db.query(tenant, SQL_CTX + f" AND r.status IN ({marks}) ORDER BY r.id", {f"s{i}": s for i, s in enumerate(statuses)}, database=DB)


def will_do(ctx: dict, channel: str) -> str:
    if ctx["rec_channel"] == "whatsapp":
        return f"{CHANNEL_NAME[channel]} reminder to the customer"
    team = messages.TEAMS.get(ctx["rec_channel"], "team")
    return f"Email to {team}" + (" and pause collection calls" if ctx["rec_channel"] == "dispute_team" else "")


def _reminder(tenant: str, ctx: dict, now: datetime, channel: str) -> tuple[str, str, dict]:
    tid, text = messages.render_reminder(ctx["account_id"], ctx["customer_name"])
    gate = messages.reminder_gate(text, text, now, log.recent_customer_messages(tenant, ctx["account_id"]), ctx["label"])
    return tid, text, gate


def preview(tenant: str, ctx: dict, now: datetime | None = None, channel: str | None = None) -> dict:
    """What would happen, without sending anything (the gate is run so a blocked one is shown before the person confirms)."""
    now, channel = now or datetime.now(), customer_channel(channel)
    out = {"rec_id": ctx["rec_id"], "account_id": ctx["account_id"], "customer": ctx["customer_name"], "rec_channel": ctx["rec_channel"], "label": ctx["label"],
           "confidence": ctx["confidence"], "will_do": will_do(ctx, channel), "blocked": None, "already_done": False}
    purpose = "customer_reminder" if ctx["rec_channel"] == "whatsapp" else "team_handoff"
    out["already_done"] = log.already_done(tenant, ctx["rec_id"], purpose)
    if ctx["rec_channel"] == "whatsapp" and not out["already_done"]:
        gate = _reminder(tenant, ctx, now, channel)[2]
        if not gate["passed"]:
            out["blocked"] = gate["failed"]
        plan = config.plan(channel)
        if plan[0] == "blocked":
            out["blocked"] = (out["blocked"] or []) + [f"channel: {plan[1]}"]
    elif not out["already_done"]:
        plan = config.plan("email")
        if plan[0] == "blocked":
            out["blocked"] = [f"channel: {plan[1]}"]
    return out


def execute(tenant: str, rec_id: int, now: datetime | None = None, channel: str | None = None) -> dict:
    """Approve and do it. Returns what happened step by step. Never raises for a send problem."""
    now, channel = now or datetime.now(), customer_channel(channel)
    ctx = load(tenant, rec_id)
    if ctx is None:
        return {"rec_id": rec_id, "ok": False, "outcome": "not_found", "steps": [], "rec_status": None}
    base = {"rec_id": rec_id, "account_id": ctx["account_id"], "customer": ctx["customer_name"], "rec_channel": ctx["rec_channel"], "steps": []}
    if ctx["rec_status"] not in ("pending", "approved"):
        return {**base, "ok": False, "outcome": "not_actionable", "rec_status": ctx["rec_status"], "error": f"status is {ctx['rec_status']}"}

    steps = base["steps"]
    if ctx["rec_channel"] == "whatsapp":                                      # customer reminder
        if log.already_done(tenant, rec_id, "customer_reminder"):
            steps.append({"kind": "customer_reminder", "channel": channel, "status": "already_done"})
        else:
            tid, text, gate = _reminder(tenant, ctx, now, channel)
            if not gate["passed"]:
                why = "gate: " + ", ".join(gate["failed"])
                log.record(tenant, channel=channel, mode=config.mode(), status="blocked", rec_id=rec_id, account_id=ctx["account_id"], purpose="customer_reminder",
                           intended_for=messages.first_name(ctx["customer_name"]), error=why, message=text)
                steps.append({"kind": "customer_reminder", "channel": channel, "status": "blocked", "error": why, "gate_failed": gate["failed"], "template": tid})
            else:
                r = sender.send(tenant, channel, text, subject=f"Reminder for {messages.first_name(ctx['customer_name'])}", rec_id=rec_id, account_id=ctx["account_id"],
                                purpose="customer_reminder", intended_for=ctx["customer_name"])
                steps.append({"kind": "customer_reminder", "template": tid, **r})
    else:                                                                       # internal handoff
        team, subject, body = messages.team_email(ctx, now)
        r = sender.send(tenant, "email", body, subject=subject, rec_id=rec_id, account_id=ctx["account_id"], purpose="team_handoff", intended_for=team)
        steps.append({"kind": "team_handoff", "team": team, **r})
        if ctx["rec_channel"] == "dispute_team" and r["status"] in OK:
            steps.append({"kind": "pause_calls", "status": "paused" if log.add_hold(tenant, ctx["account_id"], rec_id, "dispute handed to the dispute team") else "already_paused"})

    ok = all(s["status"] in OK or s["status"] in ("paused", "already_paused") for s in steps)
    if ok:
        db.execute(tenant, "UPDATE recommendations SET status = 'approved' WHERE tenant_id = :tenant_id AND id = :id AND status IN ('pending', 'approved')", {"id": rec_id}, database=DB)
    outcome = ("already_done" if all(s["status"] == "already_done" for s in steps) else "sent" if any(s["status"] == "sent" for s in steps)
               else "mock" if ok else "blocked" if any(s["status"] == "blocked" for s in steps) else "failed")
    return {**base, "ok": ok, "outcome": outcome, "rec_status": "approved" if ok else ctx["rec_status"]}
