"""
Outreach API: approve and really send (email, WhatsApp, SMS), the summary, the "approve many" flow, and the send log.

  GET  /outreach/status                       mode, which channels are ready, where messages go (masked). No secrets.
  GET  /outreach/summary                      what is waiting, grouped, with counts and a one-line text
  POST /outreach/recommendations/{id}/send    approve one recommendation and do the action (WhatsApp reminder, or a handoff email)
  POST /outreach/bulk/preview                 {"command": "approve all who went to the dispute team"} -> the exact list, what is left out; nothing is sent
  POST /outreach/bulk/confirm                 {"plan_id": ...} -> act on exactly that list
  GET  /outreach/log                          the send log (audit trail)
  GET  /outreach/holds                        accounts whose collection calls are paused

Every message goes only to the test recipient from the settings. The BI-02 router (routers/bi02.py) is not touched: its approve endpoint still only records.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND / "db")]
from outreach import actions, bulk, config, log  # noqa: E402

router = APIRouter(prefix="/outreach", tags=["outreach"])
_SEND_LOCK = threading.Lock()


def get_tenant(x_tenant_id: str = Header("abc_bank")) -> str:
    return x_tenant_id


@router.get("/status")
def status():
    return {**config.status(), "batch_max": bulk.BATCH_MAX, "min_confidence": bulk.MIN_CONF, "customer_channel": actions.customer_channel()}


@router.get("/summary")
def summary(tenant: str = Depends(get_tenant)):
    return bulk.summary(tenant)


class SendBody(BaseModel):
    channel: str | None = None          # customer reminders only: whatsapp | sms | email


@router.post("/recommendations/{rec_id}/send")
def send_one(rec_id: int, body: SendBody | None = None, tenant: str = Depends(get_tenant)):
    with _SEND_LOCK:
        res = actions.execute(tenant, rec_id, channel=body.channel if body else None)
    if res["outcome"] == "not_found":
        raise HTTPException(404, "recommendation not found")
    if res["outcome"] == "not_actionable":
        raise HTTPException(409, res.get("error", "this recommendation can no longer be acted on"))
    return res


class PreviewBody(BaseModel):
    command: str


@router.post("/bulk/preview")
def bulk_preview(body: PreviewBody, tenant: str = Depends(get_tenant)):
    if not body.command.strip():
        raise HTTPException(400, "write what you want to approve")
    res = bulk.make_preview(tenant, body.command.strip())
    if not res["ok"]:
        raise HTTPException(400, res["error"])
    return res


class ConfirmBody(BaseModel):
    plan_id: str


@router.post("/bulk/confirm")
def bulk_confirm(body: ConfirmBody, tenant: str = Depends(get_tenant)):
    with _SEND_LOCK:
        res = bulk.confirm(tenant, body.plan_id)
    if not res["ok"]:
        raise HTTPException(410, res["error"])
    return res


@router.get("/log")
def send_log(limit: int = Query(50, ge=1, le=500), tenant: str = Depends(get_tenant)):
    return log.recent(tenant, limit)


@router.get("/holds")
def holds(tenant: str = Depends(get_tenant)):
    return log.holds(tenant)
