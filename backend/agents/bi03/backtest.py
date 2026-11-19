"""
BI-03 backtest.

  python backtest.py train     PUBLIC data only: cross-validated rehearsal, pick a model, save it (safe to re-run)
  python backtest.py hidden    the HIDDEN period, evaluated once. Every run is written to a ledger so the number of
                               looks at the hidden set is on record (do not tune on it).

How "top 5% flagged" is measured:
  HEADLINE  DAILY ALERTS with a fixed threshold tau, set (from scores only, no labels) so that about 5% of accounts ever
            cross it. An account is flagged the first day it crosses. Precision = share of flagged accounts that then miss
            an EMI; lift = precision / base rate (target >= 3x). Lead time = days from the first flag to its DPD 1
            (target: median >= 21 days). Baselines get the same 5% flag budget, so the comparison is fair.
  ALSO      SNAPSHOT LIFT on one day: rank the accounts still current, take the top 5%, compare with the base rate over the
            next 60 days. (Later snapshots have a shorter window and look easier, so only the first is a clean number.)

Cross-validation rule: accounts are split into 5 folds BY ACCOUNT. A model never scores an account it was trained on.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import db  # noqa: E402
import features as F  # noqa: E402
import model as M  # noqa: E402

DATA = HERE.parents[1] / "data"
REPORT = DATA / "bi03_backtest_report.json"
LEDGER = DATA / "bi03_hidden_eval_ledger.json"
MODEL_NAME = "bi03-v1"
TARGET_LIFT, TARGET_LEAD, BUDGET = 3.0, 21.0, 0.05
# Both runs have the same shape: flags may be raised from 30 days BEFORE the evaluation window (warm-up, the real system
# does not reset), and only misses inside the 60-day evaluation window count.
CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST = list(range(1, 91)), 31, [31, 45, 60], 90        # rehearsal: public days
HID_DAYS, HID_EVAL_FROM, HID_SNAPSHOTS, HID_LAST = list(range(61, 151)), 91, [91, 105, 120], 150  # hidden period
CONFIGS = [("logreg", {"C": 0.05}), ("logreg", {"C": 0.3}), ("logreg", {"C": 1.0}),
           ("hgb", {"max_depth": 2, "max_iter": 150}), ("hgb", {"max_depth": 3, "max_iter": 200}), ("hgb", {"max_depth": 3, "max_iter": 350})]


def label(kind: str, params: dict) -> str:
    return kind + " " + ", ".join(f"{k}={v}" for k, v in params.items())


def account_jitter(index: pd.Index) -> np.ndarray:
    """A fixed tiny number per account (same every day) to break ties without favouring any account order."""
    return pd.util.hash_pandas_object(pd.Series(index), index=False).to_numpy() % 100003 / 1e9


def rule_score(X: pd.DataFrame) -> pd.Series:
    """A hand-written baseline (no learning): how many warning conditions does the account meet?"""
    s = ((X["sal_delay_max3"].fillna(0) >= 4).astype(float) + (X["sal_overdue_days"].fillna(0) >= 5) + (X["bounce_60"] >= 1)
         + (X["enq_30"] >= 3) + (X["util_max3"].fillna(0) >= 0.8) + (X["emi_late_cnt_90"] >= 1))
    return s.astype(float) + account_jitter(X.index)


def random_baseline(universe: pd.Index, days: list, seed: int = 0) -> dict:
    """Flag a random 5% of accounts on random days (what 'no model' gives you under the same flag budget)."""
    rng = np.random.default_rng(seed)
    chosen = rng.random(len(universe)) < BUDGET
    flag_day = rng.choice(days, len(universe))
    return {d: pd.Series(np.where(chosen & (flag_day == d), 1.0, 0.0), index=universe) for d in days}


def current_only(scores: pd.Series, miss_day: pd.Series, day: int) -> pd.Series:
    md = miss_day.reindex(scores.index)
    return scores[(md.isna() | (md > day)).to_numpy()]


def universe_at(miss_day: pd.Series, accounts: pd.Index, day: int) -> pd.Index:
    """Accounts still current on `day` (no DPD 1 yet)."""
    md = miss_day.reindex(accounts)
    return accounts[(md.isna() | (md > day)).to_numpy()]


def score_days(raw_by_day: dict, miss_day: pd.Series, days: list, universe: pd.Index) -> dict:
    """Keep, for each day, only accounts that are in the evaluation universe AND still current that day."""
    return {d: current_only(raw_by_day(d), miss_day, d).reindex(universe).dropna() for d in days}


def evaluate(score_by_day: dict, miss_day: pd.Series, days: list, eval_from: int, snapshots: list, last_day: int,
             universe: pd.Index, tau: float | None = None) -> dict:
    tau = M.calibrate_tau(score_by_day, days, universe, BUDGET) if tau is None else tau
    snaps = [M.snapshot_lift(score_by_day[d], miss_day, d, last_day) for d in snapshots]
    return {"replay": M.replay(score_by_day, miss_day, days, tau, universe, eval_from), "snapshots": snaps}


def clean(ev: dict) -> dict:
    """The same result without the per-account table, so it can be written to JSON."""
    return {"replay": {k: v for k, v in ev["replay"].items() if k != "records"}, "snapshots": ev["snapshots"]}


def line(name: str, ev: dict) -> str:
    r = ev["replay"]
    return (f"{name:34s} lift {r['lift']:5.1f}x (precision {r['precision']:.0%} vs base {r['base_rate']:.1%}) | flagged {r['flagged_share']:.1%} | "
            f"caught {r['caught']}/{r['missers']} | median lead {r['median_lead_days']:.0f} d | snapshot lift {ev['snapshots'][0]['lift']:.1f}x")


def bootstrap_replay(rec: pd.DataFrame, reps: int = 500) -> dict:
    """95% intervals for lift and median lead by resampling accounts."""
    rng, lifts, leads = np.random.default_rng(1), [], []
    for _ in range(reps):
        s = replay_stats_safe(rec.iloc[rng.integers(0, len(rec), len(rec))])
        if s:
            lifts.append(s["lift"]); leads.append(s["median_lead_days"])
    ci = lambda v: (float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5)))
    return {"lift_ci95": ci(lifts), "lead_ci95": ci(leads)}


def replay_stats_safe(rec: pd.DataFrame):
    try:
        s = M.replay_stats(rec)
        return s if s["missers"] and s["flagged"] else None
    except Exception:
        return None


def snapshot_ci(scores: pd.Series, miss_day: pd.Series, day: int, last_day: int, reps: int = 500) -> tuple[float, float]:
    md = miss_day.reindex(scores.index)
    y = ((md > day) & (md <= min(day + M.HORIZON, last_day))).to_numpy(int)
    s, rng, n = scores.to_numpy(), np.random.default_rng(2), len(scores)
    k, lifts = max(1, int(round(BUDGET * n))), []
    for _ in range(reps):
        i = rng.integers(0, n, n)
        yy, top = y[i], np.argpartition(-(s[i] + rng.random(n) * 1e-9), k)[:k]
        if yy.mean() > 0:
            lifts.append(yy[top].mean() / yy.mean())
    return float(np.percentile(lifts, 2.5)), float(np.percentile(lifts, 97.5))


def load_report() -> dict:
    return json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}


# ====================================================================================== train (public data only)
def cmd_train(tenant: str) -> None:
    t0 = time.time()
    eng = F.FeatureEngine.from_db(tenant)
    start = M.stream_start(tenant)
    store = M.FeatureStore(eng, tenant, start)
    pub = M.miss_day_series(tenant, start)
    train = M.build_training_set(store, pub)
    print(f"training set: {len(train):,} rows (account x snapshot day), {int(train['y'].sum())} positives ({train['y'].mean():.2%}), "
          f"snapshot days {M.TRAIN_DAYS[0]}..{M.TRAIN_DAYS[-1]}, label = misses within {M.HORIZON} days, public labels only")
    accounts = store.get(1).index
    fold_of = pd.Series(np.random.default_rng(0).permutation(len(accounts)) % 5, index=accounts)
    for d in CV_DAYS:
        store.get(d)
    uni = universe_at(pub, accounts, CV_EVAL_FROM - 1)                 # accounts still current when the rehearsal's evaluation window starts
    print(f"features ready ({time.time() - t0:.0f}s). 5-fold cross-validation BY ACCOUNT, rehearsing the daily alert process: "
          f"flags from public day {CV_DAYS[0]}, misses counted on days {CV_EVAL_FROM}-{CV_LAST} ({len(uni)} accounts)\n")

    results = []
    for kind, params in CONFIGS:
        oof = {d: [] for d in CV_DAYS}
        for k in range(5):
            m = M.fit(train[fold_of.reindex(train.index).to_numpy() != k], kind, params)
            held = fold_of.index[fold_of == k]
            for d in CV_DAYS:
                oof[d].append(M.score(m, store.get(d).loc[held]))
        sbd = score_days(lambda d: pd.concat(oof[d]), pub, CV_DAYS, uni)
        ev = evaluate(sbd, pub, CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST, uni)
        results.append({"kind": kind, "params": params, **clean(ev)})
        print(line(label(kind, params), ev), flush=True)

    rule_sbd = score_days(lambda d: rule_score(store.get(d)), pub, CV_DAYS, uni)
    rnd = random_baseline(uni, CV_DAYS)
    rnd_sbd = score_days(lambda d: rnd[d], pub, CV_DAYS, uni)
    baselines = {"hand-written rule (no learning)": evaluate(rule_sbd, pub, CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST, uni),
                 "random flagging": evaluate(rnd_sbd, pub, CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST, uni, tau=0.5)}
    for name, ev in baselines.items():
        print(line(name, ev))

    # pick: best headline lift, but prefer the simpler logistic model unless gradient boosting is clearly better
    best = max(results, key=lambda r: r["replay"]["lift"])
    best_lr = max((r for r in results if r["kind"] == "logreg"), key=lambda r: r["replay"]["lift"])
    chosen = best if (best["kind"] == "logreg" or best["replay"]["lift"] > best_lr["replay"]["lift"] + 0.3) else best_lr
    print(f"\nchosen by cross-validation (public data only): {label(chosen['kind'], chosen['params'])} "
          f"(lift {chosen['replay']['lift']:.1f}x, median lead {chosen['replay']['median_lead_days']:.0f} d)")

    final = M.fit(train, chosen["kind"], chosen["params"])
    fin_sbd = score_days(lambda d: M.score(final, store.get(d)), pub, CV_DAYS, uni)
    bundle = {"version": MODEL_NAME, "kind": chosen["kind"], "params": chosen["params"], "model": final, "features": F.FEATURES,
              "medians": train[F.FEATURES].median(), "horizon": M.HORIZON, "train_days": M.TRAIN_DAYS, "trained_rows": len(train),
              "positives": int(train["y"].sum()), "created": datetime.now().isoformat(timespec="seconds"),
              "tau": M.calibrate_tau(fin_sbd, CV_DAYS, uni, BUDGET), "tau_note": "set from public-day scores only (no labels): ~5% of accounts cross it"}
    path = M.save_bundle(bundle, MODEL_NAME)
    last = fin_sbd[90]
    ex = M.explain(bundle, store.get(90).loc[M.top_fraction(last, BUDGET)])
    print(f"saved {path.name} ({M.bundle_sha(path)}) | alert threshold tau = {bundle['tau']:.4f} (about 5% of accounts cross it)")
    print("dominant signal among the top 5% on day 90:", ex["dominant_signal"].value_counts().to_dict())

    rep = load_report()
    rep["cv"] = {"chosen": {"kind": chosen["kind"], "params": chosen["params"]}, "configs": results,
                 "baselines": {k: clean(v) for k, v in baselines.items()},
                 "note": "public labels only; 5-fold CV split by account; rehearsal of the daily alert process on public days 31-90"}
    REPORT.write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    print(f"\ndone in {time.time() - t0:.0f}s. Report: {REPORT.name}")


# ====================================================================================== confirm (public data only)
def cmd_confirm(tenant: str) -> None:
    """Does fetching fresh data for borderline accounts help? A/B on PUBLIC days with out-of-fold models, same 5% flag budget."""
    import confirm as C
    t0 = time.time()
    eng = F.FeatureEngine.from_db(tenant)
    start = M.stream_start(tenant)
    store = M.FeatureStore(eng, tenant, start)
    pub = M.miss_day_series(tenant, start)
    train = M.build_training_set(store, pub)
    saved = M.load_bundle(MODEL_NAME)
    kind, params, tau = saved["kind"], saved["params"], saved["tau"]
    accounts = store.get(1).index
    fold_of = pd.Series(np.random.default_rng(0).permutation(len(accounts)) % 5, index=accounts)
    uni = universe_at(pub, accounts, CV_EVAL_FROM - 1)
    base, conf, rows = {d: [] for d in CV_DAYS}, {d: [] for d in CV_DAYS}, []
    for k in range(5):
        m = M.fit(train[fold_of.reindex(train.index).to_numpy() != k], kind, params)
        bundle_k = {"model": m, "medians": train[F.FEATURES].median(), "tau": tau}
        held = fold_of.index[fold_of == k]
        for d in CV_DAYS:
            X = store.get(d).loc[held]
            md = pub.reindex(X.index)
            cur = X[(md.isna() | (md > d)).to_numpy()]
            s = M.score(m, cur)
            res = C.confirm_borderline(eng, bundle_k, M.day_to_date(start, d), cur, s)
            base[d].append(s); conf[d].append(res["scores"])
            if len(res["decisions"]):
                dec = res["decisions"].copy()
                dec["day"] = d
                y = ((pub.reindex(dec.index) > d) & (pub.reindex(dec.index) <= min(d + M.HORIZON, 90))).astype(int)
                dec["missed"] = y.values
                rows.append(dec)
        print(f"  fold {k + 1}/5 done ({time.time() - t0:.0f}s)", flush=True)
    ev_b = evaluate(score_days(lambda d: pd.concat(base[d]), pub, CV_DAYS, uni), pub, CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST, uni)
    ev_c = evaluate(score_days(lambda d: pd.concat(conf[d]), pub, CV_DAYS, uni), pub, CV_DAYS, CV_EVAL_FROM, CV_SNAPSHOTS, CV_LAST, uni)
    print("\nPUBLIC rehearsal, out-of-fold models, each variant gets the same 5% flag budget:")
    print(line("without borderline confirmation", ev_b)); print(line("WITH borderline confirmation", ev_c))
    dec = pd.concat(rows)
    per_day = dec.groupby("day").size()
    print(f"\nborderline accounts re-scored per day: {per_day.mean():.0f} on average (about {per_day.mean() / len(accounts):.1%} of accounts), "
          f"pulls: {dec['tools'].value_counts().to_dict()}")
    print("what the fresh data decided (per decision):", dec["status"].value_counts().to_dict())
    print("share of decisions whose account really missed within 60 days (capped at day 90):")
    print((dec.groupby("status")["missed"].agg(["mean", "size"]).rename(columns={"mean": "missed", "size": "decisions"})).round(3).to_string())
    rep = load_report()
    rep["confirm_public_ab"] = {"without": clean(ev_b), "with": clean(ev_c), "decisions": dec["status"].value_counts().to_dict(),
                                "avg_pulled_accounts_per_day": float(per_day.mean())}
    REPORT.write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    print(f"\ndone in {time.time() - t0:.0f}s")


# ====================================================================================== hidden (evaluator only)
def cmd_hidden(tenant: str) -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []
    if ledger:
        print(f"WARNING: the hidden set has already been evaluated {len(ledger)} time(s) (see {LEDGER.name}). "
              f"Results from now on are no longer an untouched test. Do not tune on them.\n")
    bundle = M.load_bundle(MODEL_NAME)
    sha = M.bundle_sha(M.MODEL_DIR / f"{MODEL_NAME}.joblib")
    eng = F.FeatureEngine.from_db(tenant)
    start = M.stream_start(tenant)
    store = M.FeatureStore(eng, tenant, start)
    pub = M.miss_day_series(tenant, start)
    # ---- the evaluator (and only the evaluator) reads the hidden labels ----
    hdf = db.query_df(tenant, "SELECT account_id, dpd1_date FROM outcome_labels_hidden WHERE tenant_id = :tenant_id", database="bi03")
    hid = pd.Series(((pd.to_datetime(hdf["dpd1_date"]) - start).dt.days + 1).to_numpy(float), index=hdf["account_id"].values)
    miss = pub.combine_first(hid)
    model, tau = bundle["model"], bundle["tau"]

    accounts = store.get(1).index
    universe = universe_at(miss, accounts, HID_EVAL_FROM - 1)            # accounts still current when the hidden period starts
    sbd = score_days(lambda d: M.score(model, store.get(d)), miss, HID_DAYS, universe)
    ev = evaluate(sbd, miss, HID_DAYS, HID_EVAL_FROM, HID_SNAPSHOTS, HID_LAST, universe, tau=tau)   # tau came from PUBLIC scores
    # baselines under the same budget; the rule's threshold is also calibrated on public days only
    uni_pub = universe_at(pub, accounts, CV_EVAL_FROM - 1)
    rule_pub = score_days(lambda d: rule_score(store.get(d)), pub, CV_DAYS, uni_pub)
    rule_ev = evaluate(score_days(lambda d: rule_score(store.get(d)), miss, HID_DAYS, universe), miss, HID_DAYS, HID_EVAL_FROM, HID_SNAPSHOTS, HID_LAST,
                       universe, tau=M.calibrate_tau(rule_pub, CV_DAYS, uni_pub, BUDGET))
    rnd = random_baseline(universe, HID_DAYS, seed=5)
    rnd_ev = evaluate(score_days(lambda d: rnd[d], miss, HID_DAYS, universe), miss, HID_DAYS, HID_EVAL_FROM, HID_SNAPSHOTS, HID_LAST, universe, tau=0.5)
    boot = bootstrap_replay(ev["replay"]["records"])
    s_ci = [snapshot_ci(sbd[d], miss, d, HID_LAST) for d in HID_SNAPSHOTS]

    r = ev["replay"]
    print(f"HIDDEN PERIOD: misses counted on days {HID_EVAL_FROM}-{HID_LAST} (flags from day {HID_DAYS[0]}, public data only before day {HID_EVAL_FROM}) | "
          f"{len(universe)} accounts current on day {HID_EVAL_FROM - 1} | "
          f"model {MODEL_NAME} ({bundle['kind']}), sha {sha}, tau {tau:.4f} (set on public data)\n")
    print(line("MODEL", ev)); print(line("hand-written rule (no learning)", rule_ev)); print(line("random flagging", rnd_ev))
    print(f"\nMODEL 95% intervals (bootstrap over accounts): lift {boot['lift_ci95'][0]:.1f}-{boot['lift_ci95'][1]:.1f}x, "
          f"median lead {boot['lead_ci95'][0]:.0f}-{boot['lead_ci95'][1]:.0f} days")
    print(f"MODEL caught {r['caught']} of {r['missers']} accounts that missed (recall {r['recall']:.0%}) | flagged {r['flagged']} accounts "
          f"({r['flagged_share']:.1%}) | precision {r['precision']:.1%} vs base rate {r['base_rate']:.2%}")
    print("snapshot lifts (top 5% by score on one day; later days have shorter windows):",
          "; ".join(f"day {s['day']}: {s['lift']:.1f}x (CI {lo:.1f}-{hi:.1f})" for s, (lo, hi) in zip(ev["snapshots"], s_ci)))
    ok_lift, ok_lead = r["lift"] >= TARGET_LIFT, r["median_lead_days"] >= TARGET_LEAD
    print(f"\nACCEPTANCE  precision lift in the top 5% flagged >= {TARGET_LIFT:.0f}x : {'PASS' if ok_lift else 'FAIL'}  ({r['lift']:.1f}x)")
    print(f"            median lead time >= {TARGET_LEAD:.0f} days              : {'PASS' if ok_lead else 'FAIL'}  (median {r['median_lead_days']:.0f} d, 25th percentile {r['p25_lead_days']:.0f} d)")

    ledger.append({"at": datetime.now().isoformat(timespec="seconds"), "model": MODEL_NAME, "model_sha": sha, "lift": round(r["lift"], 2),
                   "median_lead_days": r["median_lead_days"], "looks_at_hidden_set": len(ledger) + 1})
    LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    rep = load_report()
    rep["hidden"] = {"model": MODEL_NAME, "sha": sha, "looks_at_hidden_set": len(ledger), "model_result": clean(ev), "rule_baseline": clean(rule_ev),
                     "random_baseline": clean(rnd_ev), "bootstrap": boot, "snapshot_ci95": s_ci, "pass_lift": bool(ok_lift), "pass_lead": bool(ok_lead)}
    REPORT.write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["train", "confirm", "hidden"])
    ap.add_argument("--tenant", default="abc_bank")
    a = ap.parse_args()
    {"train": cmd_train, "confirm": cmd_confirm, "hidden": cmd_hidden}[a.mode](a.tenant)
