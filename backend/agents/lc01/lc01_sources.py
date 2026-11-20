"""
LC-01 extra sources: questions about the OTHER modules' data (BI-02 portfolio and non-payment reasons, BI-03 early warning,
LC-04 command center), answered from ask_all.db, the read-only copy made by data/sync_all.py.

Same rules as the lender warehouse, kept separate so the 38 governed lender metrics and their evaluation are untouched:
  - the question is routed to a metric by code (aliases and value words, English and Hinglish); no model, no tokens
  - every statement is assembled here from fixed fragments (user text only ever becomes a bound parameter), then passed through
    lc01_access.check_sql with the table whitelist of that source, and run through db.py with the tenant id bound by the data layer
  - who may read which source is decided by the USER RECORD (role and scope), not by the question:
        bi02 / bi03  ->  tenant abc_bank   (role lender_user with scope abc_bank)
        lc04         ->  tenants GLOBAL, L1..L5   (role cn_leader)
    anyone else gets "not in your access" (an explicit mention) or falls through to the lender warehouse (a generic word)
  - the answer shows the SQL, the source module and when the copy was made
"""
import re
import time
from dataclasses import dataclass

import lc01_access as A
import lc01_intent as I
import lc01_registry as R

DB = "ask"
LC04_TENANTS = ("GLOBAL", "L1", "L2", "L3", "L4", "L5")
SOURCES = {
    "bi02": dict(name="BI-02 portfolio and non-payment reasons", module="BI-02"),
    "bi03": dict(name="BI-03 early warning", module="BI-03"),
    "lc04": dict(name="LC-04 command center", module="LC-04"),
}
TABLES = {
    "bi02": {"bi02_accounts", "bi02_assessments", "bi02_recommendations", "bi02_promises"},
    "bi03": {"bi03_flags", "bi03_flag_outcomes", "bi03_nudges", "bi03_bp_accounts"},
    "lc04": {"lc04_anomalies", "lc04_alerts", "lc04_lc_accounts", "lc04_dashboards"},
}
_LATEST_ASSESSMENT = "assessment_id IN (SELECT MAX(assessment_id) FROM bi02_assessments WHERE tenant_id = :tenant_id GROUP BY account_id)"

# words that name one value of a dimension (English + Hinglish), on top of the value's own text
SYN = {
    ("reason", "forgot"): ["forgot", "forgotten", "forget", "forgetting", "bhool", "bhul", "bhula", "yaad nahi", "yaad nai"],
    ("reason", "cant_pay"): ["can't pay", "cant pay", "cannot pay", "unable to pay", "income cut", "salary cut", "job loss", "afford"],
    ("reason", "wont_pay"): ["won't pay", "wont pay", "will not pay", "unwilling", "refuse to pay", "refusing", "wilful", "willful"],
    ("reason", "insufficient"): ["insufficient", "low balance", "no balance", "balance low", "shortfall"],
    ("reason", "disputed"): ["disputed", "dispute", "disputes", "disagree"],
    ("status", "pending"): ["pending", "to approve", "awaiting approval", "waiting for approval"],
    ("status", "approved"): ["approved"],
    ("channel", "telecall"): ["telecall", "tele call", "phone call", "call"],
    ("channel", "dispute_team"): ["dispute team"],
    ("channel", "field_visit"): ["field visit", "field visits"],
    ("signal", "salary_delay"): ["salary delay", "salary delays", "delayed salary"],
    ("signal", "card_utilisation"): ["card utilisation", "card utilization", "credit card"],
    ("signal", "other_loan_bounce"): ["other loan bounce", "other loan bounces"],
    ("signal", "late_emi_payments"): ["late emi", "late emis", "late emi payments"],
    ("signal", "new_enquiries"): ["new enquiries", "enquiries", "inquiries"],
    ("signal", "emi_burden"): ["emi burden"],
}
LABELS = {("reason", "forgot"): "Forgot", ("reason", "cant_pay"): "Cannot pay", ("reason", "wont_pay"): "Will not pay",
          ("reason", "insufficient"): "Insufficient balance", ("reason", "disputed"): "Disputed", ("lender", "GLOBAL"): "All lenders (global)",
          ("audience", "cn_leadership"): "CN leadership", ("audience", "lender_rm"): "Lender RM", ("audience", "lender_user"): "Lender user",
          ("audience", "ops_head"): "Ops head", ("audience", "delivery_head"): "Delivery head", ("role", "cn_leadership"): "CN leadership",
          ("role", "lender_rm"): "Lender RM", ("role", "lender_user"): "Lender user", ("role", "ops_head"): "Ops head", ("role", "delivery_head"): "Delivery head"}
STATIC_VALUES = {"reason": ["forgot", "cant_pay", "wont_pay", "insufficient", "disputed"],
                 "status": None, "channel": None, "signal": None, "bucket": None, "product": None, "branch": None, "city": None,
                 "type": None, "metric": None, "audience": None, "role": None, "source": None}
DIM_WORDS = {"reason": ["reason", "reasons", "kaaran", "wajah"], "bucket": ["bucket", "buckets"], "product": ["product", "products", "loan type"],
             "branch": ["branch", "branches", "city", "cities"], "city": ["city", "cities"], "channel": ["channel", "channels"],
             "status": ["status"], "signal": ["signal", "signals"], "type": ["nudge type", "type"], "metric": ["metric", "metrics"],
             "audience": ["audience", "role"], "role": ["role", "persona"], "lender": ["lender", "lenders"], "source": ["source"]}


@dataclass(frozen=True)
class XMetric:
    metric_id: str
    name: str
    source: str
    table: str
    num: str
    den: str | None
    unit: str
    noun: str
    definition: str
    aliases: tuple
    dims: tuple                       # ((dimension, column or None for the tenant), ...)
    base: str = ""
    default_group: tuple = ()
    boost: int = 0

    def col(self, d):
        return dict(self.dims)[d]


def _x(mid, name, source, table, num, den, unit, noun, definition, aliases, dims=(), base="", default_group=(), boost=0):
    return XMetric(mid, name, source, table, num, den, unit, noun, definition, tuple(a.lower() for a in aliases), tuple(dims), base, tuple(default_group), boost)


XMETRICS = {m.metric_id: m for m in [
    # ---- BI-02: the 300 delinquent accounts and why they are not paying ----
    _x("bi02_accounts_by_reason", "Accounts by non-payment reason (BI-02)", "bi02", "bi02_assessments", "COUNT(*)", None, "count", "assessed accounts",
       "Accounts the BI-02 agent has assessed, by the reason it found for non-payment (forgot, cannot pay, will not pay, insufficient balance, disputed). Each account counted once, by its latest assessment.",
       ("not paying", "not pay", "not paid", "non payment", "non-payment", "nonpayment", "paise nahi", "payment nahi", "nahi de rahe", "nahi bhar", "unpaid", "defaulting",
        "assessed accounts", "reason for non", "why are accounts", "accounts by reason", "kitne accounts", "reasons"), (("reason", "label"),), _LATEST_ASSESSMENT, ("reason",), 20),
    _x("bi02_recommendations", "Recommended actions (BI-02)", "bi02", "bi02_recommendations", "COUNT(*)", None, "count", "recommended actions",
       "Actions the BI-02 agent recommended for assessed accounts, by channel and approval status.",
       ("recommended actions", "recommendations", "recommended action", "pending approval", "to approve", "awaiting approval", "approved actions", "next actions", "actions pending"),
       (("channel", "channel"), ("status", "status")), "", (), 10),
    _x("bi02_promise_kept_rate", "Promise kept rate (BI-02)", "bi02", "bi02_promises", "SUM(kept)", "COUNT(*)", "pct", "promises",
       "Promises to pay that were kept / all promises to pay recorded for the BI-02 accounts.",
       ("promise kept rate", "promises kept", "kept promises", "promise keep rate", "promises were kept", "promise to pay kept", "ptp kept"), (), "", (), 10),
    _x("bi02_account_count", "Delinquent accounts (BI-02)", "bi02", "bi02_accounts", "COUNT(*)", None, "count", "delinquent accounts",
       "Accounts in the BI-02 portfolio, all of them delinquent (missed an EMI), by DPD bucket, product or branch.",
       ("how many accounts", "number of accounts", "total accounts", "delinquent accounts", "accounts in bucket", "accounts by bucket", "bucket wise", "accounts by product", "accounts by branch"),
       (("bucket", "bucket"), ("product", "product"), ("branch", "branch")), "", (), 0),
    _x("bi02_outstanding", "Outstanding amount (BI-02)", "bi02", "bi02_accounts", "SUM(outstanding)", None, "rs", "outstanding",
       "Outstanding principal of the BI-02 delinquent accounts, by DPD bucket, product or branch.",
       ("outstanding amount", "outstanding", "exposure at risk", "money at risk", "overdue amount"), (("bucket", "bucket"), ("product", "product"), ("branch", "branch")), "", (), 5),
    # ---- BI-03: behavioural early warning on 6,003 accounts ----
    _x("bi03_flags", "Early-warning flags (BI-03)", "bi03", "bi03_flags", "COUNT(*)", None, "count", "early-warning flags",
       "Accounts BI-03 flagged as likely to miss their next EMI, by the signal that drove the flag and by status (nudged or closed).",
       ("early warning flags", "early-warning flags", "early warning", "early-warning", "flagged accounts", "accounts flagged", "flags", "flagged", "warning signals", "kitne flag"),
       (("signal", "dominant_signal"), ("status", "status")), "", (), 15),
    _x("bi03_flag_hit_rate", "Flag hit rate (BI-03)", "bi03", "bi03_flag_outcomes", "SUM(COALESCE(went_dpd1, 0))", "COUNT(*)", "pct", "evaluated flags",
       "Of the flags whose outcome has been checked, the share where the account really went late (DPD 1 or more) afterwards.",
       ("flag hit rate", "hit rate", "flag precision", "precision", "flag accuracy", "flags that went late", "flags went late", "flags that went dpd", "flags turned late", "how accurate"),
       (), "", (), 25),
    _x("bi03_lead_time", "Early-warning lead time (BI-03)", "bi03", "bi03_flag_outcomes",
       "SUM(CASE WHEN went_dpd1 = 1 THEN lead_time_days ELSE 0 END)", "SUM(CASE WHEN went_dpd1 = 1 THEN 1 ELSE 0 END)", "days", "days",
       "Average number of days between the flag and the account actually going late, for flags that turned out right.",
       ("lead time", "how early", "warning lead", "early warning lead time", "days before"), (), "", (), 25),
    _x("bi03_nudges", "Nudges (BI-03)", "bi03", "bi03_nudges", "COUNT(*)", None, "count", "nudges",
       "Customer nudges BI-03 prepared for flagged accounts, by nudge type and status.",
       ("nudges", "nudge", "nudged customers", "customer messages"), (("type", "nudge_type"), ("status", "status")), "", (), 15),
    _x("bi03_watched_accounts", "Accounts watched by early warning (BI-03)", "bi03", "bi03_bp_accounts", "COUNT(*)", None, "count", "accounts",
       "Accounts in the BI-03 early-warning book, by DPD bucket, product or branch.",
       ("accounts being watched", "being watched", "are watched", "watched accounts", "monitored accounts", "accounts monitored", "being monitored", "accounts under watch", "early warning book", "watchlist"),
       (("bucket", "bucket"), ("product", "product"), ("branch", "branch")), "", (), 15),
    # ---- LC-04: command center (5 lenders) ----
    _x("lc04_anomalies", "Anomalies detected (LC-04)", "lc04", "lc04_anomalies", "COUNT(*)", None, "count", "anomalies",
       "Anomalies the command center detected in the intraday metrics, by metric, status and lender.",
       ("anomalies", "anomaly", "incidents", "incident", "command center anomalies"), (("metric", "metric_id"), ("status", "status"), ("lender", None)), "", (), 15),
    _x("lc04_alerts", "Alerts sent (LC-04)", "lc04", "lc04_alerts", "COUNT(*)", None, "count", "alerts",
       "Alerts the command center sent for those anomalies, by audience role, status and lender.",
       ("alerts", "alert", "alerts sent", "notifications sent"), (("audience", "audience_role"), ("status", "status"), ("lender", None)), "", (), 10),
    _x("lc04_accounts", "Accounts in the command center (LC-04)", "lc04", "lc04_lc_accounts", "COUNT(*)", None, "count", "accounts",
       "Accounts of the five command-center lenders, by DPD bucket, product, city or lender.",
       ("command center accounts", "command-center accounts", "lc-04 accounts", "accounts in the command center", "command centre accounts"),
       (("bucket", "bucket"), ("product", "product"), ("city", "city"), ("lender", None)), "", (), 20),
    _x("lc04_outstanding", "Outstanding in the command center (LC-04)", "lc04", "lc04_lc_accounts", "SUM(outstanding)", None, "rs", "outstanding",
       "Outstanding principal of the five command-center lenders, by DPD bucket, product, city or lender.",
       ("command center outstanding", "command-center outstanding", "lc-04 outstanding", "outstanding in the command center"),
       (("bucket", "bucket"), ("product", "product"), ("city", "city"), ("lender", None)), "", (), 25),
    _x("lc04_dashboards", "Dashboards generated (LC-04)", "lc04", "lc04_dashboards", "COUNT(*)", None, "count", "dashboards",
       "Role dashboards the command center generated, by role and by how they were made.",
       ("dashboards generated", "generated dashboards", "dashboards", "dashboard"), (("role", "role_id"), ("source", "source")), "", (), 15),
]}

_FORGOT = re.compile(r"(?<![a-z0-9])(forgot|forgotten|forget|forgetting|bhool\w*|bhul\w*|yaad nahi|yaad nai)(?![a-z0-9])")
# words that say "the other module's data" for a user who also has the lender warehouse, where the same everyday word exists in both
_OWN = re.compile(r"\bbi-?0[23]\b|\bbi0[23]\b|\babc bank\b|\bassessed\b|\bdelinquent accounts?\b|\bearly[- ]warning\b|\bcommand[- ]cent(er|re)\b|\blc-?04\b|\bnon[- ]?payment reasons?\b|\bnot paying\b|\bpaise nahi\b")
# an explicit mention of a module: a user without access gets "not in your access" instead of a silent fall-through
_MENTION = {"bi03": re.compile(r"early[- ]warning|\bbi-?03\b"), "lc04": re.compile(r"command[- ]cent(er|re)|\blc-?04\b"),
            "bi02": re.compile(r"\bbi-?02\b|non[- ]?payment reasons?")}


# ---------------------------------------------------------------- access
def source_tenants(p: A.Principal, source: str) -> tuple:
    """Tenants of ask_all.db this user may read in a source. Decided from the verified user record only."""
    p = A.verify(p)
    if source in ("bi02", "bi03"):
        if p.role == "collections_head":                              # Neha Kapoor: the one demo user who sees every module's data
            return ("abc_bank",)
        return ("abc_bank",) if p.role == "lender_user" and p.lenders == ("abc_bank",) else ()
    if source == "lc04":
        return LC04_TENANTS if p.role in ("cn_leader", "collections_head") else ()
    return ()


def sources_for(p: A.Principal) -> list:
    return [s for s in SOURCES if source_tenants(p, s)]


def has_lender_warehouse(p: A.Principal) -> bool:
    return set(p.lenders) <= set(R.LENDER_IDS)


def available() -> bool:
    return A.db._paths(DB)[0].exists()


def synced_at() -> str | None:
    try:
        rows = A.db.query("GLOBAL", "SELECT value FROM ask_meta WHERE tenant_id = :tenant_id AND key = 'synced_at'", None, DB)
        return rows[0]["value"] if rows else None
    except Exception:
        return None


# ---------------------------------------------------------------- the only place SQL is assembled and run
def _run(p: A.Principal, source: str, sql: str, params: dict) -> list:
    p = A.verify(p)
    tenants = source_tenants(p, source)
    if not tenants:
        raise A.AccessError(f"{SOURCES[source]['module']} data is outside your access")
    sql = A.check_sql(sql, TABLES[source])
    rows = []
    for t in tenants:
        for r in A.db.query(t, sql, params, DB):
            r["_tenant"] = t
            rows.append(r)
    return rows


_vals: dict = {}


def valid_values(p: A.Principal, m: XMetric, dim: str) -> list:
    col = m.col(dim)
    if dim == "lender":
        return list(source_tenants(p, m.source))
    if STATIC_VALUES.get(dim):
        return list(STATIC_VALUES[dim])
    key = (p.user_id, m.table, col)
    if key not in _vals:
        rows = _run(p, m.source, f"SELECT DISTINCT {col} AS v FROM {m.table} WHERE tenant_id = :tenant_id", {})
        _vals[key] = sorted({str(r["v"]) for r in rows if r["v"] is not None})
    return _vals[key]


def _pat(s: str):
    return re.compile(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])")


def _phrases(dim: str, value: str) -> list:
    v = str(value).lower()
    ph = {v, v.replace("_", " "), v.replace("_", "-")} | {s.lower() for s in SYN.get((dim, value), [])}
    return [x for x in ph if len(x) > 1]


def display(dim: str, value) -> str:
    v = str(value)
    if (dim, v) in LABELS:
        return LABELS[(dim, v)]
    return v if dim in ("lender", "bucket", "product", "branch", "city", "metric") else v.replace("_", " ").capitalize()


# ---------------------------------------------------------------- routing and parsing (code, no model)
def score(text: str, m: XMetric) -> int:
    hits = [a for a in m.aliases if _pat(a).search(text)]
    if not hits:
        return 0
    return sum(len(a) for a in hits) + m.boost


def route(p: A.Principal, text: str):
    """(metric, forced) best metric for the question among the sources the user can read, or (None, mention_source_without_access)."""
    best, best_s = None, 0
    reason_hit = any(_pat(ph).search(text) for v in STATIC_VALUES["reason"] for ph in _phrases("reason", v))
    can = {s: bool(source_tenants(p, s)) for s in SOURCES}
    for m in XMETRICS.values():
        if not can[m.source]:
            continue
        s = score(text, m)
        if m.metric_id == "bi02_accounts_by_reason" and reason_hit:
            s += 30                                                   # "how many accounts forgot" names a value of this metric's own dimension
        if m.metric_id == "bi03_flags" and any(_pat(ph).search(text) for v in ("salary_delay", "card_utilisation", "other_loan_bounce", "late_emi_payments", "new_enquiries") for ph in _phrases("signal", v)):
            s += 30
        if s > best_s:
            best, best_s = m, s
    if best:
        return best, None
    for src, rx in _MENTION.items():
        if rx.search(text) and not can[src]:
            return None, src
    return None, None


def parse(p: A.Principal, m: XMetric, text: str):
    """(group_by list, filters dict) read from the question; values must exist in the user's data, dimensions must belong to the metric."""
    dims = [d for d, _ in m.dims]
    filters, group = {}, []
    for d in dims:
        found = [v for v in valid_values(p, m, d) if any(_pat(ph).search(text) for ph in _phrases(d, v))]
        if found:
            filters[d] = found
    for d in dims:
        words = DIM_WORDS.get(d, [d])
        rx = "|".join(re.escape(w) for w in words)
        if re.search(rf"(?:\bby|\bper|\beach|\bevery|\bacross|\bwhich|\bsplit by|\bbreakdown by|\bgrouped by)\s+(?:the\s+)?(?:{rx})\b|(?:{rx})\s*-?\s*wise\b|\bkaun\s?(?:sa|si|se)\s+(?:{rx})\b", text):
            group.append(d)
    if not group and not filters:
        group = list(m.default_group)
    elif not group and m.default_group and len(m.default_group) == 1 and m.default_group[0] not in filters:
        group = list(m.default_group)
    group = [d for d in group if d not in filters or len(filters[d]) > 1][:2]
    return group, filters


def build_sql(m: XMetric, group: list, filters: dict):
    cols = [(d, m.col(d)) for d in group if m.col(d)]
    sel = [f"{c} AS d_{d}" for d, c in cols] + [f"{m.num} AS num", f"{m.den or 'NULL'} AS den", "COUNT(*) AS n_rows"]
    where, params = ["tenant_id = :tenant_id"], {}
    if m.base:
        where.append(m.base)
    for d, vals in filters.items():
        col = m.col(d)
        if not col:
            continue
        ph = []
        for i, v in enumerate(vals):
            params[f"f_{d}_{i}"] = v
            ph.append(f":f_{d}_{i}")
        where.append(f"{col} IN ({', '.join(ph)})")
    sql = f"SELECT {', '.join(sel)} FROM {m.table} WHERE {' AND '.join(where)}"
    if cols:
        sql += " GROUP BY " + ", ".join(c for _, c in cols) + " ORDER BY " + ", ".join(c for _, c in cols)
    return sql, params


def _num(v):
    return float(v or 0)


def _fmt(v, unit):
    return R.fmt_value(v, unit)


def examples(p: A.Principal) -> list:
    out = []
    if "bi02" in sources_for(p):
        out += [("How many accounts have not paid because they forgot?", "value"), ("Kitne accounts paise nahi de rahe, reason ke hisaab se?", "value"),
                ("How many recommended actions are pending approval?", "value"), ("What is the BI-02 promise kept rate?", "value"),
                ("BI-02 outstanding amount by bucket", "rank")]
    if "bi03" in sources_for(p):
        out += [("How many early-warning flags do we have, by signal?", "rank"), ("What is the flag hit rate?", "value"), ("What is the early warning lead time?", "value")]
    if "lc04" in sources_for(p):
        out += [("How many anomalies has the command center detected, by metric?", "rank"), ("How many alerts were sent by audience?", "rank"),
                ("Outstanding in the command center by lender", "rank")]
    return [dict(id=f"x{i}", text=t, kind=k) for i, (t, k) in enumerate(out)]


def try_answer(p: A.Principal, question: str):
    """None = not about another module's data (the normal lender-warehouse flow continues); otherwise a response dict."""
    p = A.verify(p)
    text = I.norm_text(question)
    try:
        I.precheck(p, question)                                      # the same code-level walls (injection, raw SQL, writes, other lenders)
    except I.Refusal:
        return None                                                  # the normal flow raises it again and logs it
    m, denied = route(p, text)
    if denied:
        mod = SOURCES[denied]["module"]
        return dict(status="refused", refusal=dict(category="out_of_scope", reason=f"{SOURCES[denied]['name']} data is not part of your access."),
                    headline=f"{SOURCES[denied]['name']} data is not part of your access, so I cannot answer that for this user.", source="xsource")
    if m is None:
        if _FORGOT.search(text) and not source_tenants(p, "bi02"):   # a reason that exists only in BI-02: say so instead of guessing another measure
            return dict(status="unsupported", source="xsource", headline="Non-payment reasons (forgot, cannot pay, will not pay ...) are BI-02 data, which is not part of your access or of your lender warehouse.")
        return None
    if has_lender_warehouse(p) and not _OWN.search(text):             # a user who has both datasets: a word the lender warehouse also defines stays with the lender warehouse
        spec, gen = I.find_metrics(text)
        if spec or gen:
            return None
    t0 = time.time()
    if not available():
        return dict(status="error", headline="The data copy for the other modules (ask_all.db) has not been built yet. Run: python backend/data/sync_all.py", source="xsource")
    group, filters = parse(p, m, text)
    sql, params = build_sql(m, group, filters)
    rows = _run(p, m.source, sql, params)
    steps = [dict(tool="metric_definitions", detail=f"{len(XMETRICS)} extra metrics over BI-02, BI-03 and LC-04 data; question read by code", ms=0),
             dict(tool="warehouse_sql", detail=f"{m.name}" + (f" by {', '.join(group)}" if group else ""), ms=int((time.time() - t0) * 1000), rows=len(rows))]
    acc: dict = {}
    for r in rows:
        if not r["n_rows"]:                                          # a tenant with no rows at all (an aggregate over nothing) is not a segment
            continue
        key = tuple((r["_tenant"] if m.col(d) is None else r[f"d_{d}"]) for d in group)
        a = acc.setdefault(key, [0.0, 0.0])
        a[0] += _num(r["num"])
        a[1] += _num(r["den"])
    out_rows = []
    for key, (n, d) in acc.items():
        val = (n / d if d else None) if m.den else n
        out_rows.append(dict({g: key[i] for i, g in enumerate(group)}, value=val, num=n, den=d if m.den else None))
    out_rows.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0)))
    tot_n = sum(r["num"] for r in out_rows)
    tot_d = sum(r["den"] or 0 for r in out_rows) if m.den else None
    total = (tot_n / tot_d if tot_d else None) if m.den else tot_n
    flt = "" if not filters else " (" + ", ".join(f"{d} = {' / '.join(display(d, v) for v in vs)}" for d, vs in filters.items()) + ")"
    mod = SOURCES[m.source]["module"]
    if not out_rows or (m.den and not tot_d):
        headline, table, chart = f"There is no {m.name.lower()} data{flt} in the {mod} data.", None, None
        status = "no_data"
    else:
        status = "answered"
        if m.den:
            headline = f"{_fmt(total, m.unit)}{' (' + format(tot_n, ',.0f') + ' of ' + format(tot_d, ',.0f') + ' ' + m.noun + ')' if m.unit == 'pct' else ''}{flt}"
        else:
            headline = f"{_fmt(total, m.unit)} {m.noun}{flt}"
        if group:
            top = ", ".join(f"{' · '.join(display(g, r[g]) for g in group)} {_fmt(r['value'], m.unit)}" for r in out_rows[:4])
            headline += f". By {' and '.join(group)}: {top}" + (" ..." if len(out_rows) > 4 else "")
        label = lambda r: " · ".join(display(g, r[g]) for g in group)  # noqa: E731
        table = dict(columns=[*group, m.name] + (["numerator", "denominator"] if m.den else []),
                     rows=[[display(g, r[g]) for g in group] + [_fmt(r["value"], m.unit)] + ([f"{r['num']:,.0f}", f"{r['den']:,.0f}"] if m.den else []) for r in out_rows]) if group else None
        chart = dict(kind="bar", title=f"{m.name} by {' and '.join(group)}", unit=m.unit, x_label=" · ".join(group),
                     series=[dict(name=m.name, points=[{"x": label(r), "y": r["value"]} for r in out_rows[:15] if r["value"] is not None])], highlight=[]) if group else None
    notes = [f"Source: {SOURCES[m.source]['name']}, read from a copy made {synced_at() or 'at an unknown time'} (rebuild with sync_all.py after the module's data moves)."]
    if m.metric_id == "bi02_accounts_by_reason":
        notes.append("Only accounts the BI-02 agent has assessed are counted (not all 300 delinquent accounts), each once, by its latest assessment.")
    if m.source == "lc04":
        notes.append("Command-center lender ids (L1 to L5, and GLOBAL for network-wide rows) belong to the LC-04 dataset; they are not the three lenders of the lender warehouse.")
    if re.search(r"\bwhy\b|\bkyun\b|\broot cause\b|\bexplain\b", text):
        notes.append("A root-cause explanation is built only for the 38 lender-warehouse metrics; here you get the breakdown behind the number.")
    if re.search(r"\bwhich accounts\b|\blist\b|\baccount ids?\b|\bnames?\b", text):
        notes.append("I answer with counts and breakdowns; the account-level list is on the module's own screen (for example BI-02 Approve).")
    return dict(status=status, headline=headline, source="xsource", table=table, chart=chart, explanation=None, data=out_rows, notes=notes,
                metric=dict(id=m.metric_id, name=m.name, unit=m.unit, definition=m.definition, family=m.source, overlap=None),
                scope=dict(lenders=list(source_tenants(p, m.source)), label=SOURCES[m.source]["name"], filters={d: list(v) for d, v in filters.items()},
                           group_by=group, trend=False),
                data_source=dict(module=mod, name=SOURCES[m.source]["name"], synced_at=synced_at()),
                sql=[dict(label=m.name + (f" by {', '.join(group)}" if group else ""), sql=sql, params=params, lenders=list(source_tenants(p, m.source)), rows=len(rows))],
                steps=steps + ([dict(tool="chart_generator", detail="bar", ms=0)] if chart else []))


def catalog() -> list:
    return [dict(metric_id=m.metric_id, name=m.name, source=m.source, module=SOURCES[m.source]["module"], unit=m.unit, definition=m.definition,
                 dims=[d for d, _ in m.dims], table=m.table, formula=(f"{m.num} / {m.den}" if m.den else m.num)) for m in XMETRICS.values()]
