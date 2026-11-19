"""
Evaluate BI-02 on every gold-labelled account (the agent never sees gold_labels).

  python evaluate.py [--workers 4] [--limit N] [--tenant abc_bank]

- Resumable: accounts that already have an assessment are skipped, so you can stop and re-run.
- Two score tables: ALL accounts, and "unseen" = without the 25 accounts used while tuning prompts.
- Writes backend/data/eval_report.json (numbers + every wrong prediction) for the demo / slides.
"""
import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
sys.path[:0] = [str(HERE), str(BACKEND), str(BACKEND / "db"), str(BACKEND / "llm"), str(BACKEND / "tools")]
import act  # noqa: E402
import db  # noqa: E402
import groq_client  # noqa: E402
from routers import bi02  # noqa: E402

REPORT = BACKEND / "data" / "eval_report.json"
LABELS = ["forgot", "cant_pay", "wont_pay", "disputed", "insufficient"]


def tuning_ids(tenant: str) -> set[str]:
    """The 25 accounts (5 per label, seed 7) I looked at while tuning prompts, so they are not 'unseen'."""
    rnd, out = random.Random(7), set()
    for lab in LABELS:
        a = [r["account_id"] for r in db.query(
            tenant, "SELECT account_id FROM gold_labels WHERE tenant_id=:tenant_id AND expert_label=:l", {"l": lab})]
        out |= set(rnd.sample(a, 5))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="abc_bank")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    T = args.tenant

    t0 = time.time()
    failed: dict[str, str] = {}
    for pass_no in (1, 2):  # second pass retries accounts that failed on rate limits
        todo = [r["account_id"] for r in db.query(
            T, "SELECT g.account_id FROM gold_labels g WHERE g.tenant_id=:tenant_id AND NOT EXISTS "
               "(SELECT 1 FROM assessments s WHERE s.tenant_id=g.tenant_id AND s.account_id=g.account_id) "
               "ORDER BY g.account_id")]
        if args.limit:
            todo = todo[:args.limit]
        if not todo:
            break
        print(f"pass {pass_no}: {len(todo)} accounts to assess", flush=True)
        lock, state = threading.Lock(), {"done": 0, "failed": 0}

        def one(aid):
            try:
                act.run_and_save(T, aid)
                ok = True
                failed.pop(aid, None)
            except Exception as e:
                ok = False
                failed[aid] = repr(e)[:160]
            with lock:
                state["done" if ok else "failed"] += 1
                n = state["done"] + state["failed"]
                if n % 10 == 0 or n == len(todo):
                    el = time.time() - t0
                    print(f"  [{n}/{len(todo)}] ok={state['done']} failed={state['failed']} "
                          f"elapsed={el/60:.1f}min eta={(el / n) * (len(todo) - n) / 60:.1f}min", flush=True)

        with ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(one, todo))

    tuned = tuning_ids(T)
    everyone, unseen = bi02.compute_metrics(T), bi02.compute_metrics(T, exclude_ids=tuned)
    misses = db.query(
        T, "SELECT g.account_id, g.expert_label AS gold, s.label AS predicted, s.confidence, g.rationale AS gold_rationale, "
           "s.reasoning_summary FROM gold_labels g JOIN assessments s ON s.assessment_id = (SELECT MAX(assessment_id) "
           "FROM assessments WHERE tenant_id=g.tenant_id AND account_id=g.account_id) "
           "WHERE g.tenant_id=:tenant_id AND g.expert_label != s.label ORDER BY g.account_id")
    report = {"all": everyone, "unseen": unseen, "n_tuning_excluded": len(tuned), "wrong_predictions": misses,
              "failed_accounts": failed, "elapsed_min": round((time.time() - t0) / 60, 1), "llm_stats": groq_client.STATS}
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    for name, m in (("ALL", everyone), ("UNSEEN (tuning accounts excluded)", unseen)):
        print(f"\n=== {name}: n={m['n_assessed']} | macro-F1={m['macro_f1']} | accuracy={m['accuracy']}")
        print("  per label:", {p["label"]: p["f1"] for p in m["per_label"]}, "| abstention:", m["abstention"])
        print("  calibration:", [(b["range"], b["n"], b["accuracy"]) for b in m["calibration"]])
    print(f"\nwrong predictions: {len(misses)} | failed accounts: {len(failed)} | report: {REPORT}")


if __name__ == "__main__":
    main()
