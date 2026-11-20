"""
LC-04 drill-down evaluation on the PUBLIC days: investigate every alert the detector raises, group the findings into incidents
(one per root cause) and check the DRIVING SEGMENT of each incident against the practice answer key.

  python drill_eval.py            (needs lc04.db loaded; ~1 minute)
"""
import sys
import warnings
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect as D  # noqa: E402
import detect_eval as E  # noqa: E402
import drill as DR  # noqa: E402
from frame import CubeFrame  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)


def investigate_all(alerts, frame, cal, extend=0):
    """Replay in time order, so the history only excludes days that already had an alert. extend = refinement run, N buckets later."""
    inv = DR.Investigator(frame, cal)
    findings = []
    for a in sorted([x for x in alerts if not x["catchup"]], key=lambda x: x["first_tick"]):
        try:
            findings.append(inv.investigate(a, extend=extend))
        except ValueError:
            pass
        inv.note_alert(a)
    return inv, findings


def evaluate(findings, inv, keys, alerts):
    incs = inv.group(findings)
    prim = [(inc, inv.primary(inc)) for inc in incs]
    out = []
    for k in keys:
        s, e = pd.Timestamp(k["start_ts"]), pd.Timestamp(k["end_ts"])
        day = (s.normalize() - D.STREAM_START).days
        if day < D.FIRST_DAY:
            continue
        mine = [(inc, p) for inc, p in prim if any(s <= pd.Timestamp(f.alert["detected_at"]) <= e + pd.Timedelta(hours=3) for f in inc)
                and E.related(p.segment, k["segment"], max(day - 1, 0))]
        exact = [(inc, p) for inc, p in mine if p.segment == k["segment"]]
        equiv = [(inc, p) for inc, p in mine if inv.equivalent(p.segment, k["segment"], max(day, 0))]
        out.append(dict(anomaly_id=k["anomaly_id"], kind=k["kind"], key=k["segment"], incidents=len(mine),
                        exact=bool(exact), equivalent=bool(equiv), correct=bool(exact or equiv),
                        found=[p.segment for _, p in mine], confirmed=[p.confirmed for _, p in mine],
                        counterfactual=[p.counterfactual_ok for _, p in mine]))
    rel = set()
    for k in keys:
        s, e = pd.Timestamp(k["start_ts"]), pd.Timestamp(k["end_ts"])
        day = (s.normalize() - D.STREAM_START).days
        for inc, p in prim:
            if any(s <= pd.Timestamp(f.alert["detected_at"]) <= e + pd.Timedelta(hours=3) for f in inc) and \
                    E.related(p.segment, k["segment"], max(day - 1, 0)):
                rel.add(id(inc))
    false_inc = [(inc, p) for inc, p in prim if id(inc) not in rel and D.FIRST_DAY <= p.alert["day"] <= 29]
    per_day = pd.Series([p.alert["day"] for _, p in false_inc], dtype=int).value_counts().reindex(range(D.FIRST_DAY, 30), fill_value=0)
    return out, dict(incidents=len(incs), false_incidents=len(false_inc), false_incidents_confirmed=sum(p.confirmed for _, p in false_inc),
                     false_incidents_per_day_max=int(per_day.max()), false_incidents_per_day_mean=round(float(per_day.mean()), 2))


def main():
    series, cal, params = D.build_series(), D.load_calendar(), D.load_params()
    alerts = D.run(series, cal, params)
    keys = E.load_key()
    frame = CubeFrame(max_tick=30 * D.SLOTS - 1)
    res = {}
    for label, extend in (("AT DETECTION", 0), ("REFINED one hour later (+4 buckets)", 4)):
        inv, findings = investigate_all(alerts, frame, cal, extend=extend)
        out, sm = evaluate(findings, inv, keys, alerts)
        print(f"\n== {label}: {len(findings)} alerts investigated")
        for x in out:
            print(f"{x['anomaly_id']} {x['kind']:22} key={x['key']}  exact: {x['exact']} equivalent: {x['equivalent']}  incidents: "
                  f"{x['found']} confirmed={x['confirmed']}")
        print(sm, " correct (exact or equivalent):", sum(x["correct"] for x in out), "of", len(out))
        res[extend] = (out, sm)
    return res


if __name__ == "__main__":
    main()
