"""
LC-04: THE one way to get a metric number. Dashboards, detector, drill-down, alerts and tests all call compute().

    compute("pay_success_rate", Scope(("L1", "L2")), "2026-10-15 10:00:00", "2026-10-15 14:00:00",
            group_by=["gateway"], grain="hour", filters={"city": "Pune"})

- It takes a governed metric id, never a formula: there is no way to pass an ad hoc definition.
- The metric's allowed dimensions are enforced (group_by and filters); unknown metric or dimension raises MetricError.
- Every SQL statement is built only from the registry and a whitelist of column names; values travel as parameters.
- It reads the 15-minute cube, one lender (tenant) at a time through db.py, so tenant isolation is the data layer's.
  Numerators and denominators are summed ACROSS lenders/segments first and divided last, so a total is never an
  average of ratios and every view agrees with every other view.
- Window = buckets with start <= bucket_ts < end (bucket = arrival time, see cube.py).
Returns rows: {<group dims>, time (if grain), value, numerator, denominator}; value is None when the denominator is 0.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
from registry import DIM_COL, MetricError, UNITS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

DB = "lc04"
FMT = "%Y-%m-%d %H:%M:%S"
GRAINS = {"bucket": "bucket_ts", "hour": "substr(bucket_ts,1,13) || ':00:00'", "day": "day"}
LENDERS = ("L1", "L2", "L3", "L4", "L5")


@dataclass(frozen=True)
class Scope:
    """Which lenders (tenants) a call may read. Step 5 builds it from the logged-in role, never from a prompt."""
    lenders: tuple

    def __post_init__(self):
        if not self.lenders or not all(isinstance(x, str) and x for x in self.lenders):
            raise MetricError("a scope needs at least one lender")
        object.__setattr__(self, "lenders", tuple(sorted(set(self.lenders))))


def _sum_sql(terms):
    out = ""
    for i, (col, sign) in enumerate(terms):
        out += (" - " if sign < 0 else (" + " if i else "")) + f"SUM({col})"
    return out


def _ts(x):
    return pd.Timestamp(x).strftime(FMT)


def compute(metric_id, scope, start, end, group_by=(), grain=None, filters=None, database=DB):
    m = registry.get(metric_id)
    allowed = set(m.allowed_dims)
    group_by = list(group_by or [])
    filters = dict(filters or {})
    for d in group_by + list(filters):
        if d not in DIM_COL:
            raise MetricError(f"{d!r} is not a governed dimension")
        if d not in allowed:
            raise MetricError(f"{metric_id} cannot be sliced by {d!r} (allowed: {', '.join(m.allowed_dims)})")
    if len(set(group_by)) != len(group_by):
        raise MetricError("duplicate group_by dimension")
    if grain is not None and grain not in GRAINS:
        raise MetricError(f"unknown grain {grain!r}")
    if not isinstance(scope, Scope):
        raise MetricError("scope must be a Scope")
    s, e = _ts(start), _ts(end)
    if s >= e:
        raise MetricError("empty window")

    lenders = list(scope.lenders)
    if "lender" in filters:                                    # a lender filter can only narrow the scope
        want = filters.pop("lender")
        want = [want] if isinstance(want, str) else list(want)
        lenders = [x for x in lenders if x in want]
    cols = [f"{DIM_COL[d]} AS {d}" for d in group_by if d != "lender"]
    if grain:
        cols.append(f"{GRAINS[grain]} AS time")
    where, params = ["tenant_id = :tenant_id", "bucket_ts >= :s", "bucket_ts < :e"], {"s": s, "e": e}
    for i, (d, v) in enumerate(filters.items()):
        vals = [v] if isinstance(v, str) else list(v)
        names = []
        for j, x in enumerate(vals):
            params[f"f{i}_{j}"] = str(x)
            names.append(f":f{i}_{j}")
        where.append(f"{DIM_COL[d]} IN ({', '.join(names)})")
    den = _sum_sql(m.den_terms) if m.den_terms else "0"
    group_sql = (" GROUP BY " + ", ".join(str(i + 1) for i in range(len(cols)))) if cols else ""
    sql = (f"SELECT {', '.join(cols + [f'({_sum_sql(m.num_terms)}) AS n', f'({den}) AS d'])} FROM cube_15m "
           f"WHERE {' AND '.join(where)}{group_sql}")

    acc = {}
    for t in lenders:
        for r in db.query(t, sql, params, database=database):
            key = tuple(r[d] for d in group_by if d != "lender") + ((r["time"],) if grain else ())
            if "lender" in group_by:
                key = (t,) + key
            n, d_ = acc.get(key, (0.0, 0.0))
            acc[key] = (n + (r["n"] or 0), d_ + (r["d"] or 0))
    if not acc and not group_by and not grain:
        acc[()] = (0.0, 0.0)
    names = (["lender"] if "lender" in group_by else []) + [d for d in group_by if d != "lender"] + (["time"] if grain else [])
    mult = UNITS[m.unit]
    rows = []
    for key in sorted(acc, key=lambda k: tuple(str(x) for x in k)):
        n, d_ = acc[key]
        if m.denominator:
            value = None if d_ == 0 else n * mult / d_
        else:
            value = n
        rows.append({**dict(zip(names, key)), "value": value, "numerator": n, "denominator": d_ if m.denominator else None})
    return rows
