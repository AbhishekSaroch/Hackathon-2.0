"""
LC-04 compliance guard: legal communications must go out between 08:00 and 20:00. This is a RULE, not a statistical detector: the night-time
baseline is zero, so "zero tolerance" is the only sensible test. As soon as a bucket that has ARRIVED by the clock contains a legal
notice sent outside the window, an incident is raised; nothing is learned from history, nothing is tuned.

How it works (numbers come from the governed metrics via compute(); only the send-time range, a fact about the events, is read from them):
  1. FIND   buckets (by arrival time, like every other metric) with legal_notices_out_of_window > 0; buckets within an hour of each
            other are ONE incident.
  2. DRILL  which agency / lender / city / product / DPD bucket carries the notices: a dimension value that holds at least 80% of them is
            the driver (the segment). Lenders holding at least 3% are "affected".
  3. TEXT   two templated lines and a link (see alerts.two_lines for the other incidents). The platform view and each lender's own view
            (a lender user sees only its own count) are written separately.
Point-in-time: only buckets that ended by the clock are looked at, so an incident appears when the sender's log ARRIVES, not when the
notices were sent (a batch log that reaches the platform at 06:15 about 03:00 sends is found at 06:15, three hours late, and the text says so).
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
sys.path.insert(0, str(HERE.parents[1] / "db"))
import alerts as AL  # noqa: E402
import compute as C  # noqa: E402
import cube  # noqa: E402
import db  # noqa: E402
import registry  # noqa: E402

DB = "lc04"
METRIC = "legal_notices_out_of_window"
ALL = C.Scope(("L1", "L2", "L3", "L4", "L5"))
WINDOW = f"{cube.LEGAL_START_HOUR:02d}:00-{cube.LEGAL_END_HOUR:02d}:00"
DIMS = ["agency", "lender", "city", "product", "dpd_bucket"]
DRIVER_SHARE = 0.80
LENDER_SHARE = 0.03
GAP_BUCKETS = 4
FMT = "%Y-%m-%d %H:%M:%S"


def _first_day():
    best = None
    conn = db.get_conn(DB)
    d = conn.execute("SELECT MIN(day) FROM cube_15m WHERE legal_sent > 0").fetchone()[0]
    conn.close()
    return d


def _hm(ts):
    return pd.Timestamp(ts).strftime("%H:%M")


def _events(lender, s, e):
    """Send-time and arrival range of the out-of-window notices that arrived in [s, e) for one lender (a fact about the events)."""
    h = "CAST(substr(ts,12,2) AS INT)"
    r = db.query(lender, f"SELECT MIN(ts) a, MAX(ts) b, MIN(ingested_at) c, MAX(ingested_at) d, COUNT(*) n FROM lc_events WHERE tenant_id=:tenant_id "
                         f"AND event_type='legal' AND ingested_at >= :s AND ingested_at < :e AND ({h} < {cube.LEGAL_START_HOUR} OR {h} >= {cube.LEGAL_END_HOUR})",
                 {"s": s, "e": e}, DB)[0]
    return r


def incidents(clock):
    """Compliance incidents visible at `clock` (each: id, detected_at, window, count, driver, lenders, per-lender numbers, texts)."""
    d0 = _first_day()
    clock = pd.Timestamp(clock).floor("15min")
    if d0 is None or clock <= pd.Timestamp(d0):
        return []
    rows = [r for r in C.compute(METRIC, ALL, d0, clock, grain="bucket") if r["value"]]
    if not rows:
        return []
    ticks = sorted(pd.Timestamp(r["time"]) for r in rows)
    clusters, cur = [], [ticks[0]]
    for t in ticks[1:]:
        if t - cur[-1] <= pd.Timedelta(minutes=15 * GAP_BUCKETS):
            cur.append(t)
        else:
            clusters.append(cur)
            cur = [t]
    clusters.append(cur)
    out = []
    for cl in clusters:
        s, e = cl[0], cl[-1] + pd.Timedelta(minutes=15)
        total = C.compute(METRIC, ALL, s, e)[0]["value"]
        shares, driver = {}, {}
        for dim in DIMS:
            part = {r[dim]: r["value"] for r in C.compute(METRIC, ALL, s, e, group_by=[dim]) if r["value"]}
            shares[dim] = {k: round(v / total, 3) for k, v in sorted(part.items(), key=lambda kv: -kv[1])}
            top = next(iter(shares[dim].items()), None)
            if top and top[1] >= DRIVER_SHARE:
                driver[dim] = top[0]
        lenders = [k for k, v in shares["lender"].items() if v >= LENDER_SHARE]
        per_lender = {}
        for L in lenders:
            n = C.compute(METRIC, ALL, s, e, filters={"lender": L})[0]["value"]
            ev = _events(L, s.strftime(FMT), e.strftime(FMT))
            per_lender[L] = dict(count=int(n), sent_from=ev["a"], sent_to=ev["b"], logged_from=ev["c"], logged_to=ev["d"], events=ev["n"])
        evs = [per_lender[L] for L in lenders]
        sent_from, sent_to = min(x["sent_from"] for x in evs), max(x["sent_to"] for x in evs)
        logged_from, logged_to = min(x["logged_from"] for x in evs), max(x["logged_to"] for x in evs)
        t_detect = e
        iid = f"CMP-{t_detect:%m%d-%H%M}"
        seg = driver or {}
        where = _segment_text(seg)
        l1 = (f"{int(total)} legal notices were sent at {_hm(sent_from)}-{_hm(sent_to)}, outside the permitted {WINDOW} window "
              f"(logged {_hm(logged_from)}-{_hm(logged_to)}).")
        l2 = (f"Driver: {where} ({round(max(shares[k][v] for k, v in seg.items()) * 100) if seg else 0}% of them). "
              f"Lenders affected: {', '.join(lenders)}." if seg else f"No single sender: spread over {', '.join(lenders)}.")
        rows_out = []
        for lender, role in AL.audience(METRIC, lenders):
            if role in ("lender_rm", "lender_user"):
                p = per_lender[lender]
                a1 = (f"{p['count']} legal notices to your customers were sent at {_hm(p['sent_from'])}-{_hm(p['sent_to'])}, outside the permitted "
                      f"{WINDOW} window (logged {_hm(p['logged_from'])}-{_hm(p['logged_to'])}).")
                a2 = f"Driver: {where}. This is the part of your book that is affected; the figures above are yours only."
            else:
                a1, a2 = l1, l2
            rows_out.append(dict(tenant=lender, role=role, line1=a1, line2=a2))
        out.append(dict(
            incident_id=iid, t_detect=str(t_detect), window_start=str(s), window_end=str(e), buckets=len(cl), count=int(total), segment=seg,
            lenders=lenders, per_lender=per_lender, shares=shares, sent_from=sent_from, sent_to=sent_to, logged_from=logged_from, logged_to=logged_to,
            headline=l1 + " " + l2, line1=l1, line2=l2, rows=rows_out,
            rule=dict(name="legal communications only between " + WINDOW.replace("-", " and "), window=[cube.LEGAL_START_HOUR, cube.LEGAL_END_HOUR],
                      count=int(total), sent_from=sent_from, sent_to=sent_to, logged_from=logged_from, logged_to=logged_to)))
    return out


def _segment_text(seg):
    return " / ".join(f"{k} {v}" if k in ("agency", "lender", "dpd_bucket") else str(v) for k, v in seg.items()) or "no single sender"
