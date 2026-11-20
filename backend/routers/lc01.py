"""
LC-01 API (ask-your-portfolio agent). The user comes from the X-User-Id header (demo login; a real product would use the user's
token). The lender scope is read from that user's record in the database, never from a parameter or from the question.

  WHO      GET  /lc01/users                demo users for the switcher
           GET  /lc01/me                   who am I: role, lenders
  CATALOG  GET  /lc01/metrics              the governed metric definitions (overlapping ones are marked)
  ASK      POST /lc01/ask                  {question, choice?}  -> answer | clarifying question | refusal (+ SQL, chart, explanation)
           GET  /lc01/examples             the public example questions (for my lender)
  HISTORY  GET  /lc01/history              my recent questions       GET /lc01/history/{id}   one full response
           GET  /lc01/audit                my refused requests (cross-lender, SQL, writes, injection)
  EVAL     GET  /lc01/evaluation           public scorecard and, once run, the hidden one

Errors: 401 no/unknown user, 400 bad request. A refusal is a normal 200 answer with status "refused" (and an audit row).
LC-01's modules are imported lazily, so BI-02 / BI-03 / LC-04 start-up and endpoints are unaffected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
router = APIRouter(prefix="/lc01", tags=["lc01"])
_M: dict = {}


def mods():
    if not _M:
        for p in (BACKEND / "db", BACKEND / "agents" / "lc01"):
            if str(p) not in sys.path:
                sys.path.append(str(p))
        import lc01_access, lc01_agent, lc01_registry, lc01_sources  # noqa: E401
        _M.update(A=lc01_access, G=lc01_agent, R=lc01_registry, X=lc01_sources)
    return _M


def who(x_user_id: str | None):
    m = mods()
    if not x_user_id:
        raise HTTPException(401, "send the X-User-Id header (demo login)")
    try:
        return m["A"].login(x_user_id)
    except m["A"].AccessError as e:
        raise HTTPException(401, str(e))


class AskBody(BaseModel):
    question: str
    choice: str | None = None            # metric id picked from a clarifying question


@router.get("/users")
def users():
    m = mods()
    return [dict(user_id=u["user_id"], name=u["display_name"], role=u["role"], lenders=u["lender_scope"]) for u in m["A"].USERS if u["user_id"] in m["A"].UI_USERS]


@router.get("/me")
def me(x_user_id: str | None = Header(None)):
    return mods()["A"].principal_json(who(x_user_id))


@router.get("/metrics")
def metrics(x_user_id: str | None = Header(None)):
    who(x_user_id)
    return mods()["R"].catalog()


@router.get("/sources")
def sources(x_user_id: str | None = Header(None)):
    """Which other modules' data I can ask about, the extra metrics, and how fresh the copy is (ask_all.db)."""
    p = who(x_user_id)
    X = mods()["X"]
    mine = X.sources_for(p)
    return dict(sources=[dict(id=s, **X.SOURCES[s]) for s in mine], metrics=[c for c in X.catalog() if c["source"] in mine],
                copy_exists=X.available(), synced_at=X.synced_at())


@router.post("/sync")
def sync(x_user_id: str | None = Header(None)):
    """Rebuild ask_all.db from BI-02, BI-03 and LC-04 (read-only on them). Needs access to at least one of those sources."""
    p = who(x_user_id)
    m = mods()
    if not m["X"].sources_for(p):
        raise HTTPException(403, "your user has no access to BI-02, BI-03 or LC-04 data")
    sys.path.append(str(DATA)) if str(DATA) not in sys.path else None
    import sync_all
    try:
        res = sync_all.build()
    except PermissionError as e:
        raise HTTPException(409, f"the copy is in use right now, try again in a moment ({e})")
    except SystemExit as e:
        raise HTTPException(500, str(e))
    m["X"]._vals.clear()
    return res


@router.post("/ask")
def ask(body: AskBody, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    if body.choice is not None and body.choice not in m["R"].METRICS:
        raise HTTPException(400, "choice is not a governed metric")
    return m["G"].ask(p.user_id, body.question, hint_metric=body.choice)


@router.get("/examples")
def examples(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    f = BACKEND / "agents" / "lc01" / "questions_public.json"
    qs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    extra = m["X"].examples(p)                                    # questions over BI-02 / BI-03 / LC-04 data, only for users who may read them
    if not m["X"].has_lender_warehouse(p):
        return extra
    key = p.user_id if any(q.get("user") == p.user_id for q in qs) else "u_csm"
    return [dict(id=q["id"], text=q["text"], kind=q["kind"]) for q in qs if q.get("user") == key and q["kind"] != "refuse"] + extra


@router.get("/history")
def history(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    return mods()["A"].history(p)


@router.get("/history/{log_id}")
def history_one(log_id: int, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    import db
    row = db.query_one(p.log_tenant, "SELECT response FROM qa_log WHERE tenant_id=:tenant_id AND user_id=:u AND id=:i", {"u": p.user_id, "i": log_id}, "lc01")
    if not row:
        raise HTTPException(404, "no such question in your history")
    return json.loads(row["response"])


@router.get("/audit")
def audit(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    return mods()["A"].audit_rows(p)


@router.get("/evaluation")
def evaluation(x_user_id: str | None = Header(None)):
    who(x_user_id)
    f = DATA / "lc01_eval_report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else dict(public=None, hidden=None, note="The evaluation has not been run yet.")
