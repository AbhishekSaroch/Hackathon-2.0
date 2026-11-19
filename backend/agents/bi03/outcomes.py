"""
BI-03 outcomes and threshold tuning: close the loop ("record flags and outcomes so the threshold can be tuned").

  python outcomes.py record [--as-of-day 90]        what happened to every flag, from the account's own repayment events
  python outcomes.py tune   [--cutoff-day 90]       how would other alert thresholds have done? writes a CANDIDATE threshold
  python outcomes.py activate --version <name>      make a threshold the active one (the daily runner reads the active one)
  python outcomes.py show

Outcomes come from the repayment events, exactly like a real LMS would report them later, NOT from the label tables:
  an account missed when an EMI stayed unpaid past the 4-day grace period; its DPD 1 date is the due date + 1 day.
  For a flag raised on day f:
    went_dpd1 = 1     it missed within 60 days after f
    went_dpd1 = 0     60 days (+ grace) have passed without a miss (or it only missed after the 60 days)
    went_dpd1 = NULL  not known yet: the 60 days are not over and there was no miss so far (never counted as a "no")

Keeping the hidden set honest: the stream is replayed day by day, so outcomes exist only up to the day we have reached.
Public period = stream days 1-90. Recording or tuning on later days is allowed, but it means looking at the hidden period, so it
needs --include-hidden and is written to the hidden-evaluation ledger. By default both stop at the last public day.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import db  # noqa: E402

DB = "bi03"
GRACE_DAYS = 4
HORIZON = 60
MIN_PRECISION, MIN_LEAD = 0.30, 21        # business rule used to recommend a threshold
BUDGETS = [0.02, 0.03, 0.05, 0.08, 0.12]   # candidate shares of accounts that may ever be flagged
LEDGER = HERE.parents[1] / "data" / "bi03_hidden_eval_ledger.json"


class HiddenPeriodError(Exception):
    pass


# ------------------------------------------------------------------ meta
def stream_info(tenant: str) -> dict:
    rows = db.query(tenant, "SELECT key, value FROM bp_meta WHERE tenant_id = :tenant_id", database=DB)
    m = {r["key"]: r["value"] for r in rows}
    start = pd.Timestamp(m["stream_start_date"])
    return {"start": start, "public_days": int(m["public_days"]), "last_public_day": int(m["public_days"])}


def to_date(info: dict, day: int) -> pd.Timestamp:
    return info["start"] + pd.Timedelta(days=day - 1)


def to_day(info: dict, date) -> int:
    return int((pd.Timestamp(date) - info["start"]).days + 1)


def guard_hidden(info: dict, day: int, include_hidden: bool, what: str) -> None:
    """Going past the last public day means looking at the hidden period: refuse unless asked, and put it on the record."""
    if day <= info["last_public_day"]:
        return
    if not include_hidden:
        raise HiddenPeriodError(f"{what} up to day {day} goes past the last public day ({info['last_public_day']}), which means looking at the hidden period. "
                                "Re-run with --include-hidden if that is what you want; it will be written to the hidden-evaluation ledger.")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []
    ledger.append({"at": datetime.now().isoformat(timespec="seconds"), "kind": what, "through_day": day, "looks_at_hidden_set": len(ledger) + 1})
    LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    print(f"NOTE: {what} through day {day} looks at the hidden period. Written to the ledger (look #{len(ledger)}).")


# ------------------------------------------------------------------ outcomes from repayment events
def load_repayments(tenant: str) -> pd.DataFrame:
    df = db.query_df(tenant, "SELECT account_id, event_date, available_from, event_type FROM events WHERE tenant_id = :tenant_id AND source = 'repayment'", database=DB)
    df["event_date"], df["available_from"] = pd.to_datetime(df["event_date"]), pd.to_datetime(df["available_from"])
    return df


def first_miss_dates(rep: pd.DataFrame, as_of, grace: int = GRACE_DAYS) -> pd.Series:
    """account_id -> DPD 1 date (due date + 1) of the first EMI that is unpaid past the grace period, using only rows visible on as_of."""
    t = pd.Timestamp(as_of)
    vis = rep[rep["available_from"] <= t]
    dues = vis[vis["event_type"] == "emi_due"].sort_values(["account_id", "event_date"]).copy()
    paid = vis[vis["event_type"] == "emi_paid"].groupby("account_id").size()
    dues["n"] = dues.groupby("account_id").cumcount()                       # 0 = oldest due
    dues["paid_before"] = dues["account_id"].map(paid).fillna(0)
    unpaid = dues[(dues["n"] == dues["paid_before"]) & (dues["event_date"] <= t - pd.Timedelta(days=grace + 1))]    # the first EMI nobody paid
    return (unpaid.set_index("account_id")["event_date"] + pd.Timedelta(days=1))


def derive_outcomes(flags: pd.DataFrame, miss: pd.Series, as_of, horizon: int = HORIZON, grace: int = GRACE_DAYS) -> pd.DataFrame:
    """One row per flag: went_dpd1 (1 / 0 / NULL), dpd1_date, lead_time_days. See the module docstring."""
    t = pd.Timestamp(as_of)
    f = pd.to_datetime(flags["flagged_on"])
    m = flags["account_id"].map(miss)
    lead = (m - f).dt.days
    final_no = t >= f + pd.Timedelta(days=horizon + grace + 1)               # the 60 days are over and the grace period after them as well
    went = pd.Series(np.nan, index=flags.index, dtype="float64")
    went[(lead > 0) & (lead <= horizon)] = 1
    went[(lead > horizon) | (lead <= 0)] = 0                                 # missed only later than 60 days, or already past due when flagged
    went[m.isna() & final_no] = 0
    out = pd.DataFrame({"flag_id": flags["flag_id"].astype(int), "account_id": flags["account_id"], "went_dpd1": went,
                        "dpd1_date": m.dt.strftime("%Y-%m-%d").where(m.notna() & (lead > 0)), "lead_time_days": lead.where((lead > 0) & (lead <= horizon)),
                        "evaluated_on": t.strftime("%Y-%m-%d")})
    return out


def record_outcomes(tenant: str, as_of_day: int | None = None, include_hidden: bool = False, rep: pd.DataFrame | None = None) -> dict:
    info = stream_info(tenant)
    day = as_of_day or info["last_public_day"]
    guard_hidden(info, day, include_hidden, "recording flag outcomes")
    t = to_date(info, day)
    rep = rep if rep is not None else load_repayments(tenant)
    flags = db.query_df(tenant, "SELECT flag_id, account_id, flagged_on FROM flags WHERE tenant_id = :tenant_id AND flagged_on <= :t", {"t": t.strftime("%Y-%m-%d")}, database=DB)
    if flags.empty:
        return {"flags": 0}
    out = derive_outcomes(flags, first_miss_dates(rep, t), t)
    db.execute(tenant, "DELETE FROM flag_outcomes WHERE tenant_id = :tenant_id", database=DB)
    rows = out.astype(object).where(out.notna(), None).to_dict("records")
    db.insert_many(tenant, "flag_outcomes", rows, database=DB)
    hit, no, pend = int((out["went_dpd1"] == 1).sum()), int((out["went_dpd1"] == 0).sum()), int(out["went_dpd1"].isna().sum())
    n = len(out)
    # Hits are known at once but a "no" is only known after 60 days, so dividing by the RESOLVED flags would flatter the model.
    # Report an honest range instead: every pending flag could still turn out either way.
    return {"as_of_day": day, "flags": n, "hit": hit, "no_miss": no, "pending": pend,
            "precision_range": [round(hit / n, 3), round((hit + pend) / n, 3)],
            "median_lead_days_of_hits": float(out["lead_time_days"].median()) if out["lead_time_days"].notna().any() else None}


# ------------------------------------------------------------------ threshold tuning
def candidate_table(scores: pd.DataFrame, miss_day: pd.Series, info: dict, first_day: int, cutoff_day: int, current_tau: float | None) -> pd.DataFrame:
    """For each candidate threshold: if we had alerted with it over these days, how would it have done on the outcomes we know?
    scores: account_id, day, score (all current accounts, every day). miss_day: account_id -> day of DPD 1."""
    import model as M                                          # reuse the exact statistics the backtest uses
    universe = pd.Index(scores.loc[scores["day"] == first_day, "account_id"].unique())
    peak = scores.groupby("account_id")["score"].max().reindex(universe)
    taus = {f"top {b:.0%}": float(peak.quantile(1 - b)) for b in BUDGETS}
    if current_tau is not None:
        taus["ACTIVE now"] = float(current_tau)
    md = miss_day.reindex(universe)
    rows = []
    for name, tau in taus.items():
        flagged = scores[scores["score"] >= tau].groupby("account_id")["day"].min()
        rec = pd.DataFrame(index=universe)
        rec["flag_day"] = flagged.reindex(universe)
        rec["miss_day"] = md.where((md >= first_day) & (md <= cutoff_day))
        rec["hit"] = rec["flag_day"].notna() & rec["miss_day"].notna() & (rec["flag_day"] < rec["miss_day"])
        rec["lead"] = (rec["miss_day"] - rec["flag_day"]).where(rec["hit"])
        s = M.replay_stats(rec)
        rows.append({"candidate": name, "tau": tau, "flagged": s["flagged"], "flagged_share": s["flagged_share"], "precision": s["precision"], "lift": s["lift"],
                     "recall": s["recall"], "median_lead_days": s["median_lead_days"], "missers": s["missers"], "hits": s["caught"]})
    return pd.DataFrame(rows)


def recommend(table: pd.DataFrame) -> tuple[pd.Series, str]:
    """Highest recall among thresholds that keep precision >= 30% AND median lead >= 21 days.
    A short outcome window cuts lead times short (and holds precision down), so if no candidate can meet the lead rule we fall back to the
    precision rule alone, and only if even that fails to the best lift. The reason is returned so nobody mistakes a fallback for a clean pass."""
    cand = table[table["candidate"] != "ACTIVE now"]
    both = cand[(cand["precision"] >= MIN_PRECISION) & (cand["median_lead_days"] >= MIN_LEAD)]
    if len(both):
        return both.sort_values(["recall", "precision"], ascending=False).iloc[0], "precision and lead-time rules both met"
    prec = cand[cand["precision"] >= MIN_PRECISION]
    if len(prec):
        return prec.sort_values(["recall", "precision"], ascending=False).iloc[0], "lead-time rule could not be met on this window (lead times are cut short by it); precision rule met"
    return cand.sort_values("lift", ascending=False).iloc[0], "no candidate met the precision rule; showing the best lift"


def tune_threshold(tenant: str, cutoff_day: int | None = None, include_hidden: bool = False, rep: pd.DataFrame | None = None) -> dict:
    info = stream_info(tenant)
    cutoff = cutoff_day or info["last_public_day"]
    guard_hidden(info, cutoff, include_hidden, "tuning the threshold")
    t = to_date(info, cutoff)
    days = db.query_df(tenant, "SELECT MIN(as_of_date) a FROM risk_scores WHERE tenant_id = :tenant_id", database=DB)["a"][0]
    if days is None:
        return {"error": "no risk scores yet: run the daily runner first"}
    first_day = to_day(info, days)
    sc = db.query_df(tenant, "SELECT account_id, as_of_date, score FROM risk_scores WHERE tenant_id = :tenant_id AND as_of_date <= :t",
                     {"t": t.strftime("%Y-%m-%d")}, database=DB)
    sc["day"] = (pd.to_datetime(sc.pop("as_of_date")) - info["start"]).dt.days + 1
    rep = rep if rep is not None else load_repayments(tenant)
    miss = first_miss_dates(rep, t)
    miss_day = pd.Series((miss - info["start"]).dt.days + 1, index=miss.index)
    active = db.query_one(tenant, "SELECT threshold_version, value FROM thresholds WHERE tenant_id = :tenant_id AND is_active = 1", database=DB)
    table = candidate_table(sc, miss_day, info, first_day, cutoff, active["value"] if active else None)
    best, reason = recommend(table)
    window = cutoff - first_day + 1
    version = f"bi03-v1-tau-cand-{t.strftime('%Y%m%d')}"
    db.execute(tenant, "DELETE FROM thresholds WHERE tenant_id = :tenant_id AND threshold_version = :v", {"v": version}, database=DB)
    db.insert(tenant, "thresholds", {"threshold_version": version, "value": float(best["tau"]), "top_pct": float(best["flagged_share"]), "precision_top": float(best["precision"]),
                                     "lift": float(best["lift"]), "median_lead_days": float(best["median_lead_days"]), "is_active": 0,
                                     "notes": f"CANDIDATE from outcomes known up to day {cutoff} (alert window days {first_day}-{cutoff}, only {window} days: precision is a lower bound and lead "
                                              f"times are truncated). Rule: highest recall with precision >= {MIN_PRECISION:.0%} and median lead >= {MIN_LEAD} d. Outcome: {reason}. "
                                              "Not active until someone activates it."}, database=DB)
    return {"cutoff_day": cutoff, "window": [first_day, cutoff], "window_days": window, "table": table, "recommended": best.to_dict(), "reason": reason, "version": version}


def activate_threshold(tenant: str, version: str) -> dict:
    row = db.query_one(tenant, "SELECT threshold_version, value FROM thresholds WHERE tenant_id = :tenant_id AND threshold_version = :v", {"v": version}, database=DB)
    if row is None:
        raise ValueError(f"unknown threshold version {version}")
    db.execute(tenant, "UPDATE thresholds SET is_active = CASE WHEN threshold_version = :v THEN 1 ELSE 0 END WHERE tenant_id = :tenant_id", {"v": version}, database=DB)
    return {"active": version, "tau": row["value"]}


# ------------------------------------------------------------------ CLI
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record"); r.add_argument("--as-of-day", type=int)
    t = sub.add_parser("tune"); t.add_argument("--cutoff-day", type=int)
    a = sub.add_parser("activate"); a.add_argument("--version", required=True)
    s = sub.add_parser("show")
    for p in (r, t, a, s):
        p.add_argument("--tenant", default="abc_bank")
    for p in (r, t):
        p.add_argument("--include-hidden", action="store_true")
    args = ap.parse_args()
    try:
        if args.cmd == "record":
            print(record_outcomes(args.tenant, args.as_of_day, args.include_hidden))
        elif args.cmd == "tune":
            res = tune_threshold(args.tenant, args.cutoff_day, args.include_hidden)
            pd.set_option("display.width", 200)
            print(f"alert window days {res['window'][0]}-{res['window'][1]} (outcomes known up to day {res['cutoff_day']})")
            print(res["table"].round(3).to_string(index=False))
            print(f"\nCAUTION: the window is only {res['window_days']} days. Precision is a lower bound (flags raised late have had little time to miss) and lead times are cut short by the window.")
            print(f"recommended: {res['recommended']['candidate']} (tau {res['recommended']['tau']:.4f}), because: {res['reason']}")
            print(f"saved as INACTIVE candidate '{res['version']}'. To use it: python outcomes.py activate --version {res['version']}")
        elif args.cmd == "activate":
            print(activate_threshold(args.tenant, args.version))
        else:
            print(db.query_df(args.tenant, "SELECT threshold_version, ROUND(value, 4) tau, top_pct, ROUND(precision_top, 3) precision, ROUND(lift, 1) lift, median_lead_days, is_active "
                                           "FROM thresholds WHERE tenant_id = :tenant_id", database=DB).to_string(index=False))
    except HiddenPeriodError as e:
        sys.exit(f"REFUSED: {e}")
