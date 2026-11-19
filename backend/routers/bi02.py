"""
BI-02 API. Everything is tenant-scoped through db.py.

Tenant comes from the X-Tenant-Id header (default abc_bank). That is demo-only: in the real
product it is read from the logged-in user's token, never from the client.

  GET  /bi02/accounts            list + filters + latest label/confidence/next action
  GET  /bi02/accounts/{id}       full detail: facts, calls, evidence, agent steps, recommendation
  POST /bi02/assess/{id}         run the agent on one account (blocking, ~10-60 s)
  POST /bi02/batch               run many accounts in the background -> job id
  GET  /bi02/batch/{job_id}      batch progress
  GET  /bi02/recommendations     "To approve" queue
  POST /bi02/recommendations/{id}/status   approve / skip (nothing is sent to customers here)
  GET  /bi02/metrics             macro-F1, confusion matrix, abstention, calibration
"""
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi02"), str(BACKEND / "tools"), str(BACKEND / "llm"), str(BACKEND / "db")]
import account_tools as tools  # noqa: E402
import act  # noqa: E402
import db  # noqa: E402
import groq_client  # noqa: E402

router = APIRouter(prefix="/bi02", tags=["bi02"])
LABELS = ["forgot", "cant_pay", "wont_pay", "disputed", "insufficient"]


def get_tenant(x_tenant_id: str = Header("abc_bank")) -> str:
    return x_tenant_id


# ------------------------------------------------------------------ accounts
_LATEST = ("LEFT JOIN assessments s ON s.assessment_id = (SELECT MAX(assessment_id) FROM assessments "
           "WHERE tenant_id = a.tenant_id AND account_id = a.account_id) "
           "LEFT JOIN recommendations r ON r.id = (SELECT MAX(id) FROM recommendations "
           "WHERE tenant_id = a.tenant_id AND account_id = a.account_id AND source = 'bi02' AND status != 'superseded')")
SORTS = {"dpd_desc": "a.dpd DESC", "outstanding_desc": "a.outstanding DESC", "account": "a.account_id",
         "confidence_asc": "s.confidence ASC, a.account_id", "confidence_desc": "s.confidence DESC, a.account_id"}


@router.get("/accounts")
def list_accounts(label: str | None = Query(None, description="a label, or 'unassessed'"),
                  q: str | None = Query(None, description="account no or customer name"),
                  sort: str = "account", page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
                  tenant: str = Depends(get_tenant)):
    if sort not in SORTS:
        raise HTTPException(400, f"sort must be one of {list(SORTS)}")
    where, params = ["a.tenant_id = :tenant_id"], {}
    if label == "unassessed":
        where.append("s.label IS NULL")
    elif label:
        if label not in LABELS:
            raise HTTPException(400, f"label must be one of {LABELS} or 'unassessed'")
        where.append("s.label = :label")
        params["label"] = label
    if q:
        where.append("(a.account_id LIKE :q OR LOWER(a.customer_name) LIKE :q)")
        params["q"] = f"%{q.lower()}%"
    w = " AND ".join(where)
    total = db.query_one(tenant, f"SELECT COUNT(*) AS n FROM accounts a {_LATEST} WHERE {w}", params)["n"]
    items = db.query(
        tenant,
        "SELECT a.account_id, a.customer_name, a.product, a.branch, a.dpd, a.bucket, a.outstanding, a.emi_amount, "
        "s.label, s.confidence, s.created_at AS assessed_at, r.id AS recommendation_id, r.channel, r.status AS rec_status "
        f"FROM accounts a {_LATEST} WHERE {w} ORDER BY {SORTS[sort]} LIMIT :limit OFFSET :offset",
        {**params, "limit": page_size, "offset": (page - 1) * page_size})
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _latest_assessment(tenant: str, account_id: str):
    return db.query_one(tenant, "SELECT * FROM assessments WHERE tenant_id=:tenant_id AND account_id=:a "
                                "ORDER BY assessment_id DESC LIMIT 1", {"a": account_id})


@router.get("/accounts/{account_id}")
def account_detail(account_id: str, tenant: str = Depends(get_tenant)):
    account = db.query_one(tenant, "SELECT * FROM accounts WHERE tenant_id=:tenant_id AND account_id=:a", {"a": account_id})
    if not account:
        raise HTTPException(404, "account not found")
    assessment = _latest_assessment(tenant, account_id)
    evidence = steps = []
    recommendation = None
    pull = 1
    if assessment:
        evidence = db.query(tenant, "SELECT source, source_ref, text, supports FROM evidence "
                                    "WHERE tenant_id=:tenant_id AND assessment_id=:i ORDER BY id", {"i": assessment["assessment_id"]})
        steps = db.query(tenant, "SELECT step_no, tool, input, output, ts FROM agent_steps "
                                 "WHERE tenant_id=:tenant_id AND run_id=:r ORDER BY step_no", {"r": assessment["run_id"]})
        for s in steps:
            s["input"], s["output"] = json.loads(s["input"]), json.loads(s["output"])
            if s["tool"] == "request_fresh_statement" and s["output"].get("new_data"):
                pull = s["output"]["pull_no"]
        recommendation = db.query_one(tenant, "SELECT id, channel, action, reason, status FROM recommendations "
                                              "WHERE tenant_id=:tenant_id AND source_ref=:i AND source='bi02' ORDER BY id DESC LIMIT 1",
                                      {"i": assessment["assessment_id"]})
    pack = tools.build_evidence_pack(tenant, account_id, pull_no=pull)
    return {"account": account, "assessment": assessment, "evidence": evidence, "agent_steps": steps,
            "recommendation": recommendation, "statement_pull_shown": pull,
            "facts": {"repayment": pack["repayment"], "promises": pack["promises"], "statement": pack["statement"]},
            "repayments": tools.get_repayments(tenant, account_id), "calls": pack["calls"]}


@router.post("/assess/{account_id}")
def assess(account_id: str, tenant: str = Depends(get_tenant)):
    try:
        return act.run_and_save(tenant, account_id)
    except ValueError:
        raise HTTPException(404, "account not found")
    except RuntimeError as e:  # LLM unavailable / rate limited too long
        raise HTTPException(503, str(e))


# ------------------------------------------------------------------ batch
JOBS: dict[str, dict] = {}


class BatchBody(BaseModel):
    account_ids: list[str] | None = None
    only_unassessed: bool = True
    limit: int = 20      # clamped to 1..500 in the endpoint
    workers: int = 3     # clamped to 1..6


def _run_batch(tenant: str, job: dict, workers: int) -> None:
    lock = threading.Lock()

    def one(aid):
        try:
            act.run_and_save(tenant, aid)
            with lock:
                job["done"] += 1
        except Exception as e:  # keep going, report at the end
            with lock:
                job["failed"] += 1
                job["errors"].append({"account_id": aid, "error": str(e)[:200]})

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, job["account_ids"]))
    job["status"], job["finished_at"] = "done", time.time()


@router.post("/batch")
def start_batch(body: BatchBody, tenant: str = Depends(get_tenant)):
    limit, workers = max(1, min(body.limit, 500)), max(1, min(body.workers, 6))
    if body.account_ids:
        ids = body.account_ids
    else:
        extra = "AND s.label IS NULL" if body.only_unassessed else ""
        ids = [r["account_id"] for r in db.query(
            tenant, f"SELECT a.account_id FROM accounts a {_LATEST} WHERE a.tenant_id = :tenant_id {extra} "
                    "ORDER BY a.account_id LIMIT :n", {"n": limit})]
    job_id = "job-" + uuid.uuid4().hex[:8]
    JOBS[job_id] = {"job_id": job_id, "status": "running", "total": len(ids), "done": 0, "failed": 0,
                    "errors": [], "account_ids": ids, "started_at": time.time(), "finished_at": None}
    threading.Thread(target=_run_batch, args=(tenant, JOBS[job_id], workers), daemon=True).start()
    return {"job_id": job_id, "total": len(ids)}


@router.get("/batch/{job_id}")
def batch_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    end = job["finished_at"] or time.time()
    return {**{k: v for k, v in job.items() if k != "account_ids"}, "errors": job["errors"][-10:],
            "elapsed_s": round(end - job["started_at"], 1)}


# ------------------------------------------------------------------ recommendations ("To approve")
@router.get("/recommendations")
def list_recommendations(status: str = "pending", tenant: str = Depends(get_tenant)):
    return db.query(
        tenant,
        "SELECT r.id, r.account_id, a.customer_name, r.channel, r.action, r.reason, r.status, r.created_at, "
        "s.label, s.confidence, s.reasoning_summary "
        "FROM recommendations r JOIN accounts a ON a.account_id = r.account_id AND a.tenant_id = r.tenant_id "
        "LEFT JOIN assessments s ON s.assessment_id = r.source_ref "
        "WHERE r.tenant_id = :tenant_id AND r.source = 'bi02' AND r.status = :st ORDER BY r.id DESC LIMIT 200",
        {"st": status})


class StatusBody(BaseModel):
    status: str


@router.post("/recommendations/{rec_id}/status")
def set_recommendation_status(rec_id: int, body: StatusBody, tenant: str = Depends(get_tenant)):
    if body.status not in ("approved", "skipped"):
        raise HTTPException(400, "status must be approved or skipped")
    n = db.execute(tenant, "UPDATE recommendations SET status=:st WHERE tenant_id=:tenant_id AND id=:i AND status='pending'",
                   {"st": body.status, "i": rec_id})
    if not n:
        raise HTTPException(404, "pending recommendation not found")
    return {"id": rec_id, "status": body.status}  # demo: approval is recorded, nothing is sent


# ------------------------------------------------------------------ metrics
def _prf(rows: list[dict], label: str) -> dict:
    tp = sum(1 for r in rows if r["expert_label"] == label and r["label"] == label)
    fp = sum(1 for r in rows if r["expert_label"] != label and r["label"] == label)
    fn = sum(1 for r in rows if r["expert_label"] == label and r["label"] != label)
    p, rc = (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)
    return {"label": label, "support": tp + fn, "precision": round(p, 3), "recall": round(rc, 3),
            "f1": round(2 * p * rc / (p + rc), 3) if p + rc else 0.0}


@router.get("/metrics")
def metrics(tenant: str = Depends(get_tenant)):
    return compute_metrics(tenant)


def compute_metrics(tenant: str, exclude_ids=frozenset()) -> dict:
    rows = db.query(
        tenant,
        "SELECT g.account_id, g.expert_label, g.is_ambiguous, s.label, s.confidence FROM gold_labels g "
        "JOIN assessments s ON s.assessment_id = (SELECT MAX(assessment_id) FROM assessments "
        "WHERE tenant_id = g.tenant_id AND account_id = g.account_id) WHERE g.tenant_id = :tenant_id")
    rows = [r for r in rows if r["account_id"] not in exclude_ids]
    n_gold = db.query_one(tenant, "SELECT COUNT(*) AS n FROM gold_labels WHERE tenant_id = :tenant_id")["n"]
    base = {"n_gold": n_gold, "n_assessed": len(rows), "llm_stats": groq_client.STATS}
    if not rows:
        return {**base, "macro_f1": None, "accuracy": None, "per_label": [], "confusion": {}, "abstention": None, "calibration": []}

    per = [_prf(rows, lb) for lb in LABELS if any(r["expert_label"] == lb for r in rows)]
    confusion = {g: {p: sum(1 for r in rows if r["expert_label"] == g and r["label"] == p) for p in LABELS} for g in LABELS}
    amb = [r for r in rows if r["is_ambiguous"]]
    clear = [r for r in rows if not r["is_ambiguous"]]
    bins = []
    for lo, hi in [(0, 0.6), (0.6, 0.8), (0.8, 0.9), (0.9, 1.01)]:
        b = [r for r in rows if lo <= (r["confidence"] or 0) < hi]
        if b:
            bins.append({"range": f"{lo:.1f}-{min(hi, 1):.1f}", "n": len(b),
                         "accuracy": round(sum(r["expert_label"] == r["label"] for r in b) / len(b), 3),
                         "avg_confidence": round(sum(r["confidence"] for r in b) / len(b), 3)})
    return {**base, "macro_f1": round(sum(p["f1"] for p in per) / len(per), 3),
            "accuracy": round(sum(r["expert_label"] == r["label"] for r in rows) / len(rows), 3),
            "per_label": per, "confusion": confusion, "calibration": bins,
            "abstention": {
                "ambiguous_cases": len(amb),
                "correctly_abstained": sum(r["label"] == "insufficient" for r in amb),
                "clear_cases": len(clear),
                "wrongly_abstained": sum(r["label"] == "insufficient" for r in clear)}}
