"""
LC-04 detector evaluation on the PUBLIC days only (days 1-30): did it find the practice anomalies within the hour, and how
many false alerts per day? Never reads the hidden answer key (it is not even in the database in development mode).

  python detect_eval.py                 run with the saved parameters
  python detect_eval.py --fit --save    fit the dispersion factors on training days 8-20 and save params
  python detect_eval.py --T 6 5 4.5     override thresholds for windows 2 / 4 / 8 (tuning)
  python detect_eval.py --grid          try a grid of thresholds
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect as D  # noqa: E402

C, db = D.C, D.db
ALL = C.Scope(D.LENDERS)
TRAIN_DAYS = range(D.FIRST_DAY, 20)


def load_key(table="anomaly_key"):
    """Evaluator only: the answer key (public practice anomalies by default; 'anomaly_key_hidden' in the final run)."""
    conn = db.get_conn("lc04")
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY anomaly_id")]
    conn.close()
    for r in rows:
        r["segment"] = json.loads(r["segment"])
        if r["tenant_id"] != "GLOBAL":
            r["segment"] = {**r["segment"], "lender": r["tenant_id"]}
    return rows


_pop = {}


def population(seg, day):
    """events_total of a segment on `day` (a stable measure of which part of the book it covers)."""
    key = (json.dumps(seg, sort_keys=True), day)
    if key not in _pop:
        s = pd.Timestamp(D.STREAM_START) + pd.Timedelta(days=day)
        _pop[key] = C.compute("events_total", ALL, s, s + pd.Timedelta(days=1), filters=seg)[0]["numerator"]
    return _pop[key]


def related(alert_seg, key_seg, day):
    """The alert's segment and the anomaly's segment cover the same part of the book (>= 10% of one inside the other)."""
    if any(k in key_seg and key_seg[k] != v for k, v in alert_seg.items()):
        return False
    both = {**key_seg, **alert_seg}
    n_both, n_key, n_alert = population(both, day), population(key_seg, day), population(alert_seg, day)
    return n_both > 0 and (n_both / max(n_key, 1) >= .10 or n_both / max(n_alert, 1) >= .10)


def evaluate(alerts, keys, first_day=D.FIRST_DAY, last_day=29, verbose=True):
    live = [a for a in alerts if not a["catchup"]]
    rep = {"anomalies": [], "false_alerts": []}
    claimed = set()
    for k in keys:
        s, e = pd.Timestamp(k["start_ts"]), pd.Timestamp(k["end_ts"])
        day = (s.normalize() - D.STREAM_START).days
        if day < first_day:
            rep["anomalies"].append({**{x: k[x] for x in ("anomaly_id", "kind")}, "evaluated": False})
            continue
        cands = [a for a in live if s <= pd.Timestamp(a["detected_at"]) <= e + pd.Timedelta(hours=3)
                 and related(a["segment"], k["segment"], max(day - 1, 0))]
        for a in cands:
            claimed.add(id(a))
        ttd = (min(pd.Timestamp(a["detected_at"]) for a in cands) - s).total_seconds() / 60 if cands else None
        rep["anomalies"].append(dict(anomaly_id=k["anomaly_id"], kind=k["kind"], segment=k["segment"], start=k["start_ts"],
                                     evaluated=True, detected=bool(cands), minutes_to_detect=ttd,
                                     within_hour=bool(cands) and ttd <= 60, alerts=[
                                         f"{a['metric']} {a['segment']} {a['direction']} @{a['detected_at'][11:16]} S={a['score']}"
                                         for a in cands[:6]]))
    for a in live:
        if id(a) not in claimed and first_day <= a["day"] <= last_day:
            rep["false_alerts"].append(a)
    days = list(range(first_day, last_day + 1))
    per_day = pd.Series([a["day"] for a in rep["false_alerts"]], dtype=int).value_counts().reindex(days, fill_value=0)
    inc = D.incidents([a for a in rep["false_alerts"]])
    inc_day = pd.Series([g[0]["day"] for g in inc], dtype=int).value_counts().reindex(days, fill_value=0)
    ev = [x for x in rep["anomalies"] if x["evaluated"]]
    rep["summary"] = dict(anomalies=len(ev), detected=sum(x["detected"] for x in ev), within_hour=sum(x["within_hour"] for x in ev),
                          false_alerts_total=int(per_day.sum()), false_alerts_per_day_mean=round(float(per_day.mean()), 2),
                          false_alerts_per_day_max=int(per_day.max()), false_incidents_per_day_max=int(inc_day.max()),
                          alerts_total=len(live), catchup_merged=len(alerts) - len(live))
    rep["false_per_day"] = {int(k): int(v) for k, v in per_day.items()}
    if verbose:
        print(f"{'id':3} {'kind':22} {'detected':9} {'minutes':8} first alerts")
        for x in rep["anomalies"]:
            if not x["evaluated"]:
                print(f"{x['anomaly_id']:3} {x['kind']:22} (before the warm-up, not evaluated)")
                continue
            m = "-" if x["minutes_to_detect"] is None else f"{x['minutes_to_detect']:.0f}"
            print(f"{x['anomaly_id']:3} {x['kind']:22} {str(x['detected']):9} {m:8} {x['alerts'][:2]}")
        print("false alerts per day (days 8-30):", {d + 1: n for d, n in rep["false_per_day"].items() if n})
        print(rep["summary"])
        for a in rep["false_alerts"][:12]:
            print("  FALSE", a["detected_at"][:16], a["metric"], a["segment"], a["direction"], f"S={a['score']} W={a['window']}")
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--T", nargs=3, type=float)
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()
    series, cal, params = D.build_series(), D.load_calendar(), D.load_params()
    print(f"{len(series)} monitored series")
    if a.fit:
        params["phi"] = D.fit_phi(series, cal, params, TRAIN_DAYS)
        print("fitted dispersion (phi):", params["phi"])
    if a.T:
        params["T"] = {"2": a.T[0], "4": a.T[1], "8": a.T[2], "16": a.T[2]}
    keys = load_key()
    if a.grid:
        for t2, t4, t8 in itertools.product((5.0, 6.0, 7.0), (4.5, 5.0, 6.0), (4.0, 4.5, 5.5)):
            params["T"] = {"2": t2, "4": t4, "8": t8, "16": t8}
            r = evaluate(D.run(series, cal, params), keys, verbose=False)["summary"]
            print(f"T=({t2},{t4},{t8}) detected {r['detected']}/{r['anomalies']} within hour {r['within_hour']} "
                  f"false/day mean {r['false_alerts_per_day_mean']} max {r['false_alerts_per_day_max']}")
        return
    rep = evaluate(D.run(series, cal, params), keys)
    if a.save:
        D.PARAMS_PATH.write_text(json.dumps({"phi": params["phi"], "T": params["T"], "min_n": params["min_n"],
                                             "min_d": params["min_d"], "alpha": params["alpha"]}, indent=2))
        print("saved", D.PARAMS_PATH.name)


if __name__ == "__main__":
    main()
