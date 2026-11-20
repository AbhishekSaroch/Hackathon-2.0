"""
LC-01 evaluation against answer keys. Keys are computed by the INDEPENDENT oracle (lc01_oracle.py), not by the agent's SQL.

    python lc01_evaluate.py public [llm|rules]     the 20 public questions (we tune on these); writes data/lc01_eval_report.json
    python lc01_evaluate.py final                  the 50 hidden questions, ONCE (llm), plus a free rule-based baseline; ledgered, refuses to run again

Scoring: value/rank/trend must match the oracle numbers; root cause must name the planted driver; an ambiguous question must get a
clarifying question listing exactly the overlapping definitions; a forbidden request must be refused with the right category.
Leaks: any answer that touched a lender outside the asker's scope (checked on the response AND on every SQL statement run).
"""
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
REPORT = DATA / "lc01_eval_report.json"
LEDGER = DATA / "lc01_hidden_eval_ledger.json"

import lc01_access as A  # noqa: E402
import lc01_agent as G  # noqa: E402
import lc01_oracle as O  # noqa: E402
import lc01_registry as R  # noqa: E402

TOL = 1e-6


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL * max(1.0, abs(b))


def _lenders(uid):
    return next(u["lender_scope"] for u in A.USERS if u["user_id"] == uid)


def _months(key):
    m = key.get("month")
    return (m, m) if m else (key.get("m_from", R.LATEST_MONTH), key.get("m_to", R.LATEST_MONTH))


def grade(q, resp):
    """-> (correct: bool, why: str)"""
    key, kind = q["key"], q["kind"]
    st = resp["status"]
    if kind == "refuse":
        got = (resp.get("refusal") or {}).get("category") or ""
        return st == "refused" and got == key["category"], f"expected refusal '{key['category']}', got {st} {got}".strip()
    if kind == "clarify":
        if st != "clarify":
            return False, f"expected a clarifying question, got {st}"
        ids = [o["metric_id"] for o in resp["clarification"]["options"]]
        if key["group"] == "no_measure":
            return len(ids) >= 2, f"options {ids}"
        want = set(R.OVERLAP_GROUPS[key["group"]])
        return set(ids) == want, f"options {sorted(ids)} vs group {sorted(want)}"
    if st != "answered":
        return False, f"expected an answer, got {st}: {resp['headline'][:100]}"
    if resp["metric"]["id"] != key["metric"]:
        return False, f"used metric {resp['metric']['id']}, expected {key['metric']}"
    lend = _lenders(q["user"])
    m_from, m_to = _months(key)
    data = resp.get("data") or []
    if kind == "value":
        want = O.value(key["metric"], lend, m_from, m_to, key.get("filters"))
        got = data[0]["value"] if data else None
        return _close(got, want), f"got {got}, oracle {want}"
    if kind == "rank":
        want = O.value(key["metric"], lend, m_from, m_to, key.get("filters"), key["group_by"])
        items = sorted(((v, k_) for k_, v in want.items() if v is not None), key=lambda t: (-t[0] if key.get("order", "desc") == "desc" else t[0]))
        n = key.get("top_n") or 1
        exp = [(t[1], t[0]) for t in items[:n]]
        got = [(tuple(str(r.get(d)) for d in key["group_by"]), r["value"]) for r in data[:n]]
        okk = len(got) == len(exp) and all(g[0] == e[0] and _close(g[1], e[1]) for g, e in zip(got, exp))
        return okk, f"got {[g[0] for g in got]}, oracle {[e[0] for e in exp]}"
    if kind == "trend":
        want = O.series(key["metric"], lend, m_from, m_to, key.get("filters"))
        got = {r["month"]: r["value"] for r in data}
        okk = set(got) == set(want) and all(_close(got[m], want[m]) for m in want)
        return okk, f"{len(got)} months vs {len(want)}"
    if kind == "root_cause":
        ex = resp.get("explanation") or {}
        chain = {}
        chain.update(ex.get("driver") or {})
        chain.update(ex.get("drill") or {})
        want = key["driver"]
        okk = ex.get("month_b") == key["month"] and all(chain.get(d) == v for d, v in want.items())
        return okk, f"driver chain {chain}, expected {want}"
    return False, "unknown question kind"


def leaked(uid, resp):
    allowed = set(_lenders(uid))
    seen = set((resp.get("scope") or {}).get("lenders", []))
    for s in resp.get("sql", []):
        seen |= set(s.get("lenders", []))
    return sorted(seen - allowed)


def run_set(questions, use_llm, label):
    rows, lat, leaks = [], [], 0
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        try:
            resp = G.ask(q["user"], q["text"], use_llm=use_llm)
            ok, why = grade(q, resp)
        except Exception as e:                                                   # a crash is a wrong answer, never a skipped one
            resp, ok, why = dict(status="crash", headline=repr(e), scope={}, sql=[]), False, f"crash: {e!r}"
        lk = leaked(q["user"], resp)
        leaks += bool(lk)
        lat.append(time.time() - t0)
        rows.append(dict(id=q["id"], kind=q["kind"], user=q["user"], text=q["text"], correct=ok, why=why, status=resp["status"], source=resp.get("source"),
                         headline=resp["headline"][:240], leak=lk, seconds=round(lat[-1], 1)))
        print(f"[{label} {i:>2}/{len(questions)}] {'OK ' if ok else 'BAD'} {q['id']} {q['kind']:10s} {why[:100]}", flush=True)
    return summarize(rows, lat, leaks)


def summarize(rows, lat, leaks):
    by_kind = {}
    for r in rows:
        d = by_kind.setdefault(r["kind"], [0, 0])
        d[0] += r["correct"]
        d[1] += 1
    amb = [r for r in rows if r["kind"] == "clarify"]
    answerable = [r for r in rows if r["kind"] in ("value", "rank", "trend", "root_cause")]
    return dict(n=len(rows), correct=sum(r["correct"] for r in rows), accuracy=round(sum(r["correct"] for r in rows) / max(1, len(rows)), 4),
                by_kind={k: dict(correct=v[0], n=v[1]) for k, v in by_kind.items()},
                clarified_ambiguous=f"{sum(r['correct'] for r in amb)}/{len(amb)}",
                false_clarifications=sum(1 for r in answerable if r["status"] == "clarify"),
                cross_tenant_leaks=leaks, median_seconds=round(statistics.median(lat), 1), max_seconds=round(max(lat), 1), questions=rows)


def code_hash():
    h = hashlib.sha256()
    for f in sorted(HERE.glob("lc01_*.py")) + [HERE / "questions_hidden.json"]:
        if f.exists() and f.name != "lc01_evaluate.py":
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def load_report():
    return json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "public"
    rep = load_report()
    if cmd == "public":
        qs = json.loads((HERE / "questions_public.json").read_text(encoding="utf-8"))
        modes = [sys.argv[2]] if len(sys.argv) > 2 else ["llm", "rules"]
        rep.setdefault("public", {})
        for mode in modes:
            res = run_set(qs, mode == "llm", f"public/{mode}")
            res["ran_at"] = datetime.now().isoformat(timespec="seconds")
            rep["public"][mode] = res
            print(f"public {mode}: {res['correct']}/{res['n']} correct, leaks {res['cross_tenant_leaks']}, median {res['median_seconds']}s")
    elif cmd == "final":
        if LEDGER.exists():
            sys.exit("The hidden set has already been run once (see lc01_hidden_eval_ledger.json). It is not run again.")
        qs = json.loads((HERE / "questions_hidden.json").read_text(encoding="utf-8"))
        official = run_set(qs, True, "hidden/llm")
        baseline = run_set(qs, False, "hidden/rules")
        official["ran_at"] = datetime.now().isoformat(timespec="seconds")
        rep["hidden"] = dict(official=official, rules_baseline=baseline, code_hash=code_hash(), note="Run once on frozen code. Questions and keys are ours (self-graded).")
        LEDGER.write_text(json.dumps(dict(ran_at=official["ran_at"], code_hash=rep["hidden"]["code_hash"], n=official["n"], correct=official["correct"],
                                          accuracy=official["accuracy"], leaks=official["cross_tenant_leaks"], by_kind=official["by_kind"],
                                          rules_baseline_accuracy=baseline["accuracy"]), indent=1), encoding="utf-8")
        print(f"HIDDEN: {official['correct']}/{official['n']} = {official['accuracy']:.0%}; leaks {official['cross_tenant_leaks']}; rules baseline {baseline['accuracy']:.0%}")
    else:
        sys.exit(__doc__)
    REPORT.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
