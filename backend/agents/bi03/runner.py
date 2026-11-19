"""
BI-03 daily runner: sense -> update each account's distress state -> flag -> record. One call = one morning.

  python runner.py replay --from 61 --to 150 [--reset] [--no-cache]   replay the stream day by day into bi03.db
  python runner.py day --day 120                                       run a single day (state is read from the database)
  python runner.py speed --scale 10                                    time one daily run over scale x 6,000 accounts (in memory, real DB untouched)

What a day does (each step is written to agent_steps, so the UI can show it):
  1 ingest    count the events that became visible today (available_from <= today). Nothing newer exists for the run.
  2 features  point-in-time features for every account (agents/bi03/features.py)
  3 status    accounts with an EMI unpaid past the grace period are no longer "current" and leave the watch (open flags close)
  4 score     risk = probability of missing an EMI within 60 days (the saved model)
  5 flag      score >= tau and no flag in the last 30 days (cooldown) -> new flag
  6 explain   which signal drives each watched account (salary delay, bounces, card utilisation ...), so step 7 can pick the nudge
  7 write     risk_scores, flags, agent_runs / agent_steps, and the replay pointer in bp_meta

The runner never reads outcome labels. Whether an account is current comes from its own repayment events.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import db  # noqa: E402
import confirm as C  # noqa: E402
import features as F  # noqa: E402
import model as M  # noqa: E402

DB = "bi03"
MODEL_NAME = "bi03-v1"
THRESHOLD_VERSION = "bi03-v1-tau"
COOLDOWN_DAYS = 30        # an account is not flagged again for 30 days (no nudge fatigue; the compliance gate has its own cap)
GRACE_DAYS = 4            # an EMI paid up to 4 days after its due date counts as paid (grace period)
WATCH_FRACTION = 0.5      # accounts above half the alert threshold are "on watch" and get an explanation stored
REPORT = HERE.parents[1] / "data" / "bi03_backtest_report.json"


# ====================================================================================== pure computation (no database)
def not_current_accounts(ev_visible: pd.DataFrame, as_of: pd.Timestamp, grace: int = GRACE_DAYS) -> pd.Index:
    """Accounts with an EMI that is due, past the grace period, and still unpaid. Uses only rows visible on `as_of`."""
    rep = ev_visible[ev_visible["source"] == "repayment"]
    due = rep[(rep["event_type"] == "emi_due") & (rep["event_date"] <= as_of - pd.Timedelta(days=grace + 1))].groupby("account_id").size()
    paid = rep[rep["event_type"] == "emi_paid"].groupby("account_id").size().reindex(due.index).fillna(0)
    return due.index[(due - paid) > 0]


def compute_day(eng: F.FeatureEngine, bundle: dict, as_of, last_flag: pd.Series, features: pd.DataFrame | None = None, cooldown: int = COOLDOWN_DAYS,
                confirm: bool = False) -> dict:
    """Everything a daily run decides, as plain DataFrames. `last_flag` = account_id -> date of its latest flag.
    confirm=True adds the borderline step: accounts close to the threshold get fresh bureau / AA data and are re-scored (confirm.py)."""
    t = pd.Timestamp(as_of)
    steps, tau, model = [], bundle["tau"], bundle["model"]

    def step(tool: str, t0: float, **out) -> None:
        steps.append({"tool": tool, "input": {"as_of": t.date().isoformat()}, "output": {**out, "seconds": round(time.perf_counter() - t0, 3)}})

    t0 = time.perf_counter()
    ev = eng.events[eng.events["available_from"] <= t]                       # point-in-time gate for the status check
    step("ingest", t0, visible_events=int(len(ev)), new_today=int((eng.events["available_from"] == t).sum()))

    t0 = time.perf_counter()
    X = features if features is not None else F.compute_features(eng.events, eng.snaps, eng.accounts, t)
    step("features", t0, accounts=int(len(X)), n_features=int(X.shape[1]))

    t0 = time.perf_counter()
    missed = not_current_accounts(ev, t)
    keep = ~X.index.isin(missed)
    Xc = X[keep]
    step("status", t0, current=int(keep.sum()), not_current=int((~keep).sum()))

    t0 = time.perf_counter()
    scores = M.score(model, Xc)
    step("score", t0, scored=int(len(scores)), at_or_above_tau=int((scores >= tau).sum()), tau=round(tau, 4))

    decisions = pd.DataFrame()
    if confirm:                                                              # the agent decides to fetch fresher data for borderline accounts
        t0 = time.perf_counter()
        cres = C.confirm_borderline(eng, bundle, t, Xc, scores)
        scores, Xc, decisions = cres["scores"], cres["X"], cres["decisions"]
        secs = round(time.perf_counter() - t0, 3)
        steps.append({"tool": "confirm_select", "input": {"band": [round(C.BAND_LOW * tau, 4), round(C.BAND_HIGH * tau, 4)]}, "output": {"borderline_accounts": cres["band"]}})
        for call in cres["calls"]:
            steps.append({"tool": call["tool"], "input": {"accounts": call["accounts"]}, "output": {"new_rows_returned": call["new_rows_returned"]}})
        counts = decisions["status"].value_counts().to_dict() if len(decisions) else {}
        steps.append({"tool": "confirm_decide", "input": {"rescored": int(len(decisions))},
                      "output": {**counts, "withdrawn_accounts": [str(a) for a in decisions.index[decisions["status"] == "withdrawn"]][:50], "seconds": secs}})

    t0 = time.perf_counter()
    since = (t - last_flag.reindex(scores.index)).dt.days
    allowed = since.isna() | (since >= cooldown)
    is_flag = (scores >= tau) & allowed
    new_flags = scores[is_flag]
    step("flag", t0, new_flags=int(len(new_flags)), suppressed_by_cooldown=int(((scores >= tau) & ~allowed).sum()))

    t0 = time.perf_counter()
    watch = scores[scores >= WATCH_FRACTION * tau].index
    ex = M.explain(bundle, Xc.loc[watch]) if len(watch) else pd.DataFrame(columns=["risk", "dominant_signal"])
    step("explain", t0, on_watch=int(len(watch)))

    out = pd.DataFrame({"score": scores})
    out["dominant_signal"] = ex["dominant_signal"].reindex(out.index) if len(watch) else None
    drops = ex.filter(like="drop_") if len(watch) else pd.DataFrame()
    out["signals_json"] = drops.round(4).apply(lambda r: json.dumps(r.to_dict()), axis=1).reindex(out.index) if len(watch) else None
    if len(decisions):                                                       # keep the audit trail on the account's score row
        def with_refresh(a):
            cur = out.at[a, "signals_json"]
            d = json.loads(cur) if isinstance(cur, str) else {}                  # empty (NaN) for accounts not on the watchlist
            r = decisions.loc[a]
            d["refresh"] = {"score_before": round(float(r["score_before"]), 4), "tools": r["tools"], "status": r["status"]}
            return json.dumps(d)
        out["signals_json"] = out["signals_json"].astype(object)
        for a in decisions.index:
            out.at[a, "signals_json"] = with_refresh(a)
    flags = out.loc[new_flags.index].copy()
    if confirm:
        flags["confirmed_by_refresh"] = decisions["status"].isin(["confirmed", "promoted"]).reindex(flags.index).fillna(False).astype(int) if len(decisions) else 0
    return {"as_of": t, "scores": out, "flags": flags, "not_current": missed, "steps": steps, "current": int(keep.sum()), "confirm": decisions}


# ====================================================================================== database side
def stream_dates(tenant: str) -> pd.Timestamp:
    return M.stream_start(tenant)


def load_last_flags(tenant: str) -> pd.Series:
    df = db.query_df(tenant, "SELECT account_id, MAX(flagged_on) AS last_flag FROM flags WHERE tenant_id = :tenant_id GROUP BY account_id", database=DB)
    return pd.Series(pd.to_datetime(df["last_flag"]).to_numpy(), index=df["account_id"].values, dtype="datetime64[ns]")


def ensure_threshold(tenant: str, bundle: dict) -> None:
    if db.query_one(tenant, "SELECT 1 AS x FROM thresholds WHERE tenant_id = :tenant_id AND threshold_version = :v", {"v": THRESHOLD_VERSION}, database=DB):
        return
    prec = lift = lead = None
    try:                                       # performance on PUBLIC data (the rehearsal); hidden results are not copied here
        rep = json.loads(REPORT.read_text(encoding="utf-8"))["cv"]
        c = next(c for c in rep["configs"] if c["kind"] == bundle["kind"] and c["params"] == bundle["params"])["replay"]
        prec, lift, lead = c["precision"], c["lift"], c["median_lead_days"]
    except Exception:
        pass
    db.insert(tenant, "thresholds", {"threshold_version": THRESHOLD_VERSION, "value": float(bundle["tau"]), "top_pct": 0.05, "precision_top": prec,
                                     "lift": lift, "median_lead_days": lead, "is_active": 1,
                                     "notes": f"{bundle['version']} ({bundle['kind']}); set from public scores only so ~5% of accounts cross it; "
                                              "performance shown is the public-data rehearsal"}, database=DB)


def active_threshold(tenant: str, bundle: dict) -> tuple[str, float]:
    """The alert threshold the daily run uses: the ACTIVE row of the thresholds table (tuning can add and activate new ones),
    falling back to the one saved with the model."""
    row = db.query_one(tenant, "SELECT threshold_version, value FROM thresholds WHERE tenant_id = :tenant_id AND is_active = 1", database=DB)
    return (row["threshold_version"], float(row["value"])) if row else (THRESHOLD_VERSION, float(bundle["tau"]))


def _records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict("records")


def persist_day(tenant: str, res: dict, bundle: dict, store_scores: str = "all", threshold_version: str = THRESHOLD_VERSION) -> dict:
    """store_scores: 'all' = a score row for every current account every day (best for the UI timeline, fine for thousands);
    'watch' = only accounts on the watchlist (score >= half the alert threshold) or flagged today (keeps the table small at 60,000+)."""
    t0 = time.perf_counter()
    d = res["as_of"].date().isoformat()
    run_id = f"bi03-{d}-{uuid.uuid4().hex[:6]}"
    db.insert(tenant, "agent_runs", {"run_id": run_id, "agent_name": "bi03_daily", "entity_id": d, "status": "running"}, database=DB)

    keep = res["scores"]
    if store_scores == "watch":
        keep = keep[(keep["score"] >= WATCH_FRACTION * bundle["tau"]) | keep.index.isin(res["flags"].index)]
    sc = keep.reset_index().rename(columns={"index": "account_id"})
    sc["account_id"] = sc["account_id"].astype(str)
    sc["as_of_date"], sc["model_version"] = d, bundle["version"]
    db.insert_many(tenant, "risk_scores", _records(sc[["account_id", "as_of_date", "score", "dominant_signal", "signals_json", "model_version"]]), database=DB)

    fl = res["flags"].reset_index().rename(columns={"index": "account_id"})
    if len(fl):
        fl["account_id"] = fl["account_id"].astype(str)
        fl["flagged_on"], fl["threshold_version"], fl["status"] = d, threshold_version, "open"
        cols = ["account_id", "flagged_on", "score", "dominant_signal", "signals_json", "threshold_version", "status"] + (["confirmed_by_refresh"] if "confirmed_by_refresh" in fl else [])
        db.insert_many(tenant, "flags", _records(fl[cols]), database=DB)

    closed = 0
    gone = [str(a) for a in res["not_current"]]
    for i in range(0, len(gone), 500):                         # accounts that left the "current" group: close their open flags
        chunk = gone[i:i + 500]
        names = {f"a{j}": a for j, a in enumerate(chunk)}
        closed += db.execute(tenant, "UPDATE flags SET status = 'closed' WHERE tenant_id = :tenant_id AND status IN ('open', 'nudged') "
                                     f"AND account_id IN ({', '.join(':' + k for k in names)})", names, database=DB)

    steps = res["steps"] + [{"tool": "write", "input": {"as_of": d}, "output": {"risk_scores": int(len(sc)), "flags": int(len(fl)), "closed_flags": closed,
                                                                               "seconds": round(time.perf_counter() - t0, 3)}}]
    db.insert_many(tenant, "agent_steps", [{"run_id": run_id, "step_no": i + 1, "tool": s["tool"], "input": json.dumps(s["input"]),
                                            "output": json.dumps(s["output"])} for i, s in enumerate(steps)], database=DB)
    db.execute(tenant, "UPDATE agent_runs SET status = 'done', finished_at = CURRENT_TIMESTAMP WHERE tenant_id = :tenant_id AND run_id = :r", {"r": run_id}, database=DB)
    db.execute(tenant, "UPDATE bp_meta SET value = :v WHERE tenant_id = :tenant_id AND key = 'replay_date'", {"v": d}, database=DB)
    return {"run_id": run_id, "risk_scores": len(sc), "flags": len(fl), "closed_flags": closed, "steps": steps}


def already_run(tenant: str, as_of: pd.Timestamp, version: str) -> bool:
    r = db.query_one(tenant, "SELECT COUNT(*) AS n FROM risk_scores WHERE tenant_id = :tenant_id AND as_of_date = :d AND model_version = :v",
                     {"d": as_of.date().isoformat(), "v": version}, database=DB)
    return r["n"] > 0


def reset_outputs(tenant: str) -> None:
    for t in ("nudges", "flag_outcomes", "flags", "risk_scores"):
        db.execute(tenant, f"DELETE FROM {t} WHERE tenant_id = :tenant_id", database=DB)
    db.execute(tenant, "DELETE FROM agent_steps WHERE tenant_id = :tenant_id AND run_id IN (SELECT run_id FROM agent_runs WHERE tenant_id = :tenant_id AND agent_name = 'bi03_daily')", database=DB)
    db.execute(tenant, "DELETE FROM agent_runs WHERE tenant_id = :tenant_id AND agent_name = 'bi03_daily'", database=DB)


def run_range(tenant: str, first_day: int, last_day: int, reset: bool = False, use_cache: bool = True, eng: F.FeatureEngine | None = None,
              quiet: bool = False, store_scores: str = "all", confirm: bool = False) -> list[dict]:
    eng = eng or F.FeatureEngine.from_db(tenant)
    bundle = M.load_bundle(MODEL_NAME)
    start = M.stream_start(tenant)
    store = M.FeatureStore(eng, tenant, start) if use_cache else None
    db.init_db(DB)
    ensure_threshold(tenant, bundle)
    if reset:
        reset_outputs(tenant)
    version, tau = active_threshold(tenant, bundle)
    bundle = {**bundle, "tau": tau}                                # every step of the run (flagging, watchlist, borderline band) uses the active threshold
    last_flag = load_last_flags(tenant)
    results = []
    for day in range(first_day, last_day + 1):
        t = M.day_to_date(start, day)
        if already_run(tenant, t, bundle["version"]):
            if not quiet:
                print(f"day {day} ({t.date()}) already has scores, skipped (use --reset to redo)")
            continue
        t0 = time.perf_counter()
        res = compute_day(eng, bundle, t, last_flag, features=store.get(day) if store else None, confirm=confirm)
        out = persist_day(tenant, res, bundle, store_scores, threshold_version=version)
        for a in res["flags"].index:
            last_flag.loc[a] = t
        results.append({"day": day, "date": str(t.date()), "current": res["current"], "flags": out["flags"], "closed": out["closed_flags"], "seconds": time.perf_counter() - t0})
        if not quiet and (day % 10 == 0 or day == last_day or day == first_day):
            r = results[-1]
            print(f"day {day:3d} {r['date']}: {r['current']:5d} current | {r['flags']:3d} new flags | {r['closed']:3d} flags closed (account missed) | {r['seconds']:.1f}s", flush=True)
    return results


# ====================================================================================== speed test (in memory, real database untouched)
def scale_frames(eng: F.FeatureEngine, k: int) -> F.FeatureEngine:
    """k copies of the 6,000-account book with compact dtypes (account ids become integers 0..k*6000-1)."""
    n = len(eng.accounts)
    code = pd.Series(range(n), index=eng.accounts["account_id"].values)
    ev = eng.events.drop(columns=["narration"]).copy()
    ev["acct"] = ev["account_id"].map(code).astype("int32")
    sn = eng.snaps.copy()
    sn["acct"] = sn["account_id"].map(code).astype("int32")
    top = int(ev["event_id"].max()) + 1
    cat = ["source", "event_type"]
    parts_e, parts_s = [], []
    for i in range(k):
        e = ev.drop(columns=["account_id"]).rename(columns={"acct": "account_id"})
        e = e.assign(account_id=e["account_id"] + i * n, event_id=(e["event_id"] + i * top).astype("int64"))
        for c in cat:
            e[c] = e[c].astype("category")
        parts_e.append(e)
        s = sn.drop(columns=["account_id"]).rename(columns={"acct": "account_id"})
        parts_s.append(s.assign(account_id=s["account_id"] + i * n))
    events = pd.concat(parts_e, ignore_index=True)
    for c in cat:
        events[c] = events[c].astype(str).astype("category")
    accounts = pd.DataFrame({"account_id": np.arange(k * n, dtype="int32"), "emi_amount": np.tile(eng.accounts["emi_amount"].to_numpy(), k)})
    return F.FeatureEngine(events, pd.concat(parts_s, ignore_index=True), accounts)


def speed_test(tenant: str, scale: int) -> None:
    import os, tempfile
    base = F.FeatureEngine.from_db(tenant)
    t0 = time.perf_counter()
    big = scale_frames(base, scale)
    n = len(big.accounts)
    print(f"scaled book built in {time.perf_counter() - t0:.0f}s: {n:,} accounts, {len(big.events):,} events, {len(big.snaps):,} bureau snapshots")
    bundle = M.load_bundle(MODEL_NAME)
    start = M.stream_start(tenant)
    as_of = M.day_to_date(start, 120)

    t0 = time.perf_counter()
    res = compute_day(big, bundle, as_of, pd.Series(dtype="datetime64[ns]"))
    compute_s = time.perf_counter() - t0
    for s in res["steps"]:
        print(f"  {s['tool']:9s} {s['output']['seconds']:7.1f}s   {({k: v for k, v in s['output'].items() if k != 'seconds'})}")

    # write the day into a throw-away (empty) database so the real one is untouched
    writes = {}
    for mode in ("all", "watch"):
        tmp = Path(tempfile.mkdtemp()) / "speed.db"
        saved = db.BI03_DB_PATH
        db.BI03_DB_PATH = tmp
        try:
            db.init_db(DB)
            db.insert_many("t_speed", "bp_accounts", [{"account_id": str(i), "customer_name": "x", "status": "current"} for i in range(n)], database=DB)
            t0 = time.perf_counter()
            out = persist_day("t_speed", res, bundle, store_scores=mode)
            writes[mode] = (time.perf_counter() - t0, out["risk_scores"])
        finally:
            db.BI03_DB_PATH = saved
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(str(tmp) + ext)
                except OSError:
                    pass
    print(f"\nONE DAILY RUN over {n:,} accounts (flags raised: {len(res['flags'])}):")
    for mode, (w, rows) in writes.items():
        total = compute_s + w
        print(f"  store scores '{mode:5s}': compute {compute_s:.0f}s + database write {w:.0f}s ({rows:,} score rows) = {total:.0f}s = {total / 3600:.1%} of the 1-hour budget")
    print("Not included: loading the events from the database (done once, then only the new day's events are added), and the write slowing down as the"
          " score table grows (on the real 6,000-account replay a day's write went from 0.3s to about 3s at 500,000 rows).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay"); r.add_argument("--from", dest="first", type=int, default=61); r.add_argument("--to", dest="last", type=int, default=150)
    r.add_argument("--reset", action="store_true"); r.add_argument("--no-cache", action="store_true")
    d = sub.add_parser("day"); d.add_argument("--day", type=int, required=True); d.add_argument("--no-cache", action="store_true")
    s = sub.add_parser("speed"); s.add_argument("--scale", type=int, default=10)
    for p in (r, d, s):
        p.add_argument("--tenant", default="abc_bank")
    for p in (r, d):
        p.add_argument("--scores", choices=["all", "watch"], default="all", help="store a score row for every account, or only the watchlist")
        p.add_argument("--confirm", action="store_true", help="fetch fresh bureau / AA data for borderline accounts and re-score them")
    a = ap.parse_args()
    if a.cmd == "replay":
        run_range(a.tenant, a.first, a.last, reset=a.reset, use_cache=not a.no_cache, store_scores=a.scores, confirm=a.confirm)
    elif a.cmd == "day":
        run_range(a.tenant, a.day, a.day, use_cache=not a.no_cache, store_scores=a.scores, confirm=a.confirm)
    else:
        speed_test(a.tenant, a.scale)
