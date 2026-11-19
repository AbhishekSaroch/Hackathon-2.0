"""
Point-in-time feature engine for BI-03 (pre-delinquency early warning).

THE RULE: a feature computed "as of day D" may only use rows whose available_from <= D.
That one filter is applied at the top of compute_features() and nothing newer is ever read, so a
backtest on day D sees exactly what the production system would have seen that morning.
(event_date is used for windows like "last 30 days"; available_from decides what is VISIBLE.)

Also by design:
  - this module never reads the outcome_labels tables (the tests enforce it with a database-level block)
  - the result does not depend on row order, and is the same on every call
  - nothing here uses a label, a DPD value or "did this account miss later" in any form

Use:
    eng = FeatureEngine.from_db("abc_bank")
    X = eng.features_at("2026-07-29")      # DataFrame: one row per account, FEATURES columns
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db  # noqa: E402

DB = "bi03"

# feature -> the business signal family it belongs to (used later to name the "dominant signal")
SIGNAL_OF = {
    "sal_delay_last": "salary_delay", "sal_delay_max3": "salary_delay", "sal_late_cnt_90": "salary_delay",
    "sal_overdue_days": "salary_delay", "sal_amt_ratio": "salary_delay", "days_since_last_salary": "salary_delay",
    "sal_delay_mean3": "salary_delay", "sal_delay_slope3": "salary_delay",             # trend: is the salary getting later?
    "emi_late_cnt_90": "late_emi_payments", "emi_late_max_90": "late_emi_payments", "emi_last_late_days": "late_emi_payments",
    "bounce_30": "other_loan_bounce", "bounce_60": "other_loan_bounce", "bounce_90": "other_loan_bounce",
    "days_since_bounce": "other_loan_bounce", "other_dpd_last": "other_loan_bounce",
    "enq_30": "new_enquiries", "enq_60": "new_enquiries", "enq_accel": "new_enquiries",   # recent 30 days vs the 30 before
    "util_last": "card_utilisation", "util_delta": "card_utilisation", "util_max3": "card_utilisation",
    "util_delta2": "card_utilisation",                                                 # change over the last 3 snapshots
    "bureau_score_last": "bureau_score", "score_delta": "bureau_score", "score_delta2": "bureau_score",
    "bal_last_to_emi": "low_balance", "bal_min30_to_emi": "low_balance", "emi_to_salary": "emi_burden",
    "open_loans": "static", "snap_age_days": "static",     # context only: never reported as the "dominant signal"
}
FEATURES = list(SIGNAL_OF)


# ------------------------------------------------------------------ loading
def prepare_events(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("event_date", "available_from"):
        df[c] = pd.to_datetime(df[c])
    df["is_salary"] = (df["event_type"] == "credit") & df["narration"].fillna("").str.startswith("SALARY")
    return df


def prepare_snaps(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("snapshot_date", "available_from"):
        df[c] = pd.to_datetime(df[c])
    return df


class FeatureEngine:
    def __init__(self, events: pd.DataFrame, snaps: pd.DataFrame, accounts: pd.DataFrame):
        self.events, self.snaps, self.accounts = events, snaps, accounts

    @classmethod
    def from_db(cls, tenant: str) -> "FeatureEngine":
        ev = db.query_df(tenant, "SELECT event_id, account_id, event_date, available_from, source, event_type, amount, value, balance, narration "
                                 "FROM events WHERE tenant_id = :tenant_id", database=DB)
        sn = db.query_df(tenant, "SELECT account_id, snapshot_date, available_from, card_utilisation, enquiries_30d, open_loans, other_loan_dpd, "
                                 "other_loan_bounces_90d, bureau_score FROM bureau_snapshots WHERE tenant_id = :tenant_id", database=DB)
        ac = db.query_df(tenant, "SELECT account_id, emi_amount FROM bp_accounts WHERE tenant_id = :tenant_id ORDER BY account_id", database=DB)
        return cls(prepare_events(ev), prepare_snaps(sn), ac)

    def features_at(self, as_of) -> pd.DataFrame:
        return compute_features(self.events, self.snaps, self.accounts, as_of)


# ------------------------------------------------------------------ the feature computation
def _delay_days(dates: pd.Series, usual_day: pd.Series) -> pd.Series:
    """How many days a salary credit is from its expected date (usual day of the nearest month). Negative = early."""
    month = dates.dt.to_period("M")
    day = usual_day.clip(1, 28).astype(int)
    best = None
    for k in (-1, 0, 1):
        p = month + k
        expected = pd.to_datetime(pd.DataFrame({"year": p.dt.year, "month": p.dt.month, "day": day}))
        d = (dates - expected).dt.days
        best = d if best is None else d.where(d.abs() < best.abs(), best)
    return best


def compute_features(events: pd.DataFrame, snaps: pd.DataFrame, accounts: pd.DataFrame, as_of) -> pd.DataFrame:
    t = pd.Timestamp(as_of)
    ev = events[events["available_from"] <= t]          # <<< POINT-IN-TIME GATE: nothing newer is ever read
    sn = snaps[snaps["available_from"] <= t]
    ids = pd.Index(accounts["account_id"].values, name="account_id")
    emi = accounts.set_index("account_id")["emi_amount"].reindex(ids)
    ago = lambda n: t - pd.Timedelta(days=n)
    out = pd.DataFrame(index=ids)

    def per_account(frame: pd.DataFrame, col: str, how: str) -> pd.Series:
        return getattr(frame.groupby("account_id")[col], how)().reindex(ids)

    def n_events(frame: pd.DataFrame) -> pd.Series:
        return frame.groupby("account_id").size().reindex(ids).fillna(0)

    # ---------------- salary ----------------
    sal = ev[ev["is_salary"]]
    base = sal[(sal["event_date"] <= ago(90)) & (sal["event_date"] > ago(420))].assign(dom=lambda d: d["event_date"].dt.day)
    usual = base.groupby("account_id")["dom"].median().reindex(ids)
    usual = usual.fillna(sal.assign(dom=sal["event_date"].dt.day).groupby("account_id")["dom"].median().reindex(ids))

    rec = sal[sal["event_date"] > ago(130)].sort_values(["account_id", "event_date", "event_id"]).copy()
    rec["usual"] = rec["account_id"].map(usual)
    rec = rec[rec["usual"].notna()]
    rec["delay"] = _delay_days(rec["event_date"], rec["usual"])
    last = rec.groupby("account_id").tail(1).set_index("account_id")
    out["sal_delay_last"] = last["delay"].reindex(ids)
    out["days_since_last_salary"] = (t - last["event_date"]).dt.days.reindex(ids)
    last3 = rec.groupby("account_id").tail(3)
    out["sal_delay_max3"] = per_account(last3, "delay", "max")
    out["sal_delay_mean3"] = per_account(last3, "delay", "mean")
    first_of_3 = last3.groupby("account_id").head(1).set_index("account_id")["delay"].reindex(ids)
    out["sal_delay_slope3"] = (out["sal_delay_last"] - first_of_3).where(last3.groupby("account_id").size().reindex(ids) >= 3)
    out["sal_late_cnt_90"] = n_events(rec[(rec["event_date"] > ago(90)) & (rec["delay"] >= 3)])
    med_amt = base.groupby("account_id")["amount"].median().reindex(ids)
    out["sal_amt_ratio"] = last["amount"].reindex(ids) / med_amt
    out["emi_to_salary"] = emi / med_amt

    # salary that has not arrived yet this month (visible today, no need to wait for the credit)
    valid = usual.notna()
    expected_now = pd.Series(pd.NaT, index=ids, dtype="datetime64[ns]")
    expected_now[valid] = pd.to_datetime(pd.DataFrame({"year": t.year, "month": t.month, "day": usual[valid].clip(1, 28).astype(int)}))
    last_credit = rec.groupby("account_id")["event_date"].max().reindex(ids)
    arrived = last_credit >= (expected_now - pd.Timedelta(days=3))
    overdue = (t - expected_now).dt.days.where((t >= expected_now) & ~arrived, 0)
    out["sal_overdue_days"] = overdue.where(valid)

    # ---------------- EMI payment behaviour (completed payments only) ----------------
    paid = ev[(ev["event_type"] == "emi_paid") & (ev["event_date"] > ago(90))].sort_values(["account_id", "event_date", "event_id"])
    out["emi_late_cnt_90"] = n_events(paid[paid["value"] > 0])
    out["emi_late_max_90"] = per_account(paid, "value", "max")
    out["emi_last_late_days"] = paid.groupby("account_id").tail(1).set_index("account_id")["value"].reindex(ids)

    # ---------------- bureau events ----------------
    bounce = ev[ev["event_type"] == "other_loan_bounce"]
    for w in (30, 60, 90):
        out[f"bounce_{w}"] = n_events(bounce[bounce["event_date"] > ago(w)])
    out["days_since_bounce"] = (t - bounce.groupby("account_id")["event_date"].max()).dt.days.reindex(ids).fillna(999)
    enq = ev[ev["event_type"] == "new_enquiry"]
    for w in (30, 60):
        out[f"enq_{w}"] = n_events(enq[enq["event_date"] > ago(w)])
    out["enq_accel"] = out["enq_30"] - (out["enq_60"] - out["enq_30"])        # last 30 days minus the 30 days before

    # ---------------- monthly bureau snapshot (D9) ----------------
    sn = sn.sort_values(["account_id", "snapshot_date"])
    last_s = sn.groupby("account_id").tail(1).set_index("account_id")
    prev_s = sn.groupby("account_id").tail(2).groupby("account_id").head(1).set_index("account_id")
    out["util_last"] = last_s["card_utilisation"].reindex(ids)
    out["util_delta"] = (last_s["card_utilisation"] - prev_s["card_utilisation"]).reindex(ids)
    last3s = sn.groupby("account_id").tail(3)
    first3s = last3s.groupby("account_id").head(1).set_index("account_id")
    three = last3s.groupby("account_id").size().reindex(ids) >= 3
    out["util_max3"] = per_account(last3s, "card_utilisation", "max")
    out["util_delta2"] = (last_s["card_utilisation"] - first3s["card_utilisation"]).reindex(ids).where(three)
    out["score_delta2"] = (last_s["bureau_score"] - first3s["bureau_score"]).reindex(ids).where(three)
    out["other_dpd_last"] = last_s["other_loan_dpd"].reindex(ids)
    out["bureau_score_last"] = last_s["bureau_score"].reindex(ids)
    out["score_delta"] = (last_s["bureau_score"] - prev_s["bureau_score"]).reindex(ids)
    out["open_loans"] = last_s["open_loans"].reindex(ids)
    out["snap_age_days"] = (t - last_s["snapshot_date"]).dt.days.reindex(ids)

    # ---------------- balance ----------------
    st = ev[(ev["source"] == "statement") & ev["balance"].notna() & (ev["event_date"] > ago(60))].sort_values(["account_id", "event_date", "event_id"])
    out["bal_last_to_emi"] = st.groupby("account_id").tail(1).set_index("account_id")["balance"].reindex(ids) / emi
    out["bal_min30_to_emi"] = per_account(st[st["event_date"] > ago(30)], "balance", "min") / emi

    return out[FEATURES].astype(float)
