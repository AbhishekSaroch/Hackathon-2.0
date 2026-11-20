"""
BI-03 API (pre-delinquency early warning). Same rules as BI-02: every query is tenant-scoped through db.py (database "bi03"),
tenant comes from the X-Tenant-Id header (demo only; in a real product it comes from the user's token).

  READ     GET  /bi03/summary                      the dashboard numbers
           GET  /bi03/flags                        flagged accounts (filters, sort, pages) with nudge and outcome
           GET  /bi03/flags/{id}                   one flag: signals, nudge + gate result, outcome, risk timeline, events
           GET  /bi03/accounts/{account_id}        any account: risk timeline, events, its flags
           GET  /bi03/nudges                       the approval queue (filter by status)
           GET  /bi03/thresholds                   alert thresholds (the active one, candidates)
           GET  /bi03/runs, /bi03/runs/{date}      what the agent did on a day (step log)
           GET  /bi03/metrics                      backtest report, hidden-set ledger, live counts
  ACT      POST /bi03/nudges/{id}/approve|skip     approve = (mock) send, refused unless the compliance gate passed
           POST /bi03/gate/test                    try any message on the compliance gate
           POST /bi03/replay/advance|reset         move the stream replay forward / back (the demo's "next day" button)
           POST /bi03/thresholds/tune              replay other thresholds on known outcomes, saves an INACTIVE candidate
           POST /bi03/thresholds/{version}/activate
           POST /bi03/outcomes/record              what happened to each flag (past the last public day needs include_hidden, which is ledgered)

Nothing is ever sent to a real customer. Everything the API shows is cut at the replay pointer: the future of the stream stays hidden.
BI-03's heavy modules (scikit-learn, the feature engine) are imported lazily, so BI-02 start-up and BI-02 endpoints are unaffected.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "db")]
import db  # noqa: E402

DB = "bi03"
DATA = BACKEND / "data"
router = APIRouter(prefix="/bi03", tags=["bi03"])
_M: dict = {}


def get_tenant(x_tenant_id: str = Header("abc_bank")) -> str:
    return x_tenant_id


def mods():
    """BI-03 modules, imported on first use (not at start-up)."""
    if not _M:
        for p in (str(BACKEND / "agents" / "bi03"), str(BACKEND / "gates")):
            if p not in sys.path:
                sys.path.append(p)
        import compliance, nudge, outcomes, replay  # noqa: E401
        _M.update(G=compliance, N=nudge, O=outcomes, RP=replay)
    return _M


def rows(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict("records")


def q(tenant: str, sql: str, **params) -> pd.DataFrame:
    return db.query_df(tenant, sql, params, database=DB)


def pointer(tenant: str) -> str:
    r = db.query_one(tenant, "SELECT value FROM bp_meta WHERE tenant_id = :tenant_id AND key = 'replay_date'", database=DB)
    if not r:
        raise HTTPException(404, "no BI-03 data for this tenant")
    return r["value"]


def jload(s):
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ read
@router.get("/summary")
def summary(tenant: str = Depends(get_tenant)):
    p = pointer(tenant)
    st = mods()["RP"].status(tenant)
    cnt = lambda sql: {r["k"]: r["n"] for r in rows(q(tenant, sql))}
    return {
        "replay": st,
        "current_accounts": int(q(tenant, "SELECT COUNT(*) n FROM risk_scores WHERE tenant_id = :tenant_id AND as_of_date = :d", d=p).n[0]),
        "flags_by_status": cnt("SELECT status k, COUNT(*) n FROM flags WHERE tenant_id = :tenant_id GROUP BY 1"),
        "nudges_by_status": cnt("SELECT status k, COUNT(*) n FROM nudges WHERE tenant_id = :tenant_id GROUP BY 1"),
        "open_flags_by_signal": cnt("SELECT COALESCE(dominant_signal, 'none') k, COUNT(*) n FROM flags WHERE tenant_id = :tenant_id AND status IN ('open', 'nudged') GROUP BY 1 ORDER BY 2 DESC"),
        "flags_today": int(q(tenant, "SELECT COUNT(*) n FROM flags WHERE tenant_id = :tenant_id AND flagged_on = :d", d=p).n[0]),
        "active_threshold": next(iter(rows(q(tenant, "SELECT threshold_version, value, top_pct, precision_top, lift, median_lead_days, notes FROM thresholds WHERE tenant_id = :tenant_id AND is_active = 1"))), None),
        "refresh_settled_flags": int(q(tenant, "SELECT COUNT(*) n FROM flags WHERE tenant_id = :tenant_id AND confirmed_by_refresh = 1").n[0]),
    }


@router.get("/flags")
def list_flags(status: str | None = None, signal: str | None = None, nudge_status: str | None = None, q_: str | None = Query(None, alias="q"),
               sort: str = "newest", page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), tenant: str = Depends(get_tenant)):
    sorts = {"newest": "f.flagged_on DESC, f.score DESC", "score": "f.score DESC", "oldest": "f.flagged_on ASC, f.flag_id ASC"}
    if sort not in sorts:
        raise HTTPException(400, f"sort must be one of {list(sorts)}")
    where, p = ["f.tenant_id = :tenant_id", "f.flagged_on <= :ptr"], {"ptr": pointer(tenant)}
    for col, val, key in (("f.status", status, "st"), ("f.dominant_signal", signal, "sig"), ("n.status", nudge_status, "ns")):
        if val:
            where.append(f"{col} = :{key}")
            p[key] = val
    if q_:
        where.append("(f.account_id LIKE :q OR LOWER(a.customer_name) LIKE :q)")
        p["q"] = f"%{q_.lower()}%"
    frm = ("FROM flags f JOIN bp_accounts a ON a.account_id = f.account_id AND a.tenant_id = f.tenant_id "
           "LEFT JOIN nudges n ON n.flag_id = f.flag_id AND n.tenant_id = f.tenant_id LEFT JOIN flag_outcomes o ON o.flag_id = f.flag_id AND o.tenant_id = f.tenant_id "
           "WHERE " + " AND ".join(where))
    total = int(q(tenant, f"SELECT COUNT(*) n {frm}", **p).n[0])
    df = q(tenant, "SELECT f.flag_id, f.account_id, a.customer_name, a.product, a.branch, f.flagged_on, f.score, f.dominant_signal, f.status, f.confirmed_by_refresh, "
                   "f.threshold_version, n.nudge_id, n.nudge_type, n.status AS nudge_status, o.went_dpd1, o.lead_time_days "
                   f"{frm} ORDER BY {sorts[sort]} LIMIT :lim OFFSET :off", **p, lim=page_size, off=(page - 1) * page_size)
    return {"total": total, "page": page, "page_size": page_size, "items": rows(df)}


def timeline(tenant: str, account_id: str, until: str) -> dict:
    scores = q(tenant, "SELECT as_of_date AS date, score, dominant_signal FROM risk_scores WHERE tenant_id = :tenant_id AND account_id = :a AND as_of_date <= :d ORDER BY as_of_date", a=account_id, d=until)
    ev = q(tenant, "SELECT event_date AS date, source, event_type, amount, value, narration FROM events WHERE tenant_id = :tenant_id AND account_id = :a AND event_date <= :d "
                   "AND ((source = 'statement' AND event_type = 'credit') OR source = 'bureau' OR (source = 'repayment' AND event_type = 'emi_paid')) ORDER BY event_date", a=account_id, d=until)
    fl = q(tenant, "SELECT flag_id, flagged_on, score, dominant_signal, status FROM flags WHERE tenant_id = :tenant_id AND account_id = :a AND flagged_on <= :d ORDER BY flagged_on", a=account_id, d=until)
    return {"risk": rows(scores), "events": rows(ev), "flags": rows(fl)}


@router.get("/flags/{flag_id}")
def flag_detail(flag_id: int, tenant: str = Depends(get_tenant)):
    p = pointer(tenant)
    f = q(tenant, "SELECT f.*, a.customer_name, a.product, a.branch, a.emi_amount, a.loan_amount FROM flags f JOIN bp_accounts a ON a.account_id = f.account_id AND a.tenant_id = f.tenant_id "
                  "WHERE f.tenant_id = :tenant_id AND f.flag_id = :i AND f.flagged_on <= :d", i=flag_id, d=p)
    if f.empty:
        raise HTTPException(404, "flag not found")
    flag = rows(f)[0]
    nudge = next(iter(rows(q(tenant, "SELECT * FROM nudges WHERE tenant_id = :tenant_id AND flag_id = :i ORDER BY nudge_id DESC", i=flag_id))), None)
    if nudge:
        nudge["gate_result"] = jload(nudge["gate_result"])
    outcome = next(iter(rows(q(tenant, "SELECT went_dpd1, dpd1_date, lead_time_days, evaluated_on FROM flag_outcomes WHERE tenant_id = :tenant_id AND flag_id = :i", i=flag_id))), None)
    signals = jload(flag.pop("signals_json"))
    return {"flag": flag, "signals": signals, "nudge": nudge, "outcome": outcome, "timeline": timeline(tenant, flag["account_id"], p), "replay_date": p}


@router.get("/accounts/{account_id}")
def account_view(account_id: str, tenant: str = Depends(get_tenant)):
    a = q(tenant, "SELECT account_id, customer_name, product, branch, loan_amount, emi_amount, outstanding FROM bp_accounts WHERE tenant_id = :tenant_id AND account_id = :a", a=account_id)
    if a.empty:
        raise HTTPException(404, "account not found")
    p = pointer(tenant)
    return {"account": rows(a)[0], "replay_date": p, **timeline(tenant, account_id, p)}


@router.get("/nudges")
def list_nudges(status: str = "pending_approval", page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), tenant: str = Depends(get_tenant)):
    p = pointer(tenant)
    frm = ("FROM nudges n JOIN flags f ON f.flag_id = n.flag_id AND f.tenant_id = n.tenant_id JOIN bp_accounts a ON a.account_id = n.account_id AND a.tenant_id = n.tenant_id "
           "WHERE n.tenant_id = :tenant_id AND n.status = :st AND f.flagged_on <= :d")
    total = int(q(tenant, f"SELECT COUNT(*) n {frm}", st=status, d=p).n[0])
    df = q(tenant, "SELECT n.nudge_id, n.flag_id, n.account_id, a.customer_name, n.nudge_type, n.template_id, n.message, n.status, n.gate_passed, n.gate_result, n.created_at, n.sent_at, "
                   f"n.dispatch_ref, f.dominant_signal, f.score {frm} ORDER BY n.nudge_id DESC LIMIT :lim OFFSET :off", st=status, d=p, lim=page_size, off=(page - 1) * page_size)
    items = rows(df)
    for it in items:
        g = jload(it.pop("gate_result")) or {}
        it["gate"] = {"passed": g.get("passed"), "failed": g.get("failed", []), "attempts": len(g.get("attempts", [])), "checks": g.get("checks", [])}
    return {"total": total, "page": page, "page_size": page_size, "items": items}


class ApproveBody(BaseModel):
    now: bool = False            # False = simulated time (the nudge's scheduled time); True = the real clock (refused outside 08:00-19:00)


@router.post("/nudges/{nudge_id}/approve")
def approve_nudge(nudge_id: int, body: ApproveBody | None = None, tenant: str = Depends(get_tenant)):
    N = mods()["N"]
    try:
        return N.nudge_dispatch(tenant, nudge_id, send_at=datetime.now() if (body and body.now) else None)
    except N.GateError as e:
        raise HTTPException(404 if "does not exist" in str(e) else 409, str(e))


@router.post("/nudges/{nudge_id}/skip")
def skip_nudge(nudge_id: int, tenant: str = Depends(get_tenant)):
    n = db.execute(tenant, "UPDATE nudges SET status = 'skipped' WHERE tenant_id = :tenant_id AND nudge_id = :i AND status = 'pending_approval'", {"i": nudge_id}, database=DB)
    if not n:
        raise HTTPException(409, "only a nudge that is waiting for approval can be skipped")
    return {"nudge_id": nudge_id, "status": "skipped"}


class GateBody(BaseModel):
    message: str
    template_id: str | None = None
    send_hour: int = 10
    recent_nudges: int = 0
    account_is_current: bool = True


@router.post("/gate/test")
def gate_test(body: GateBody, tenant: str = Depends(get_tenant)):
    """Try ANY message on the compliance gate (the 'test the gate' button). The message is compared with an approved template filled with sample values."""
    m = mods()
    tpl = q(tenant, "SELECT template_id, body, language FROM service_templates WHERE tenant_id = :tenant_id AND template_id = :t",
            t=body.template_id or "T-REM-H1")
    if tpl.empty:
        raise HTTPException(404, "unknown template")
    t = tpl.iloc[0]
    expected = m["N"].render(t["body"], {"name": "Asha", "emi_day": 12, "new_day": 20}, t["language"])
    r = m["G"].evaluate(body.message, expected, datetime(2026, 8, 10, max(0, min(23, body.send_hour)), 0), body.account_is_current, body.recent_nudges)
    return {**r, "compared_with_template": t["template_id"], "expected_message": expected}


@router.get("/thresholds")
def list_thresholds(tenant: str = Depends(get_tenant)):
    return rows(q(tenant, "SELECT threshold_version, value, top_pct, precision_top, lift, median_lead_days, is_active, notes, created_at FROM thresholds WHERE tenant_id = :tenant_id ORDER BY is_active DESC, created_at DESC"))


class TuneBody(BaseModel):
    cutoff_day: int | None = None
    include_hidden: bool = False


@router.post("/thresholds/tune")
def tune(body: TuneBody | None = None, tenant: str = Depends(get_tenant)):
    O = mods()["O"]
    body = body or TuneBody()
    try:
        res = O.tune_threshold(tenant, body.cutoff_day, body.include_hidden)
    except O.HiddenPeriodError as e:
        raise HTTPException(403, str(e))
    if "error" in res:
        raise HTTPException(409, res["error"])
    return {**{k: v for k, v in res.items() if k != "table"}, "table": rows(res["table"])}


@router.post("/thresholds/{version}/activate")
def activate(version: str, tenant: str = Depends(get_tenant)):
    try:
        return mods()["O"].activate_threshold(tenant, version)
    except ValueError as e:
        raise HTTPException(404, str(e))


class OutcomeBody(BaseModel):
    as_of_day: int | None = None
    include_hidden: bool = False


@router.post("/outcomes/record")
def record_outcomes(body: OutcomeBody | None = None, tenant: str = Depends(get_tenant)):
    O = mods()["O"]
    body = body or OutcomeBody()
    try:
        return O.record_outcomes(tenant, body.as_of_day, body.include_hidden)
    except O.HiddenPeriodError as e:
        raise HTTPException(403, str(e))


# ------------------------------------------------------------------ replay
@router.get("/replay")
def replay_status(tenant: str = Depends(get_tenant)):
    return mods()["RP"].status(tenant)


class ResetBody(BaseModel):
    to_day: int


class AdvanceBody(BaseModel):
    days: int = 1
    confirm: bool = True


@router.post("/replay/reset")
def replay_reset(body: ResetBody, tenant: str = Depends(get_tenant)):
    RP = mods()["RP"]
    try:
        return RP.reset_to_day(tenant, body.to_day)
    except RP.ReplayError as e:
        raise HTTPException(400, str(e))


@router.post("/replay/advance")
def replay_advance(body: AdvanceBody | None = None, tenant: str = Depends(get_tenant)):
    RP, body = mods()["RP"], body or AdvanceBody()
    if RP.LOCK.locked():
        raise HTTPException(409, "a replay action is already running")
    try:
        return RP.advance(tenant, body.days, body.confirm)
    except RP.ReplayError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ demo data: load a CSV, run the analysis
def demo_mod():
    """backend/data/demo_accounts.py (imported on first use). It holds the CSV loader, the validation and the scoring of the loaded accounts."""
    if str(DATA) not in sys.path:
        sys.path.append(str(DATA))
    import demo_accounts
    return demo_accounts


def demo_run(tenant: str, action):
    """Run one demo-data action while no replay action runs, then drop the cached feature engine so the next 'advance' sees the new rows."""
    RP, D = mods()["RP"], demo_mod()
    pointer(tenant)                                                  # 404 if the tenant has no BI-03 data
    if RP.LOCK.locked():
        raise HTTPException(409, "a replay action is already running, try again in a moment")
    with RP.LOCK:
        try:
            out = action(D)
        except D.DemoError as e:
            raise HTTPException(400, str(e))
        RP._ENGINES.pop(tenant, None)
    return out


@router.get("/demo")
def demo_status(tenant: str = Depends(get_tenant)):
    D = demo_mod()
    pointer(tenant)
    return {**D.status(tenant), "csv": D.csv_info()}


@router.get("/demo/template")
def demo_template():
    """A zip with the three CSV files (the sample accounts) and a README: edit, then upload through POST /demo/load."""
    return Response(demo_mod().template_zip(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="portfolioiq_bi03_template.zip"'})


@router.get("/demo/bulk-sample")
def demo_bulk_sample(n: int = Query(200, ge=10, le=1000)):
    """A bulk sample for the same upload: n generated accounts (ids DEMO-B0001...), same three files and README."""
    return Response(demo_mod().bulk_zip(n), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="portfolioiq_bi03_bulk_{n}_accounts.zip"'})


class DemoLoadBody(BaseModel):
    accounts: str | None = None        # text of bp_accounts.csv
    events: str | None = None          # text of events.csv
    snapshots: str | None = None       # text of bureau_snapshots.csv


@router.post("/demo/load")
def demo_load(body: DemoLoadBody | None = None, tenant: str = Depends(get_tenant)):
    """CSV files -> checks -> database. Without a body: the sample files in backend/data/csv_bi03_demo. With a body: the user's three CSV texts.
    If a check fails nothing stays loaded."""
    texts = {k: v for k, v in (body.model_dump() if body else {}).items() if v}
    if texts and len(texts) < 3:
        raise HTTPException(400, "Choose all three files: bp_accounts.csv, events.csv and bureau_snapshots.csv")
    if any(len(v) > 20_000_000 for v in texts.values()):
        raise HTTPException(400, "A file is larger than 20 MB")

    def act(D):
        res = D.load_uploaded(tenant, texts) if texts else {"rows_loaded": D.load_csvs(tenant), "warnings": []}
        checks = D.validate(tenant)
        if not all(c["ok"] for c in checks):
            D.remove(tenant)
            raise D.DemoError("Validation failed, nothing was loaded: " + ", ".join(c["name"] for c in checks if not c["ok"]))
        return {**res, "checks": checks}
    return {**demo_run(tenant, act), **demo_mod().status(tenant)}


@router.post("/demo/analyze")
def demo_analyze(tenant: str = Depends(get_tenant)):
    """Run the agent's daily loop over the loaded accounts for every day up to the replay pointer (safe to run again)."""
    res = demo_run(tenant, lambda D: D.score_and_nudge(tenant))
    return {"result": res, **demo_mod().status(tenant)}


@router.post("/demo/remove")
def demo_remove(tenant: str = Depends(get_tenant)):
    removed = demo_run(tenant, lambda D: D.remove(tenant))
    return {"removed": removed, **demo_mod().status(tenant)}


# ------------------------------------------------------------------ agent log and metrics
@router.get("/runs")
def list_runs(limit: int = Query(15, ge=1, le=100), tenant: str = Depends(get_tenant)):
    return rows(q(tenant, "SELECT run_id, agent_name, entity_id, status, started_at, finished_at FROM agent_runs WHERE tenant_id = :tenant_id ORDER BY entity_id DESC, started_at DESC LIMIT :n", n=limit))


@router.get("/runs/{date}")
def run_detail(date: str, tenant: str = Depends(get_tenant)):
    run = q(tenant, "SELECT run_id, entity_id, status FROM agent_runs WHERE tenant_id = :tenant_id AND agent_name = 'bi03_daily' AND entity_id = :d ORDER BY started_at DESC LIMIT 1", d=date)
    if run.empty:
        raise HTTPException(404, "no daily run for that date")
    steps = rows(q(tenant, "SELECT step_no, tool, input, output FROM agent_steps WHERE tenant_id = :tenant_id AND run_id = :r ORDER BY step_no", r=run.run_id[0]))
    for s in steps:
        s["input"], s["output"] = jload(s["input"]), jload(s["output"])
    return {"run": rows(run)[0], "steps": steps}


@router.get("/metrics")
def metrics(tenant: str = Depends(get_tenant)):
    rep_path, led_path = DATA / "bi03_backtest_report.json", DATA / "bi03_hidden_eval_ledger.json"
    report = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
    ledger = json.loads(led_path.read_text(encoding="utf-8")) if led_path.exists() else []
    cv = report.get("cv", {})
    return {
        "backtest": {"hidden": report.get("hidden"), "public_rehearsal_chosen_model": cv.get("chosen"),
                     "public_baselines": cv.get("baselines"), "confirm_public_ab": report.get("confirm_public_ab"),
                     "note": "The hidden-period result was measured WITHOUT the borderline-confirmation step; that step was only validated on public data."},
        "hidden_set_looks": ledger,
        "outcomes_recorded": rows(q(tenant, "SELECT COALESCE(CAST(went_dpd1 AS TEXT), 'not known yet') k, COUNT(*) n FROM flag_outcomes WHERE tenant_id = :tenant_id GROUP BY 1")),
    }
