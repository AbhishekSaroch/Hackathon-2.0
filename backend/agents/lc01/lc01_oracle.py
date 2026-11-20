"""
LC-01 oracle: an INDEPENDENT second implementation of the metrics (pandas on the raw tables, written separately from the SQL
fragments in lc01_registry.py). It is used only by the tests and the evaluation to produce answer keys and to prove that every
number the agent shows reconciles to its governed definition. The agent never imports it.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db  # noqa: E402

_cache: dict = {}


def table(name: str) -> pd.DataFrame:
    if name not in _cache:
        con = sqlite3.connect(db._paths("lc01")[0])
        _cache[name] = pd.read_sql_query(f"SELECT * FROM {name}", con)
        con.close()
    return _cache[name]


def _r(a, b):
    return (a / b) if b else None


# metric -> (fact, function(df) -> (numerator, denominator or None))
F = {
    "total_collection_efficiency": ("fact_perf", lambda d: ((d.paid_emi + d.recovered).sum(), (d.emi_due + d.arrears_start).sum())),
    "current_collection_efficiency": ("fact_perf", lambda d: (d.paid_emi.sum(), d.emi_due.sum())),
    "collection_rate_count": ("fact_perf", lambda d: (d.paid_full.sum(), len(d))),
    "recovery_rate": ("fact_perf", lambda d: (d.recovered.sum(), d.arrears_start.sum())),
    "total_demand": ("fact_perf", lambda d: ((d.emi_due + d.arrears_start).sum(), None)),
    "total_collected": ("fact_perf", lambda d: ((d.paid_emi + d.recovered).sum(), None)),
    "delinquency_rate": ("fact_perf", lambda d: ((d.dpd_end > 0).sum(), len(d))),
    "par30": ("fact_perf", lambda d: (d.outstanding_end[d.dpd_end > 30].sum(), d.outstanding_end.sum())),
    "par90": ("fact_perf", lambda d: (d.outstanding_end[d.dpd_end > 90].sum(), d.outstanding_end.sum())),
    "npa_rate": ("fact_perf", lambda d: ((d.dpd_end > 90).sum(), len(d))),
    "roll_rate": ("fact_perf", lambda d: (((d.dpd_start > 0) & (d.dpd_end > d.dpd_start)).sum(), (d.dpd_start > 0).sum())),
    "new_delinquency_rate": ("fact_perf", lambda d: (((d.dpd_start == 0) & (d.dpd_end > 0)).sum(), (d.dpd_start == 0).sum())),
    "cure_rate": ("fact_perf", lambda d: (((d.dpd_start > 0) & (d.dpd_end == 0)).sum(), (d.dpd_start > 0).sum())),
    "avg_dpd": ("fact_perf", lambda d: (d.dpd_end.sum(), len(d))),
    "overdue_loans": ("fact_perf", lambda d: ((d.dpd_end > 0).sum(), None)),
    "loans_in_book": ("fact_perf", lambda d: (len(d), None)),
    "outstanding_principal": ("fact_perf", lambda d: (d.outstanding_end.sum(), None)),
    "avg_bureau_score": ("fact_perf", lambda d: (d.bureau_score.sum(), len(d))),
    "call_attempts": ("fact_calls", lambda d: (len(d), None)),
    "connect_rate": ("fact_calls", lambda d: ((d.outcome == "connected").sum(), len(d))),
    "loan_contact_rate": ("fact_calls", lambda d: (d.loan_id[d.outcome == "connected"].nunique(), d.loan_id.nunique())),
    "ptp_rate_on_connect": ("fact_calls", lambda d: ((d.disposition == "ptp").sum(), (d.outcome == "connected").sum())),
    "wrong_number_rate": ("fact_calls", lambda d: ((d.outcome == "wrong_number").sum(), len(d))),
    "avg_talk_time": ("fact_calls", lambda d: (d.duration_s[d.outcome == "connected"].sum(), (d.outcome == "connected").sum())),
    "dispute_disposition_rate": ("fact_calls", lambda d: ((d.disposition == "dispute").sum(), (d.outcome == "connected").sum())),
    "ptp_count": ("fact_ptp", lambda d: (len(d), None)),
    "ptp_kept_rate": ("fact_ptp", lambda d: (d.kept.sum(), len(d))),
    "ptp_kept_rate_amount": ("fact_ptp", lambda d: (d.paid_amount.sum(), d.promised_amount.sum())),
    "ptp_promised_amount": ("fact_ptp", lambda d: (d.promised_amount.sum(), None)),
    "payment_success_rate": ("fact_payments", lambda d: ((d.status == "success").sum(), len(d))),
    "payment_failure_rate": ("fact_payments", lambda d: ((d.status != "success").sum(), len(d))),
    "nach_bounce_rate": ("fact_payments", lambda d: (((d.channel == "nach") & (d.status == "bounced")).sum(), (d.channel == "nach").sum())),
    "digital_payment_share": ("fact_payments", lambda d: (d.amount[(d.status == "success") & d.channel.isin(["upi", "app"])].sum(), d.amount[d.status == "success"].sum())),
    "payments_received": ("fact_payments", lambda d: (d.amount[d.status == "success"].sum(), None)),
    "avg_payment_ticket": ("fact_payments", lambda d: (d.amount[d.status == "success"].sum(), (d.status == "success").sum())),
    "disputes_raised": ("fact_disputes", lambda d: (len(d), None)),
    "dispute_resolution_rate": ("fact_disputes", lambda d: (d.resolved.sum(), len(d))),
    "avg_days_to_resolve": ("fact_disputes", lambda d: (d.days_to_resolve[d.resolved == 1].sum(), d.resolved.sum())),
}


def _slice(fact, lenders, m_from, m_to, filters=None):
    d = table(fact)
    d = d[d.tenant_id.isin(lenders) & (d.month >= m_from) & (d.month <= m_to)]
    for k, v in (filters or {}).items():
        d = d[d[k].isin([v] if isinstance(v, str) else v)]
    return d


def value(metric_id, lenders, m_from, m_to, filters=None, group_by=None):
    """-> float (no group_by) or {segment: value}; lenders is a list of lender ids."""
    fact, fn = F[metric_id]
    d = _slice(fact, lenders, m_from, m_to, filters)

    def one(g):
        n, de = fn(g)
        return float(n) if de is None else (float(n) / float(de) if de else None)

    if not group_by:
        return one(d)
    key = ["tenant_id" if x == "lender" else x for x in group_by]
    return {(k if isinstance(k, tuple) else (k,)): one(g) for k, g in d.groupby(key)}


def series(metric_id, lenders, m_from, m_to, filters=None):
    fact, fn = F[metric_id]
    d = _slice(fact, lenders, m_from, m_to, filters)
    out = {}
    for m, g in d.groupby("month"):
        n, de = fn(g)
        out[m] = float(n) if de is None else (float(n) / float(de) if de else None)
    return out
