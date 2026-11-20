"""
LC-01 query compiler: a validated question (metric + dimensions + filters + months) -> one governed SELECT -> rows.

The LLM never writes SQL. It produces an intent; this module checks every part of it against the registry and the user's scope
and is the only place SQL text is assembled (from registry fragments + whitelisted column names; values are bound parameters).

    q = make_query(principal, "connect_rate", group_by=["agency"], m_from="2026-10", m_to="2026-10")
    res = run(principal, q)          # res.rows, res.sql, res.warnings

Numerators and denominators are summed across lenders and segments FIRST and divided LAST, so a total is never an average of
averages and every view of the same metric agrees with every other.
"""
import re
from dataclasses import dataclass, field

import lc01_access as A
import lc01_registry as R
from lc01_registry import MetricError


class UnknownValue(Exception):
    """A filter value that does not exist in the user's portfolio (e.g. a city the lender does not operate in)."""

    def __init__(self, dim, value, valid):
        super().__init__(f"no {dim} called {value!r} in your portfolio")
        self.dim, self.value, self.valid = dim, value, sorted(valid)


class SanityError(Exception):
    """The query ran but the result cannot be right (e.g. a rate above 100%)."""


@dataclass(frozen=True)
class Query:
    metric_id: str
    group_by: tuple = ()                 # dimension names (may include 'lender' for multi-lender users)
    filters: tuple = ()                  # ((dim, (values...)), ...)
    m_from: str = R.LATEST_MONTH
    m_to: str = R.LATEST_MONTH
    trend: bool = False                  # one row per month as well
    lenders: tuple = ()                  # lenders to read (always inside the user's scope)
    order: str = "desc"                  # 'desc' | 'asc' (for ranking by value)
    top_n: int | None = None


@dataclass
class Result:
    query: Query
    rows: list
    sql: str
    params: dict
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------- dimension values (what exists in the user's portfolio)
_STATIC = {"bucket": ["current", "1-30", "31-60", "61-90", "90+"], "channel": ["nach", "upi", "app", "cash", "cheque"],
           "disposition": ["ptp", "callback", "refuses", "dispute", "paid_already", "none"], "outcome": ["connected", "no_answer", "wrong_number"],
           "reason": ["wrong_amount", "paid_already", "harassment", "fraud", "other"]}
_CITY_ALIAS = {"bangalore": "Bengaluru", "bengaluru": "Bengaluru", "bombay": "Mumbai", "madras": "Chennai", "calcutta": "Kolkata", "new delhi": "Delhi"}
_cache: dict = {}


def dim_values(p: A.Principal, lenders=None) -> dict:
    """{dimension: sorted values} that exist for the lenders in scope (read through the tenant-safe layer)."""
    lenders = tuple(lenders or p.lenders)
    key = (p.user_id, lenders)
    if key not in _cache:
        out = {d: set(v) for d, v in _STATIC.items()}
        for d in ("city", "region", "product", "agency", "vintage"):
            out[d] = {r[d] for r in A.warehouse_query(p, f"SELECT DISTINCT {R.DIM_COL[d]} AS {d} FROM loans WHERE tenant_id = :tenant_id", None, lenders)}
        _cache[key] = {d: sorted(v) for d, v in out.items()}
    return _cache[key]


def clear_cache():
    _cache.clear()


def _norm(s: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(s).strip().lower())


def normalize_value(dim: str, value, valid: list):
    v = str(value).strip()
    low = v.lower()
    if dim == "city" and low in _CITY_ALIAS:
        v = _CITY_ALIAS[low]
    for cand in valid:                                              # exact, case-insensitive, space/underscore tolerant
        if cand.lower() == v.lower() or _norm(cand) == _norm(v):
            return cand
    if dim in ("agency", "city"):                                   # "Bluewave" -> "Bluewave Collections" when unique
        hits = [c for c in valid if low and (c.lower().startswith(low) or low in c.lower())]
        if len(hits) == 1:
            return hits[0]
    if dim == "bucket":
        b = re.sub(r"\s+", "", low).replace("dpd", "")
        alias = {"0": "current", "90plus": "90+", "90+": "90+", ">90": "90+", "91+": "90+", "30": "1-30", "60": "31-60", "90": "61-90", "bucket0": "current"}
        if b in alias and alias[b] in valid:
            return alias[b]
    raise UnknownValue(dim, value, valid)


# ---------------------------------------------------------------- validation
def make_query(p: A.Principal, metric_id, group_by=(), filters=None, m_from=None, m_to=None, trend=False, lenders=None, order="desc", top_n=None) -> Query:
    p = A.verify(p)
    m = R.get(metric_id)
    group_by, filters = list(group_by or []), dict(filters or {})
    if len(set(group_by)) != len(group_by):
        raise MetricError("a dimension can only be used once")
    allowed = set(m.dims) | {"lender"}
    for d in group_by + list(filters):
        if d not in allowed:
            raise MetricError(f"{m.name} cannot be split or filtered by {d!r}; it can use: {', '.join(m.dims)}")
    if len(group_by) > 2 or (trend and len(group_by) > 1):
        raise MetricError("too many dimensions: at most 2 (1 with a monthly trend)")
    lend = list(lenders or [])
    if "lender" in filters:                                         # a lender filter can only narrow the scope, and only inside it
        want = filters.pop("lender")
        want = [want] if isinstance(want, str) else list(want)
        lend = [w.upper() for w in want]
    lend = A.require_lenders(p, lend)                               # AccessError if outside the scope
    if "lender" in group_by and len(lend) < 2:
        raise A.AccessError("splitting by lender needs more than one lender in your scope")
    m_from, m_to = m_from or R.LATEST_MONTH, m_to or m_from or R.LATEST_MONTH
    if not (R.month_ok(m_from) and R.month_ok(m_to)):
        raise MetricError(f"data covers {R.MONTHS[0]} to {R.MONTHS[-1]}")
    if m_from > m_to:
        m_from, m_to = m_to, m_from
    vals = dim_values(p, lend)
    fl = []
    for d, v in filters.items():
        v = [v] if isinstance(v, (str, int)) else list(v)
        fl.append((d, tuple(normalize_value(d, x, vals[d]) for x in v)))
    return Query(metric_id, tuple(group_by), tuple(sorted(fl)), m_from, m_to, bool(trend), tuple(lend), order if order in ("asc", "desc") else "desc",
                 int(top_n) if top_n else None)


# ---------------------------------------------------------------- SQL
def compile_sql(q: Query) -> tuple[str, dict]:
    m = R.get(q.metric_id)
    sql_dims = [d for d in q.group_by if d != "lender"]
    cols = [R.DIM_COL[d] for d in sql_dims] + (["month"] if q.trend else [])
    sel = ", ".join([f"{c} AS {c}" for c in cols] + [f"{m.num} AS num", f"{m.den or 'NULL'} AS den"])
    where, params = ["tenant_id = :tenant_id", "month >= :m_from", "month <= :m_to"], {"m_from": q.m_from, "m_to": q.m_to}
    for d, vals in q.filters:
        ph = []
        for i, v in enumerate(vals):
            params[f"f_{d}_{i}"] = v
            ph.append(f":f_{d}_{i}")
        where.append(f"{R.DIM_COL[d]} IN ({', '.join(ph)})")
    sql = f"SELECT {sel} FROM {m.fact} WHERE {' AND '.join(where)}"
    if cols:
        sql += f" GROUP BY {', '.join(cols)} ORDER BY {', '.join(cols)}"
    return sql, params


def run(p: A.Principal, q: Query) -> Result:
    m = R.get(q.metric_id)
    sql, params = compile_sql(q)
    raw = A.warehouse_query(p, sql, params, q.lenders)              # checked, tenant-scoped, one lender at a time
    acc: dict = {}
    for r in raw:
        key = tuple(r[R.DIM_COL[d]] if d != "lender" else r["_lender"] for d in q.group_by) + ((r["month"],) if q.trend else ())
        a = acc.setdefault(key, [0.0, 0.0, False])
        a[0] += r["num"] or 0
        a[1] += r["den"] or 0
        a[2] = a[2] or r["den"] is not None
    rows = []
    for key, (num, den, has_den) in acc.items():
        row = {d: key[i] for i, d in enumerate(q.group_by)}
        if q.trend:
            row["month"] = key[-1]
        row.update(value=(num / den if den else None) if m.den else num, num=num, den=den if m.den else None)
        rows.append(row)
    if q.trend:
        rows.sort(key=lambda r: (tuple(str(r.get(d, "")) for d in q.group_by), r["month"]))
    elif q.group_by:
        rows.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0) if q.order == "desc" else (r["value"] or 0)))
        if q.top_n:
            rows = rows[: q.top_n]
    return Result(q, rows, sql, params, sanity(m, rows))


def sanity(m: R.Metric, rows: list) -> list:
    """Cheap checks on a result. Warnings are shown to the user; an impossible number raises SanityError."""
    warn = []
    if not rows:
        return ["The query returned no rows."]
    for r in rows:
        v = r.get("value")
        if v is None:
            continue
        if v != v or v in (float("inf"), float("-inf")):
            raise SanityError(f"{m.name} came out as {v}")
        if m.unit == "pct" and not (-1e-9 <= v <= 1 + 1e-9):
            raise SanityError(f"{m.name} came out as {v * 100:.1f}%, which is impossible for a rate")
        if m.unit in ("count", "rs", "days", "sec", "score") and v < 0:
            raise SanityError(f"{m.name} came out negative")
    small = [r for r in rows if m.den and r.get("den") is not None and 0 < r["den"] < 30]
    if small:
        warn.append(f"{len(small)} of {len(rows)} figure(s) rest on fewer than 30 records, so treat them with care.")
    if m.den and any(r.get("value") is None for r in rows):
        warn.append("Some segments have no records for this measure (shown as n/a).")
    return warn


if __name__ == "__main__":
    p = A.login("u_l1")
    q = make_query(p, "connect_rate", group_by=["agency"])
    r = run(p, q)
    print(r.sql)
    for row in r.rows:
        print(row)
