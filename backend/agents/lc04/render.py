"""
LC-04 renderer: a validated spec -> the numbers to draw, for ONE user at the simulated clock.

Every number comes from access.scoped_compute, so it is the governed definition, restricted to the user's lenders, metrics and
dimensions; nothing in a spec can widen that. Windows follow the clock (the replay): "today" = midnight .. now, "last_24h",
"last_7d". A KPI also carries the same window one week earlier (same weekday), so a weekly pattern is not mistaken for a change.
Markers: the incidents this user's role was alerted about, shown on the time series.

    out = render(principal, spec, clock)      # refreshed by the UI every spec["refresh_seconds"] (900 s = every 15 minutes)
"""
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
sys.path.insert(0, str(HERE.parents[1] / "db"))
import access as A  # noqa: E402
import registry  # noqa: E402

STREAM_START = pd.Timestamp("2026-09-28")
FMT = "%Y-%m-%d %H:%M:%S"
MAX_ROWS = 20


def window(spec, clock):
    end = pd.Timestamp(clock).floor("15min")
    kind = spec.get("window", "today")
    if kind == "last_7d":
        start = end - pd.Timedelta(days=7)
    elif kind == "last_24h":
        start = end - pd.Timedelta(hours=24)
    else:
        start = end.normalize()
    return max(start, STREAM_START), end


def _clean(v):
    return None if v is None or (isinstance(v, float) and v != v) else v


def render_panel(p, panel, start, end):
    mid, m = panel["metric_id"], registry.get(panel["metric_id"])
    base = dict(type=panel["type"], metric_id=mid, title=panel["title"], unit=m.unit, name=m.name, good_direction=m.good_direction,
                group_by=panel["group_by"], filters=panel["filters"], annotate=panel.get("annotate", False))
    if end <= start:
        return {**base, "empty": True}
    if panel["type"] == "kpi":
        now = A.scoped_compute(p, mid, start, end, filters=panel["filters"])[0]
        week = pd.Timedelta(days=7)
        prev = A.scoped_compute(p, mid, start - week, end - week, filters=panel["filters"])[0] if start - week >= STREAM_START else None
        val, pv = _clean(now["value"]), _clean(prev["value"]) if prev else None
        return {**base, "value": val, "baseline": pv, "baseline_label": "same window last week",
                "change_pct": (None if val is None or not pv else (val - pv) / abs(pv) * 100)}
    if panel["type"] == "timeseries":
        rows = A.scoped_compute(p, mid, start, end, group_by=panel["group_by"], grain=panel["grain"], filters=panel["filters"])
        if panel["group_by"]:
            g = panel["group_by"][0]
            series = {}
            for r in rows:
                series.setdefault(str(r[g]), []).append([r["time"], _clean(r["value"])])
            return {**base, "series": [dict(name=k, points=v) for k, v in sorted(series.items())][:MAX_ROWS]}
        return {**base, "series": [dict(name=m.name, points=[[r["time"], _clean(r["value"])] for r in rows])]}
    rows = A.scoped_compute(p, mid, start, end, group_by=panel["group_by"], filters=panel["filters"])
    names = panel["group_by"]
    rows = sorted(rows, key=lambda r: (r["value"] is None, -(r["value"] or 0)))[:MAX_ROWS]
    return {**base, "rows": [dict(segment={n: r[n] for n in names}, value=_clean(r["value"]), numerator=r["numerator"],
                                  denominator=r["denominator"]) for r in rows]}


def markers(p, clock):
    """Incidents this user's role was alerted about, up to the clock (same rows the alert list shows)."""
    rows = A.scoped_query(p, "SELECT incident_id, created_at, line1, line2, link FROM alerts WHERE tenant_id=:tenant_id AND "
                             "audience_role=:role AND created_at <= :c", {"role": p.role_id, "c": pd.Timestamp(clock).strftime(FMT)})
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["created_at"]):
        if r["incident_id"] not in seen:
            seen.add(r["incident_id"])
            out.append(dict(incident_id=r["incident_id"], at=r["created_at"], line1=r["line1"], line2=r["line2"], link=r["link"]))
    return out


def render(p, spec, clock):
    p = A._verify(p)
    t0 = time.time()
    start, end = window(spec, clock)
    panels = []
    for pn in spec["panels"]:
        try:
            panels.append(render_panel(p, pn, start, end))
        except A.AccessError as e:
            panels.append(dict(type=pn["type"], metric_id=pn["metric_id"], title=pn["title"], error=str(e)))
    return dict(clock=str(pd.Timestamp(clock)), window=dict(start=str(start), end=str(end), kind=spec.get("window", "today")),
               refresh_seconds=spec.get("refresh_seconds", 900), panels=panels, markers=markers(p, clock), ms=int((time.time() - t0) * 1000))
