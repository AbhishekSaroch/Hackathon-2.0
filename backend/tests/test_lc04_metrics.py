"""
LC-04 step 4 check: "every number reconciles to its governed definition".
1. The registry is sound (about 60 metrics, valid formulas, dimensions derived from the counters, mirrored in the DB).
2. For EVERY metric and 6 different views (totals, per city per day, per hour, per lender per bucket, filtered, multi-lender)
   compute() equals an independent recompute from the RAW events (pandas, not the cube, not the registry's parser).
3. 14 key metrics also match a hand-written definition in plain words.
4. Governance: no ad hoc formula, no illegal dimension, no cross-lender leak, additive across views.
No LLM. Run:  python backend/tests/test_lc04_metrics.py        (needs lc04.db loaded)
"""
import inspect
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "agents/lc04", "metrics", "data"):
    sys.path.insert(0, str(ROOT / p))
import db  # noqa: E402
import cube  # noqa: E402
import registry as R  # noqa: E402
import compute as C  # noqa: E402
from compute import Scope  # noqa: E402
from lc04_anomalies import KIND_METRIC  # noqa: E402

failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


def close(a, b):
    """exact up to floating point noise (np.isclose's default 1e-5 relative tolerance would hide +1 on 400,000)"""
    return abs(float(a) - float(b)) <= 1e-6 + 1e-9 * abs(float(b))


def raises(fn):
    try:
        fn()
    except R.MetricError:
        return True
    except Exception as e:  # noqa: BLE001
        print("   unexpected", type(e).__name__, e)
    return False


# ------------------------------------------------------------------ 1. registry
M = R.METRICS
ok("about 60 governed metrics", 55 <= len(M) <= 65, f"{len(M)} metrics, {len(R.key_metrics())} monitored")
ok("every metric has a description, an owner, a good direction and at least one sliceable dimension",
   all(m.description and m.owner and m.good_direction in ("up", "down", "neutral") and m.allowed_dims for m in M.values()))
ok("payment metrics cannot be sliced by agency or dialer; call metrics cannot be sliced by gateway",
   not {"agency", "dialer"} & set(M["pay_success_rate"].allowed_dims) and "gateway" in M["pay_success_rate"].allowed_dims
   and "gateway" not in M["connect_rate"].allowed_dims and "dialer" in M["connect_rate"].allowed_dims
   and "agency" in M["ptp_capture_rate"].allowed_dims and set(M["events_total"].allowed_dims) == set(R.DIMS))
ok("the metric that shows each of the 6 anomaly kinds is a governed, monitored metric",
   all(mid in M and M[mid].is_key for mid in KIND_METRIC.values()), str(sorted(set(KIND_METRIC.values()))))
ok("a formula with an unknown counter is refused", raises(lambda: R.parse_terms("pay_success + made_up")))
n_db = R.sync_to_db()
rows = db.query("GLOBAL", "SELECT * FROM metric_defs WHERE tenant_id=:tenant_id", database="lc04")
ok("metric_defs table mirrors the registry exactly", n_db == len(M) == len(rows) and all(
    r["numerator"] == M[r["metric_id"]].numerator and r["denominator"] == M[r["metric_id"]].denominator
    and r["unit"] == M[r["metric_id"]].unit and bool(r["is_key"]) == M[r["metric_id"]].is_key for r in rows))

# ------------------------------------------------------------------ raw events, independent of the cube
conn = db.get_conn("lc04")
ev = pd.read_sql_query(
    "SELECT e.tenant_id, e.ts, e.ingested_at, e.event_type, e.outcome, e.agency_id, e.dialer_id, e.gateway_id, e.amount, "
    "a.city, a.product, a.bucket AS dpd_bucket FROM lc_events e JOIN lc_accounts a ON a.account_id = e.account_id", conn)
ev["bucket_ts"] = pd.to_datetime(ev["ingested_at"]).dt.floor("15min").dt.strftime("%Y-%m-%d %H:%M:%S")
ev["lender"] = ev["tenant_id"]
ev["agency"], ev["dialer"], ev["gateway"] = ev["agency_id"].fillna("-"), ev["dialer_id"].fillna("-"), ev["gateway_id"].fillna("-")
t, o = ev["event_type"], ev["outcome"]
raw = {"ev_total": np.ones(len(ev)), "contacts_attempted": t == "contact", "contacts_connected": (t == "contact") & (o == "connected"),
       "disp_total": t == "disposition", "disp_rpc": (t == "disposition") & (o == "rpc"), "disp_ptp": (t == "disposition") & (o == "ptp"),
       "disp_refused": (t == "disposition") & (o == "refused"), "disp_dispute": (t == "disposition") & (o == "dispute"),
       "disp_other": (t == "disposition") & o.isin(["wrong_number", "callback"]), "ptp_created": (t == "ptp") & (o == "created"),
       "ptp_kept": (t == "ptp") & (o == "kept"), "ptp_broken": (t == "ptp") & (o == "broken"), "pay_attempts": t == "payment",
       "pay_success": (t == "payment") & (o == "success"), "pay_failed": (t == "payment") & (o == "failed"),
       "legal_sent": t == "legal",
       "legal_out_of_window": (t == "legal") & ((pd.to_datetime(ev["ts"]).dt.hour < 8) | (pd.to_datetime(ev["ts"]).dt.hour >= 20))}
for k, v in raw.items():
    ev[k] = np.asarray(v).astype(float)
ev["ptp_amount"] = np.where((t == "ptp") & (o == "created"), ev["amount"].fillna(0), 0.0)
ev["pay_amount"] = np.where((t == "payment") & (o == "success"), ev["amount"].fillna(0), 0.0)
ev["hour"] = ev["bucket_ts"].str[:13] + ":00:00"
ev["day"] = ev["bucket_ts"].str[:10]
ev["bucket"] = ev["bucket_ts"]
TIME_COL = {"bucket": "bucket", "hour": "hour", "day": "day"}
_cache = {}


def ref_groups(lenders, s, e, group_by, grain, filters):
    key = (tuple(lenders), s, e, tuple(group_by), grain, repr(sorted(filters.items())))
    if key not in _cache:
        d = ev[ev["tenant_id"].isin(lenders) & (ev["bucket_ts"] >= s) & (ev["bucket_ts"] < e)]
        for dim, v in filters.items():
            d = d[d[dim].isin([v] if isinstance(v, str) else v)]
        cols = list(group_by) + ([TIME_COL[grain]] if grain else [])
        if cols:
            g = d.groupby(cols)[cube.COUNTER_COLS].sum()
        else:
            g = pd.DataFrame([d[cube.COUNTER_COLS].sum()], index=[0])
        _cache[key] = g
    return _cache[key]


def ref_value(m, counters):
    """Test-only evaluator for the registry's formula text, working on counters recomputed from raw events."""
    num = eval(m.numerator, {}, counters.to_dict())             # noqa: S307 (only counter names and + -)
    if not m.denominator:
        return float(num), None, float(num)
    den = eval(m.denominator, {}, counters.to_dict())           # noqa: S307
    return float(num), float(den), (None if den == 0 else num * R.UNITS[m.unit] / den)


# ------------------------------------------------------------------ 2. every metric x 6 views = independent recompute
ALL = ("L1", "L2", "L3", "L4", "L5")


def views(m):
    ad = set(m.allowed_dims)
    pick = next(d for d in ("dialer", "gateway", "agency", "product") if d in ad)
    return [("totals, all lenders, whole public period", ALL, "2026-09-28 00:00:00", "2026-10-28 00:00:00", [], None, {}),
            ("L2+L3, one day, per city", ("L2", "L3"), "2026-10-14 00:00:00", "2026-10-15 00:00:00", ["city"], "day", {}),
            (f"L1, 4 hours, per {pick}, hourly", ("L1",), "2026-10-15 10:00:00", "2026-10-15 14:00:00", [pick], "hour", {}),
            ("all lenders, per lender, every 15 min", ALL, "2026-10-19 14:00:00", "2026-10-19 19:00:00", ["lender"], "bucket", {}),
            ("L1, one day, only Hyderabad, per DPD bucket", ("L1",), "2026-10-15 00:00:00", "2026-10-16 00:00:00", ["dpd_bucket"], None,
             {"city": "Hyderabad"}),
            ("L4+L5, 3 days, Mumbai/Delhi, per product x lender", ("L4", "L5"), "2026-10-13 00:00:00", "2026-10-16 00:00:00",
             ["product", "lender"], None, {"city": ["Mumbai", "Delhi"]})]


checked, bad = 0, []
for m in M.values():
    for label, lenders, s, e, gb, grain, flt in views(m):
        got = C.compute(m.metric_id, Scope(lenders), s, e, group_by=gb, grain=grain, filters=flt)
        exp = ref_groups(lenders, s, e, gb, grain, flt)
        names = gb + (["time"] if grain else [])
        got_map = {tuple(r[n] for n in names): r for r in got}
        exp_keys = {((k if isinstance(k, tuple) else (k,)) if names else ()): k for k in exp.index}
        if set(got_map) != set(exp_keys):
            bad.append((m.metric_id, label, "groups differ", len(got_map), len(exp_keys)))
            continue
        for gk, orig in exp_keys.items():
            n, d, v = ref_value(m, exp.loc[orig])
            r = got_map[gk]
            same = close(r["numerator"], n) and (r["denominator"] is None) == (d is None) and (
                d is None or close(r["denominator"], d)) and ((r["value"] is None) == (v is None)) and (
                v is None or close(r["value"], v))
            if not same:
                bad.append((m.metric_id, label, gk, r["value"], v))
                break
        checked += 1
ok("every metric x 6 views equals the independent recompute from raw events", not bad,
   f"{checked} metric-views, {len(bad)} wrong: {bad[:3]}")

# ------------------------------------------------------------------ 3. plain-words definitions, from raw events only
day = ev[(ev["bucket_ts"] >= "2026-10-14 00:00:00") & (ev["bucket_ts"] < "2026-10-15 00:00:00") & (ev["tenant_id"] == "L1")]
words = {
    "events_total": lambda x: len(x),
    "contacts_attempted": lambda x: (x.event_type == "contact").sum(),
    "connect_rate": lambda x: 100 * ((x.event_type == "contact") & (x.outcome == "connected")).sum() / (x.event_type == "contact").sum(),
    "pay_success_rate": lambda x: 100 * ((x.event_type == "payment") & (x.outcome == "success")).sum() / (x.event_type == "payment").sum(),
    "ptp_capture_rate": lambda x: 100 * ((x.event_type == "disposition") & (x.outcome == "ptp")).sum()
    / ((x.event_type == "contact") & (x.outcome == "connected")).sum(),
    "disposition_coverage": lambda x: 100 * (x.event_type == "disposition").sum() / ((x.event_type == "contact") & (x.outcome == "connected")).sum(),
    "unexplained_dispositions": lambda x: (x.event_type == "disposition").sum() - ((x.event_type == "contact") & (x.outcome == "connected")).sum(),
    "ptp_kept": lambda x: ((x.event_type == "ptp") & (x.outcome == "kept")).sum(),
    "ptp_kept_rate": lambda x: 100 * ((x.event_type == "ptp") & (x.outcome == "kept")).sum()
    / ((x.event_type == "ptp") & x.outcome.isin(["kept", "broken"])).sum(),
    "collected_amount": lambda x: x[(x.event_type == "payment") & (x.outcome == "success")]["amount"].sum(),
    "avg_payment_amount": lambda x: x[(x.event_type == "payment") & (x.outcome == "success")]["amount"].mean(),
    "collection_to_promise_ratio": lambda x: 100 * x[(x.event_type == "payment") & (x.outcome == "success")]["amount"].sum()
    / x[(x.event_type == "ptp") & (x.outcome == "created")]["amount"].sum(),
    "failed_payment_share_of_events": lambda x: 100 * ((x.event_type == "payment") & (x.outcome == "failed")).sum() / len(x),
    "refusal_rate": lambda x: 100 * ((x.event_type == "disposition") & (x.outcome == "refused")).sum()
    / ((x.event_type == "contact") & (x.outcome == "connected")).sum(),
}
miss = []
for mid, fn in words.items():
    got = {r["city"]: r["value"] for r in C.compute(mid, Scope(("L1",)), "2026-10-14 00:00:00", "2026-10-15 00:00:00",
                                                     group_by=["city"])}
    for city, x in day.groupby("city"):
        with np.errstate(all="ignore"):
            try:
                v = fn(x)
            except ZeroDivisionError:
                v = None
        if v is not None and not np.isfinite(v):
            v = None                                    # 0 / 0 in the plain-words version = "no value"
        if not (close(got[city], v) if v is not None and got.get(city) is not None else got.get(city) == v):
            miss.append((mid, city, got.get(city), v))
ok("14 key metrics equal their plain-words definition computed straight from raw events", not miss, str(miss[:3]))

# ------------------------------------------------------------------ 4. governance
ok("compute() takes a metric id only: no formula / expression parameter exists",
   set(inspect.signature(C.compute).parameters) == {"metric_id", "scope", "start", "end", "group_by", "grain", "filters", "database"})
ok("an ungoverned metric is refused", raises(lambda: C.compute("my_adhoc_metric", Scope(ALL), "2026-10-14", "2026-10-15")))
ok("illegal slice refused: payment success by agency", raises(lambda: C.compute("pay_success_rate", Scope(ALL), "2026-10-14", "2026-10-15", group_by=["agency"])))
ok("illegal slice refused: connect rate by gateway", raises(lambda: C.compute("connect_rate", Scope(ALL), "2026-10-14", "2026-10-15", group_by=["gateway"])))
ok("illegal filter refused: payments filtered by dialer", raises(lambda: C.compute("payments_failed", Scope(ALL), "2026-10-14", "2026-10-15", filters={"dialer": "D1"})))
ok("SQL injection through a dimension name is refused", raises(lambda: C.compute("events_total", Scope(ALL), "2026-10-14", "2026-10-15", group_by=["city; DROP TABLE cube_15m"])))
ok("unknown grain / empty window / empty scope refused",
   raises(lambda: C.compute("events_total", Scope(ALL), "2026-10-14", "2026-10-15", grain="minute"))
   and raises(lambda: C.compute("events_total", Scope(ALL), "2026-10-15", "2026-10-14")) and raises(lambda: Scope(())))
tot = lambda lenders, **kw: C.compute("events_total", Scope(lenders), "2026-10-14 00:00:00", "2026-10-16 00:00:00", **kw)[0]["numerator"]  # noqa: E731
direct = {l: int(((ev.tenant_id == l) & (ev.bucket_ts >= "2026-10-14 00:00:00") & (ev.bucket_ts < "2026-10-16 00:00:00")).sum()) for l in ALL}
ok("scope = one lender returns exactly that lender's events and nothing of another",
   all(tot((l,)) == direct[l] for l in ALL) and tot(("L1", "L2")) == direct["L1"] + direct["L2"])
ok("a lender outside the scope sees nothing; a lender filter can narrow a scope but never widen it",
   tot(("NOPE",)) == 0 and tot(("L1", "L2"), filters={"lender": "L2"}) == direct["L2"] and tot(("L1",), filters={"lender": "L2"}) == 0)
a = C.compute("pay_success_rate", Scope(ALL), "2026-10-14 00:00:00", "2026-10-17 00:00:00")[0]
by_l = C.compute("pay_success_rate", Scope(ALL), "2026-10-14 00:00:00", "2026-10-17 00:00:00", group_by=["lender"])
by_d = C.compute("pay_success_rate", Scope(ALL), "2026-10-14 00:00:00", "2026-10-17 00:00:00", grain="day")
by_g = C.compute("pay_success_rate", Scope(ALL), "2026-10-14 00:00:00", "2026-10-17 00:00:00", group_by=["gateway"])
ok("views agree: a total is the sum of its parts (per lender, per day, per gateway), divided once",
   all(close(a[k], sum(r[k] for r in part)) for part in (by_l, by_d, by_g) for k in ("numerator", "denominator"))
   and close(a["value"], 100 * a["numerator"] / a["denominator"]))
t0 = time.time()
for mid in M:
    C.compute(mid, Scope(ALL), "2026-10-14 00:00:00", "2026-10-15 00:00:00", group_by=["city"], grain="hour")
dt = time.time() - t0
ok("all 60 metrics, one day, per city per hour, 5 lenders: under 10 s", dt < 10, f"{dt:.1f} s")
conn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
