"""
LC-01 governed metric definitions: the ONLY definitions the agent may use. (Module names carry the lc01_ prefix on purpose:
LC-04 puts its own `registry` / `access` on the import path, and two modules with one name would shadow each other.)

A metric is one aggregate SELECT over one fact table: numerator / denominator (or a plain sum). The SQL fragments here are
the only SQL text that is ever put into a query; user text never reaches SQL. Dimensions are whitelisted per fact table, so a
question can never slice a metric by something that does not exist for it (a call has an agency but no payment channel).

Some definitions overlap ON PURPOSE (four different "collection rates", four "delinquency" measures ...). They carry an
`overlap` group name. A question that uses the generic term without saying which one is ambiguous and must be answered with a
clarifying question, never with a silent guess.
"""
from dataclasses import dataclass, field

MONTHS = [f"2026-{m:02d}" for m in range(1, 11)]
LATEST_MONTH = MONTHS[-1]
LENDER_IDS = ("L1", "L2", "L3")
LENDER_NAMES = {"L1": "Kaveri Finance", "L2": "Lakshya Capital", "L3": "Meridian Credit"}

DIM_COL = {"city": "city", "region": "region", "product": "product", "agency": "agency", "vintage": "vintage",
           "bucket": "bucket", "channel": "channel", "disposition": "disposition", "outcome": "outcome", "reason": "reason"}
DIM_HELP = {"city": "city of the loan", "region": "North | West | South | East", "product": "loan product", "agency": "collection agency",
            "vintage": "disbursal quarter e.g. 2025Q3", "bucket": "opening DPD bucket: current | 1-30 | 31-60 | 61-90 | 90+",
            "channel": "payment channel: nach | upi | app | cash | cheque", "disposition": "call result: ptp | callback | refuses | dispute | paid_already | none",
            "outcome": "call outcome: connected | no_answer | wrong_number", "reason": "dispute reason",
            "lender": "which lender (only for users who may see more than one)"}
FACTS = {
    "fact_perf": ["city", "region", "product", "agency", "vintage", "bucket"],
    "fact_calls": ["city", "region", "product", "agency", "bucket", "outcome", "disposition"],
    "fact_ptp": ["city", "region", "product", "agency", "bucket"],
    "fact_payments": ["city", "region", "product", "agency", "channel"],
    "fact_disputes": ["city", "region", "product", "agency", "reason"],
}
# tables the SQL checker lets a query touch (never users, history or audit tables)
TABLES_ALLOWED = set(FACTS) | {"loans", "agencies", "lender_config"}
UNITS = ("pct", "rs", "count", "score", "days", "sec")


class MetricError(Exception):
    """Anything that would break governance: unknown metric, dimension or month."""


@dataclass(frozen=True)
class Metric:
    metric_id: str
    name: str
    family: str
    fact: str
    num: str
    den: str | None
    unit: str
    good: str                                   # 'up' | 'down' | 'neutral'
    definition: str
    aliases: tuple = ()
    overlap: str | None = None
    drivers: tuple = ()

    @property
    def dims(self):
        return FACTS[self.fact]


_D = {  # default driver metrics (evidence shown in a root-cause answer), by family
    "collections": ("connect_rate", "loan_contact_rate", "ptp_kept_rate", "payment_failure_rate"),
    "delinquency": ("new_delinquency_rate", "cure_rate", "nach_bounce_rate", "connect_rate"),
    "calls": ("wrong_number_rate", "ptp_rate_on_connect"),
    "ptp": ("connect_rate", "ptp_rate_on_connect"),
    "payments": ("nach_bounce_rate", "payment_failure_rate"),
    "disputes": (),
    "book": (),
}


def _m(mid, name, family, fact, num, den, unit, good, definition, aliases=(), overlap=None, drivers=None):
    return Metric(mid, name, family, fact, num, den, unit, good, definition, tuple(a.lower() for a in aliases), overlap,
                  _D[family] if drivers is None else tuple(drivers))


_P = "fact_perf"
_ML = [
    # ---- collections (4 overlapping definitions of "collection rate") ----
    _m("total_collection_efficiency", "Total collection efficiency", "collections", _P, "SUM(paid_emi + recovered)", "SUM(emi_due + arrears_start)", "pct", "up",
       "Money collected (this month's EMIs + arrears) / total demand (this month's EMIs + opening arrears), by amount.",
       ("total collection efficiency", "overall collection efficiency", "collection efficiency including arrears", "collection efficiency incl arrears", "total ce"), "collection"),
    _m("current_collection_efficiency", "Current collection efficiency", "collections", _P, "SUM(paid_emi)", "SUM(emi_due)", "pct", "up",
       "EMIs paid / EMIs due this month, by amount. Ignores arrears.",
       ("current collection efficiency", "current month collection efficiency", "current ce", "emi collection efficiency", "collection efficiency on current emi", "collection efficiency excluding arrears"), "collection"),
    _m("collection_rate_count", "Collection rate (by count)", "collections", _P, "SUM(paid_full)", "COUNT(*)", "pct", "up",
       "Loans that paid their full EMI / loans due, by count of loans (not amount).",
       ("collection rate by count", "count based collection rate", "loan count collection rate", "paid in full rate", "collection rate (by count)"), "collection"),
    _m("recovery_rate", "Recovery rate (arrears)", "collections", _P, "SUM(recovered)", "SUM(arrears_start)", "pct", "up",
       "Overdue amount recovered / opening overdue amount. Only looks at loans already late.",
       ("recovery rate", "arrears recovery", "arrears recovery rate", "overdue recovery"), "collection"),
    _m("total_demand", "Total demand", "collections", _P, "SUM(emi_due + arrears_start)", None, "rs", "neutral",
       "EMIs due this month plus opening arrears.", ("demand", "total demand", "amount due")),
    _m("total_collected", "Total collected", "collections", _P, "SUM(paid_emi + recovered)", None, "rs", "up",
       "Money collected against EMIs and arrears (loan-level view).", ("total collected", "amount collected", "collections amount", "money collected")),
    # ---- delinquency / buckets ----
    _m("delinquency_rate", "Delinquency rate (DPD > 0)", "delinquency", _P, "SUM(CASE WHEN dpd_end > 0 THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "down",
       "Loans with any overdue days at month end / all loans, by count.",
       ("delinquency rate (dpd > 0)", "delinquency rate by count", "dpd>0 rate", "dpd > 0 delinquency", "overdue loan rate", "overdue rate by count"), "delinquency"),
    _m("par30", "PAR 30", "delinquency", _P, "SUM(CASE WHEN dpd_end > 30 THEN outstanding_end ELSE 0 END)", "SUM(outstanding_end)", "pct", "down",
       "Outstanding principal of loans more than 30 days past due / total outstanding principal.",
       ("par30", "par 30", "par>30", "portfolio at risk 30", "portfolio at risk over 30 days"), "delinquency"),
    _m("par90", "PAR 90", "delinquency", _P, "SUM(CASE WHEN dpd_end > 90 THEN outstanding_end ELSE 0 END)", "SUM(outstanding_end)", "pct", "down",
       "Outstanding principal of loans more than 90 days past due / total outstanding principal.",
       ("par90", "par 90", "par>90", "portfolio at risk 90", "portfolio at risk over 90 days"), "delinquency"),
    _m("npa_rate", "NPA rate (count)", "delinquency", _P, "SUM(CASE WHEN dpd_end > 90 THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "down",
       "Loans more than 90 days past due / all loans, by count (not by outstanding amount).",
       ("npa rate", "npa", "npa share", "gross npa", "gnpa"), "delinquency"),
    _m("roll_rate", "Roll rate (delinquent loans getting worse)", "delinquency", _P,
       "SUM(CASE WHEN dpd_start > 0 AND dpd_end > dpd_start THEN 1 ELSE 0 END)", "SUM(CASE WHEN dpd_start > 0 THEN 1 ELSE 0 END)", "pct", "down",
       "Among loans already late at the start of the month, the share that ended it later still (higher DPD).",
       ("roll forward rate", "roll rate of delinquent loans", "delinquent roll rate", "roll rate (delinquent loans getting worse)"), "roll"),
    _m("new_delinquency_rate", "New delinquency rate (current to late)", "delinquency", _P,
       "SUM(CASE WHEN dpd_start = 0 AND dpd_end > 0 THEN 1 ELSE 0 END)", "SUM(CASE WHEN dpd_start = 0 THEN 1 ELSE 0 END)", "pct", "down",
       "Among loans current at the start of the month, the share that became late during it (current to 1-30 flow).",
       ("new delinquency rate", "new delinquency", "slippage", "slippage rate", "current to 1-30", "flow rate", "fresh delinquency", "current to late rate"), "roll"),
    _m("cure_rate", "Cure rate", "delinquency", _P, "SUM(CASE WHEN dpd_start > 0 AND dpd_end = 0 THEN 1 ELSE 0 END)", "SUM(CASE WHEN dpd_start > 0 THEN 1 ELSE 0 END)", "pct", "up",
       "Among loans late at the start of the month, the share that became fully current by month end.", ("cure rate", "cured", "cure")),
    _m("avg_dpd", "Average DPD", "delinquency", _P, "SUM(dpd_end)", "COUNT(*)", "days", "down",
       "Average days past due at month end across all loans (current loans count as 0).", ("average dpd", "avg dpd", "mean dpd", "average days past due")),
    _m("overdue_loans", "Overdue loans", "delinquency", _P, "SUM(CASE WHEN dpd_end > 0 THEN 1 ELSE 0 END)", None, "count", "down",
       "Number of loans with DPD > 0 at month end.", ("overdue loans", "number of overdue loans", "delinquent loans", "late loans")),
    # ---- the book ----
    _m("loans_in_book", "Loans in book", "book", _P, "COUNT(*)", None, "count", "neutral", "Number of live loans in the month.", ("loans in book", "how many loans", "number of loans", "loan count", "active loans", "book size")),
    _m("outstanding_principal", "Outstanding principal", "book", _P, "SUM(outstanding_end)", None, "rs", "neutral", "Principal still to be repaid at month end.",
       ("outstanding principal", "outstanding", "pos", "principal outstanding", "portfolio outstanding", "book value")),
    _m("avg_bureau_score", "Average bureau score", "book", _P, "SUM(bureau_score)", "COUNT(*)", "score", "up", "Average credit bureau score of the loans.", ("bureau score", "average bureau score", "avg bureau score", "credit score")),
    # ---- calls ----
    _m("call_attempts", "Call attempts", "calls", "fact_calls", "COUNT(*)", None, "count", "neutral", "Number of collection calls placed.", ("call attempts", "calls made", "number of calls", "call volume", "calls placed", "calls")),
    _m("connect_rate", "Connect rate (calls)", "calls", "fact_calls", "SUM(CASE WHEN outcome = 'connected' THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "up",
       "Calls that connected / calls placed, by number of calls.", ("connect rate", "call connect rate", "calls connected rate", "connectivity", "call connectivity"), "contact"),
    _m("loan_contact_rate", "Contact rate (loans reached)", "calls", "fact_calls", "COUNT(DISTINCT CASE WHEN outcome = 'connected' THEN loan_id END)", "COUNT(DISTINCT loan_id)", "pct", "up",
       "Loans reached by at least one connected call / loans that were called, by number of loans.",
       ("loan contact rate", "contact rate by loan", "loans reached", "right party contact rate", "rpc rate", "customer contact rate", "loans contacted rate"), "contact"),
    _m("ptp_rate_on_connect", "PTP rate on connect", "calls", "fact_calls", "SUM(CASE WHEN disposition = 'ptp' THEN 1 ELSE 0 END)", "SUM(CASE WHEN outcome = 'connected' THEN 1 ELSE 0 END)", "pct", "up",
       "Connected calls that ended in a promise to pay / connected calls.", ("ptp rate on connect", "ptp conversion", "promise conversion", "ptp rate on connected calls", "promise rate on connect")),
    _m("wrong_number_rate", "Wrong-number rate", "calls", "fact_calls", "SUM(CASE WHEN outcome = 'wrong_number' THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "down",
       "Calls that reached a wrong number / calls placed.", ("wrong number rate", "wrong numbers", "wrong-number rate", "bad number rate")),
    _m("avg_talk_time", "Average talk time", "calls", "fact_calls", "SUM(CASE WHEN outcome = 'connected' THEN duration_s ELSE 0 END)", "SUM(CASE WHEN outcome = 'connected' THEN 1 ELSE 0 END)", "sec", "neutral",
       "Average length of a connected call, in seconds.", ("average talk time", "avg talk time", "talk time", "call duration", "average call duration")),
    _m("dispute_disposition_rate", "Dispute rate on connect", "calls", "fact_calls", "SUM(CASE WHEN disposition = 'dispute' THEN 1 ELSE 0 END)", "SUM(CASE WHEN outcome = 'connected' THEN 1 ELSE 0 END)", "pct", "down",
       "Connected calls where the customer raised a dispute / connected calls.", ("dispute rate on connect", "dispute disposition rate", "disputes on calls")),
    # ---- promises to pay (2 overlapping) ----
    _m("ptp_count", "Promises to pay", "ptp", "fact_ptp", "COUNT(*)", None, "count", "neutral", "Number of promises to pay taken.", ("ptp count", "number of ptps", "promises to pay", "number of promises", "ptps taken", "ptps")),
    _m("ptp_kept_rate", "PTP kept rate (by count)", "ptp", "fact_ptp", "SUM(kept)", "COUNT(*)", "pct", "up",
       "Promises kept in full / promises taken, by count.", ("ptp kept rate by count", "ptp kept rate (by count)", "count based ptp kept rate", "promises kept by count"), "ptp_kept"),
    _m("ptp_kept_rate_amount", "PTP kept rate (by amount)", "ptp", "fact_ptp", "SUM(paid_amount)", "SUM(promised_amount)", "pct", "up",
       "Money actually paid against promises / money promised.", ("ptp kept rate by amount", "ptp kept rate (by amount)", "amount based ptp kept rate", "ptp realisation", "ptp realization", "promised amount realised"), "ptp_kept"),
    _m("ptp_promised_amount", "Promised amount", "ptp", "fact_ptp", "SUM(promised_amount)", None, "rs", "neutral", "Total amount promised across promises to pay.", ("promised amount", "ptp amount", "amount promised")),
    # ---- payments (2 overlapping) ----
    _m("payment_success_rate", "Payment success rate", "payments", "fact_payments", "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "up",
       "Successful payment attempts / all payment attempts (all channels).", ("payment success rate", "payment success", "successful payments rate")),
    _m("payment_failure_rate", "Payment failure rate (all channels)", "payments", "fact_payments", "SUM(CASE WHEN status <> 'success' THEN 1 ELSE 0 END)", "COUNT(*)", "pct", "down",
       "Failed or bounced payment attempts / all payment attempts, over every channel.", ("payment failure rate", "payment failures", "failed payments rate", "failure rate of payments"), "bounce"),
    _m("nach_bounce_rate", "NACH bounce rate", "payments", "fact_payments", "SUM(CASE WHEN channel = 'nach' AND status = 'bounced' THEN 1 ELSE 0 END)", "SUM(CASE WHEN channel = 'nach' THEN 1 ELSE 0 END)", "pct", "down",
       "Bounced NACH (auto-debit) mandates / NACH attempts. NACH only.", ("nach bounce rate", "nach bounces", "nach bounce", "auto debit bounce rate", "mandate bounce rate", "ecs bounce rate"), "bounce"),
    _m("digital_payment_share", "Digital payment share", "payments", "fact_payments", "SUM(CASE WHEN status = 'success' AND channel IN ('upi','app') THEN amount ELSE 0 END)", "SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END)", "pct", "up",
       "Successful money received over UPI or the app / all successful money received.", ("digital payment share", "digital share", "upi share", "digital payments share", "share of digital payments")),
    _m("payments_received", "Payments received", "payments", "fact_payments", "SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END)", None, "rs", "up",
       "Total amount of successful payments (payment-level view).", ("payments received", "payment amount", "amount of payments", "money received")),
    _m("avg_payment_ticket", "Average payment size", "payments", "fact_payments", "SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END)", "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)", "rs", "neutral",
       "Average amount of a successful payment.", ("average payment size", "average ticket size", "avg payment ticket", "average payment", "ticket size")),
    # ---- disputes ----
    _m("disputes_raised", "Disputes raised", "disputes", "fact_disputes", "COUNT(*)", None, "count", "down", "Number of customer disputes raised.", ("disputes raised", "number of disputes", "disputes", "dispute count", "complaints")),
    _m("dispute_resolution_rate", "Dispute resolution rate", "disputes", "fact_disputes", "SUM(resolved)", "COUNT(*)", "pct", "up", "Disputes resolved / disputes raised.", ("dispute resolution rate", "disputes resolved rate", "resolution rate")),
    _m("avg_days_to_resolve", "Average days to resolve a dispute", "disputes", "fact_disputes", "SUM(CASE WHEN resolved = 1 THEN days_to_resolve ELSE 0 END)", "SUM(resolved)", "days", "down",
       "Average days taken to resolve the disputes that were resolved.", ("average days to resolve", "avg days to resolve", "dispute resolution time", "time to resolve disputes", "resolution time")),
]
METRICS: dict[str, Metric] = {m.metric_id: m for m in _ML}
assert len(METRICS) == len(_ML)

# Generic words that fit several definitions. Using one WITHOUT a qualifier makes a question ambiguous.
AMBIGUOUS_TERMS = {
    "collection": ["collection efficiency", "collection rate", "collections performance", "collection performance", "collections", "collection", "ce"],
    "delinquency": ["delinquency", "delinquent", "delinquencies", "overdue", "bad loans", "bad loan", "stressed loans", "portfolio quality", "asset quality", "portfolio at risk", "par"],
    "roll": ["roll rate", "roll", "rolled", "rolling", "rolls", "bucket roll", "bucket rolled", "bucket movement", "roll-forward", "bucket rolls"],
    "contact": ["contact rate", "contactability", "contact"],
    "ptp_kept": ["ptp kept rate", "ptp kept", "promises kept", "promise kept rate", "promise-to-pay kept", "ptp honour rate", "ptp honor rate"],
    "bounce": ["bounce rate", "bounces", "bounce", "payment failures", "failure rate", "failed payments"],
}
OVERLAP_GROUPS: dict[str, list[str]] = {}
for _mm in METRICS.values():
    if _mm.overlap:
        OVERLAP_GROUPS.setdefault(_mm.overlap, []).append(_mm.metric_id)
assert set(OVERLAP_GROUPS) == set(AMBIGUOUS_TERMS), (OVERLAP_GROUPS.keys(), AMBIGUOUS_TERMS.keys())

# "underperforming / best / worst" with no measure named: which measures could the person mean?
PERFORMANCE_CHOICES = {
    "agency": ["total_collection_efficiency", "connect_rate", "ptp_kept_rate", "cure_rate"],
    "default": ["total_collection_efficiency", "delinquency_rate", "roll_rate"],
}


def get(metric_id: str) -> Metric:
    if metric_id not in METRICS:
        raise MetricError(f"{metric_id!r} is not a governed metric")
    return METRICS[metric_id]


def month_ok(m: str) -> bool:
    return m in MONTHS


def months_between(a: str, b: str) -> list[str]:
    if not (month_ok(a) and month_ok(b)):
        raise MetricError(f"months must be between {MONTHS[0]} and {MONTHS[-1]}")
    if a > b:
        a, b = b, a
    return [m for m in MONTHS if a <= m <= b]


def prev_month(m: str):
    i = MONTHS.index(m)
    return MONTHS[i - 1] if i else None


def catalog():
    return [dict(metric_id=m.metric_id, name=m.name, family=m.family, unit=m.unit, good=m.good, definition=m.definition, dims=list(m.dims),
                 overlap=m.overlap, overlaps_with=[x for x in OVERLAP_GROUPS.get(m.overlap, []) if x != m.metric_id] if m.overlap else [],
                 table=m.fact, formula=(f"{m.num} / {m.den}" if m.den else m.num)) for m in METRICS.values()]


def compact_catalog_text() -> str:
    """One short line per metric for the LLM prompt: id | what it is | overlap group."""
    lines = []
    for m in METRICS.values():
        tag = f" [overlap:{m.overlap}]" if m.overlap else ""
        lines.append(f"{m.metric_id} = {m.name}{tag}")
    return "\n".join(lines)


def fmt_value(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "pct":
        return f"{v * 100:.1f}%"
    if unit == "rs":
        a = abs(v)
        return f"Rs {v / 1e7:.2f} crore" if a >= 1e7 else f"Rs {v / 1e5:.2f} lakh" if a >= 1e5 else f"Rs {v:,.0f}"
    if unit == "count":
        return f"{v:,.0f}"
    if unit == "score":
        return f"{v:.0f}"
    if unit == "days":
        return f"{v:.1f} days"
    if unit == "sec":
        return f"{v:.0f} s"
    return f"{v}"


def fmt_delta(a, b, unit: str) -> str:
    """Change from a to b, in the metric's own terms (percentage points for rates)."""
    if a is None or b is None:
        return "n/a"
    d = b - a
    if unit == "pct":
        return f"{d * 100:+.1f} pts"
    if unit in ("count", "score"):
        return f"{d:+,.0f}"
    if unit == "rs":
        return ("+" if d >= 0 else "-") + fmt_value(abs(d), "rs")
    return f"{d:+.1f}" + (" days" if unit == "days" else " s" if unit == "sec" else "")


if __name__ == "__main__":
    print(len(METRICS), "governed metrics;", len(OVERLAP_GROUPS), "overlap groups:", {k: len(v) for k, v in OVERLAP_GROUPS.items()})
