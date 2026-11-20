"""
LC-04 API (self-building operations command dashboard). Everything a user reads goes through the role layer:
the user comes from the X-User-Id header (demo login; a real product would use the user's token), the lender scope is read from
that user's record in the database, never from a parameter, and the numbers come from the governed metric definitions.

  WHO      GET  /lc04/users                         demo users for the role switcher
           GET  /lc04/me                            who am I: role, lenders, metrics and dimensions I may use
  CATALOG  GET  /lc04/metrics                       the governed metrics this role may use (definition, unit, dimensions)
  CLOCK    GET  /lc04/clock                         the simulated clock, whether the replay is prepared, counts
           POST /lc04/replay/prepare                detect + drill down over the stream (cached; takes a minute the first time)
           POST /lc04/replay/advance                move the clock (minutes, default 15) and write what the agent found by then
           POST /lc04/replay/reset                  go back to a time: everything produced later disappears
  VIEWS    POST /lc04/dashboards/generate           role + plain-language request -> validated spec (+ notes, source llm|fallback)
           GET  /lc04/dashboards/{id}               a saved spec           GET /lc04/dashboards/{id}/data   its numbers at the clock
           POST /lc04/dashboards/rebuild            rebuild a view around an incident
           GET  /lc04/requests                      the public request set (examples)
  FINDINGS GET  /lc04/incidents                     incidents this role knows about (alerts; leadership/heads also see "watched" ones)
           GET  /lc04/incidents/{id}                the annotated view: driving segment, evidence, counterfactual, trace, alerts
           GET  /lc04/alerts                        the alerts addressed to this role (two lines + link; mock, nothing is sent)
  EVAL     GET  /lc04/evaluation                    public-period scorecard (and the hidden one once it has been run)

Errors: 401 no/unknown user, 403 outside the role or scope, 400 not a governed metric / bad request, 404 unknown id, 409 replay not prepared.
LC-04's modules are imported lazily, so BI-02 / BI-03 start-up and endpoints are unaffected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "db")]
import db  # noqa: E402

DB = "lc04"
DATA = BACKEND / "data"
router = APIRouter(prefix="/lc04", tags=["lc04"])
_M: dict = {}


def mods():
    """LC-04 modules, imported on first use (appended to the path so nothing of BI-02 / BI-03 is shadowed)."""
    if not _M:
        for p in (BACKEND / "metrics", BACKEND / "agents" / "lc04"):
            if str(p) not in sys.path:
                sys.path.append(str(p))
        import access, registry, render, spec, lc_runner, request_set  # noqa: E401
        _M.update(A=access, R=registry, RN=render, SP=spec, RU=lc_runner, RS=request_set, GEN=spec.SpecGenerator(spec.default_llm()))
        import threading                                                    # warm the replay timeline so the first click is fast
        threading.Thread(target=lambda: lc_runner.is_prepared() and lc_runner.prepare(verbose=False), daemon=True).start()
    return _M


def who(x_user_id: str | None = Header(None)):
    m = mods()
    if not x_user_id:
        raise HTTPException(401, "send the X-User-Id header (demo login)")
    try:
        return m["A"].login(x_user_id)
    except m["A"].AccessError as e:
        raise HTTPException(401, str(e))


def guarded(fn):
    """Map the governance errors to HTTP codes."""
    m = mods()
    try:
        return fn()
    except m["A"].AccessError as e:
        raise HTTPException(403, str(e))
    except m["R"].MetricError as e:
        raise HTTPException(400, str(e))


def principal_json(p):
    return dict(user_id=p.user_id, name=p.display_name, role_id=p.role_id, role=p.role_name, scope_type=p.scope_type,
                lenders=list(p.lenders), metrics=len(p.metrics), dims=sorted(p.dims))


# ---------------------------------------------------------------------------------------------- who / catalog
@router.get("/users")
def users():
    return [dict(user_id=r["user_id"], name=r["display_name"], role_id=r["role_id"])
            for r in db.query("GLOBAL", "SELECT user_id, display_name, role_id FROM lc_users WHERE tenant_id=:tenant_id ORDER BY rowid", database=DB)]


@router.get("/me")
def me(x_user_id: str | None = Header(None)):
    return principal_json(who(x_user_id))


@router.get("/metrics")
def metrics(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    return guarded(lambda: mods()["A"].visible_catalog(p))


# ---------------------------------------------------------------------------------------------- clock / replay
def _status():
    m = mods()
    s = m["RU"].status()
    s["prepared"] = m["RU"].is_prepared()
    s["demo_legal_day"] = m["RU"].meta("demo_legal_day")              # the legal-communications demo day (None when it is not seeded)
    return s


@router.get("/clock")
def clock(x_user_id: str | None = Header(None)):
    who(x_user_id)
    return _status()


def _admin(p):
    if p.role_id not in ("cn_leadership", "ops_head"):
        raise HTTPException(403, "only leadership and the operations head control the replay clock")


@router.post("/replay/prepare")
def replay_prepare(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    _admin(p)
    tl = mods()["RU"].prepare(verbose=False)
    return dict(incidents=len(tl["incidents"]), alert_stages=sum(len(i["stages"]) for i in tl["incidents"]), **_status())


class Advance(BaseModel):
    minutes: int = 15


@router.post("/replay/advance")
def replay_advance(body: Advance, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    _admin(p)
    if not 1 <= body.minutes <= 24 * 60:
        raise HTTPException(400, "minutes must be between 1 and 1440")
    if not mods()["RU"].is_prepared():
        raise HTTPException(409, "the replay is not prepared yet: POST /lc04/replay/prepare first")
    return mods()["RU"].advance(body.minutes)


class Reset(BaseModel):
    to: str


@router.post("/replay/reset")
def replay_reset(body: Reset, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    _admin(p)
    if not mods()["RU"].is_prepared():
        raise HTTPException(409, "the replay is not prepared yet: POST /lc04/replay/prepare first")
    try:
        t = pd.Timestamp(body.to)
    except Exception:
        raise HTTPException(400, "bad time")
    RU = mods()["RU"]
    if not RU.STREAM_START <= t <= RU.stream_end():
        raise HTTPException(400, "time outside the stream")
    return RU.reset(t)


# ---------------------------------------------------------------------------------------------- views
class Generate(BaseModel):
    request: str | None = None
    use_llm: bool = True


@router.post("/dashboards/generate")
def generate(body: Generate, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    text = (body.request or "").strip()[:500]
    out = guarded(lambda: m["GEN"].generate(p, text, use_llm=body.use_llm))
    out["dashboard_id"] = m["GEN"].save(p, text, out)
    out["clock"] = str(m["RU"].get_clock())
    return out


def _own_dashboard(p, dashboard_id):
    m = mods()
    rows = []
    for t in list(p.lenders) + ["GLOBAL"]:
        rows += db.query(t, "SELECT * FROM dashboards WHERE tenant_id=:tenant_id AND dashboard_id=:i AND user_id=:u",
                         {"i": dashboard_id, "u": p.user_id}, DB)
    if not rows:
        raise HTTPException(404, "no such view")
    r = rows[0]
    return dict(dashboard_id=r["dashboard_id"], request=r["request_text"], source=r["source"], spec=json.loads(r["spec"]), created_at=r["created_at"])


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: int, x_user_id: str | None = Header(None)):
    return _own_dashboard(who(x_user_id), dashboard_id)


@router.get("/dashboards/{dashboard_id}/data")
def dashboard_data(dashboard_id: int, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    d = _own_dashboard(p, dashboard_id)
    m = mods()
    return guarded(lambda: dict(dashboard_id=dashboard_id, **m["RN"].render(p, d["spec"], m["RU"].get_clock())))


class Rebuild(BaseModel):
    dashboard_id: int
    incident_id: str


@router.post("/dashboards/rebuild")
def rebuild(body: Rebuild, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    base = _own_dashboard(p, body.dashboard_id)
    inc = _incident_for_rebuild(p, body.incident_id)
    out = guarded(lambda: m["GEN"].rebuild(p, base["spec"], inc))
    out["ms"] = 0
    out["dashboard_id"] = m["GEN"].save(p, f"rebuilt around {body.incident_id}", out)
    return out


@router.get("/requests")
def requests_examples(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    return [dict(id=r["request_id"], text=r["request_text"]) for r in
            db.query("GLOBAL", "SELECT request_id, request_text FROM dashboard_requests WHERE tenant_id=:tenant_id AND split='public' AND role_id=:r",
                     {"r": p.role_id}, DB)]


# ---------------------------------------------------------------------------------------------- incidents / alerts
def _alert_rows(p, limit=200):
    m = mods()
    rows = m["A"].scoped_query(p, "SELECT * FROM alerts WHERE tenant_id=:tenant_id AND audience_role=:role",
                               {"role": p.role_id}, DB)
    return sorted(rows, key=lambda r: (r["created_at"], r["alert_id"]), reverse=True)[:limit]


@router.get("/alerts")
def alerts(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    seen, out = set(), []
    for r in _alert_rows(p):
        k = (r["incident_id"], r["created_at"], r["line1"])
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(incident_id=r["incident_id"], at=r["created_at"], audience=r["audience_role"], line1=r["line1"], line2=r["line2"],
                        link=r["link"], lender=r["tenant_id"] if p.role_id in ("lender_rm", "lender_user") else None))
    return out


def _incident_members(incident_id):
    """All anomaly rows of an incident (every tenant incl. GLOBAL): platform-level, for leadership / heads only."""
    rows = []
    for t in ["GLOBAL", "L1", "L2", "L3", "L4", "L5"]:
        rows += db.query(t, "SELECT *, tenant_id AS t FROM anomalies WHERE tenant_id=:tenant_id AND incident_id=:i", {"i": incident_id}, DB)
    return rows


@router.get("/incidents")
def incidents(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    out = {}
    for r in _alert_rows(p, 1000):
        i = out.setdefault(r["incident_id"], dict(incident_id=r["incident_id"], first_alert_at=r["created_at"], line1=r["line1"],
                                                   line2=r["line2"], link=r["link"], alerted=True, status=None))
        i["first_alert_at"] = min(i["first_alert_at"], r["created_at"])
    if p.scope_type == "all_lenders":                                       # platform roles also see incidents that were only watched
        for t in ["GLOBAL"] + list(p.lenders):
            for r in db.query(t, "SELECT incident_id, metric_id, detected_at, status FROM anomalies WHERE tenant_id=:tenant_id", database=DB):
                if r["metric_id"] not in p.metrics:                          # only incidents about metrics this role may see
                    continue
                i = out.setdefault(r["incident_id"], dict(incident_id=r["incident_id"], first_alert_at=None, line1=None, line2=None,
                                                          link=f"/command/incidents/{r['incident_id']}", alerted=False, status=None))
                i["detected_at"] = min(filter(None, [i.get("detected_at"), r["detected_at"]]))
                i["status"] = "open" if "open" in (i["status"], r["status"]) else r["status"]
                i["detections"] = i.get("detections", 0) + 1
    for i in out.values():
        if i["status"] is None:
            i["status"] = "open"
    return sorted(out.values(), key=lambda i: i.get("first_alert_at") or i.get("detected_at") or "", reverse=True)


def _incident_for_rebuild(p, incident_id):
    d = incident_detail(incident_id, p.user_id)
    f = d.get("finding")
    if not f:
        raise HTTPException(404, "no finding for that incident")
    return dict(incident_id=incident_id, metric=f["metric"], segment=f["segment"], parent=f["parent"], metrics=d.get("metrics", [f["metric"]]))


def _compliance_detail(p, out, incident_id, members, platform):
    """Legal-window incident: platform roles get the rule, the evidence and the trace; a lender role gets its own alert lines and the
    sender's name only (the totals and the other lenders' figures stay out)."""
    ann = db.query("GLOBAL", "SELECT * FROM annotations WHERE tenant_id=:tenant_id AND anomaly_id IN "
                             "(SELECT anomaly_id FROM anomalies WHERE tenant_id=:tenant_id AND incident_id=:i) ORDER BY annotation_id DESC",
                   {"i": incident_id}, DB)
    if not ann:
        raise HTTPException(404, "no such incident for your role")
    ev = json.loads(ann[0]["evidence"])
    out["detected_at"] = ann[0]["created_at"]
    out["metrics"] = [ev["metric"]]
    if not platform:
        out["finding"] = dict(metric=ev["metric"], segment=ev["segment"], parent=ev["parent"], direction="up")
        return out
    out["finding"] = {**ev, "headline": ann[0]["headline"]}
    out["detections"] = [dict(metric=r["metric_id"], segment=ev["segment"], direction="up", score=r["score"], window_buckets=r["buckets_persisted"],
                              detected_at=r["detected_at"]) for r in members]
    run = db.query("GLOBAL", "SELECT run_id FROM agent_runs WHERE tenant_id=:tenant_id AND entity_id=:i", {"i": incident_id}, DB)
    out["trace"] = [dict(step=r["step_no"], tool=r["tool"], input=json.loads(r["input"]), output=json.loads(r["output"]))
                    for r in db.query("GLOBAL", "SELECT * FROM agent_steps WHERE tenant_id=:tenant_id AND run_id=:r ORDER BY step_no",
                                      {"r": run[0]["run_id"]}, DB)] if run else []
    return out


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str, x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    m = mods()
    mine = [r for r in _alert_rows(p, 2000) if r["incident_id"] == incident_id]
    platform = p.scope_type == "all_lenders"
    members = [r for r in _incident_members(incident_id) if r["metric_id"] in p.metrics] if platform else []
    if not mine and not members:
        raise HTTPException(404, "no such incident for your role")
    # platform roles get the same text for every affected lender: show it once; a lender role has one row per alert anyway
    uniq = {(r["created_at"], r["line1"], r["line2"], r["link"]) + (() if platform else (r["tenant_id"],)): r for r in mine}
    out = dict(incident_id=incident_id, alerts=[dict(at=r["created_at"], line1=r["line1"], line2=r["line2"], link=r["link"], lender=r["tenant_id"])
                                                for r in sorted(uniq.values(), key=lambda r: r["created_at"])])
    if incident_id.startswith("CMP-"):                                        # a compliance (rule) incident: its facts live in the database
        return _compliance_detail(p, out, incident_id, members, platform)
    tl = m["RU"].prepare(verbose=False)
    inc = next((i for i in tl["incidents"] if i["incident_id"] == incident_id), None)
    clock = m["RU"].get_clock()
    if inc is not None:
        stage_found = [s for s in inc["stages"] if pd.Timestamp(s["ts"]) <= clock]
        shown = [p_ for p_, ts in ((inc["p0"], inc["t_detect"]), (inc["p1"], inc["t_refine"])) if p_ is not None and pd.Timestamp(ts) <= clock]
        f = (shown[-1] if shown else None)
        out["detected_at"], out["refined_at"] = inc["t_detect"], inc["t_refine"] if pd.Timestamp(inc["t_refine"]) <= clock else None
        out["metrics"] = sorted({a["metric"] for a in inc["members"] if pd.Timestamp(a["detected_at"]) <= clock and a["metric"] in p.metrics})
        if f is not None and f.metric not in p.metrics:
            f = None                                                         # the finding is about a metric this role cannot see
        if f is not None and platform:
            fj = f.to_json()
            out["finding"] = {**fj, "headline": f.headline}
            out["stages"] = [dict(stage=s["stage"], at=s["ts"], segment=s["finding"].segment, lenders=s["lenders"]) for s in stage_found]
            out["detections"] = [dict(metric=a["metric"], segment=a["segment"], direction=a["direction"], score=a["score"], window_buckets=a["window"],
                                      detected_at=a["detected_at"]) for a in inc["members"]
                                 if pd.Timestamp(a["detected_at"]) <= clock and a["metric"] in p.metrics]
            run = db.query("GLOBAL", "SELECT run_id FROM agent_runs WHERE tenant_id=:tenant_id AND entity_id=:i", {"i": incident_id}, DB)
            out["trace"] = [dict(step=r["step_no"], tool=r["tool"], input=json.loads(r["input"]), output=json.loads(r["output"]))
                            for r in db.query("GLOBAL", "SELECT * FROM agent_steps WHERE tenant_id=:tenant_id AND run_id=:r ORDER BY step_no",
                                              {"r": run[0]["run_id"]}, DB)] if run else []
        elif f is not None:                                                  # a lender role: its own alert lines and the driving segment's name only
            out["finding"] = dict(metric=f.metric, segment=f.segment, parent=f.parent, direction=f.direction)
    return out


# ---------------------------------------------------------------------------------------------- evaluation
@router.get("/evaluation")
def evaluation(x_user_id: str | None = Header(None)):
    p = who(x_user_id)
    if p.scope_type != "all_lenders":
        raise HTTPException(403, "the scorecard is for platform roles")
    f = DATA / "lc04_eval_report.json"
    ledger = DATA / "lc04_hidden_eval_ledger.json"
    return dict(available=f.exists(), report=json.loads(f.read_text()) if f.exists() else None,
                ledger=json.loads(ledger.read_text()) if ledger.exists() else [])
