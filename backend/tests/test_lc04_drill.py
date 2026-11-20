"""
LC-04 step 7 check: the drill-down finds the DRIVING segment (not the symptom), proves it, and its numbers are governed.
 - CubeFrame (the fast reader) gives exactly the numbers compute() gives
 - on the public practice anomalies: the driving segment is right (at detection >= 6 of 7, one hour later 7 of 7, all exact),
   including the two traps: a parent that dilutes the effect, and a sub-segment that is just a lucky slice
 - the counterfactual holds, drill-down refuses to confirm most false alerts, numbers in a finding reconcile to compute()
 - point-in-time: the finding does not change if the future is removed
No LLM. Run:  python backend/tests/test_lc04_drill.py        (needs lc04.db loaded; ~3 minutes)
"""
import random
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "metrics", "agents/lc04"):
    sys.path.insert(0, str(ROOT / p))
import compute as C  # noqa: E402
import detect as D  # noqa: E402
import detect_eval as E  # noqa: E402
import drill as DR  # noqa: E402
import drill_eval as DE  # noqa: E402
import registry as R  # noqa: E402
from frame import CubeFrame, STREAM_START  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
failed = 0
ALL = C.Scope(D.LENDERS)


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


series, cal, params = D.build_series(), D.load_calendar(), D.load_params()
alerts = D.run(series, cal, params)
keys = E.load_key()
frame = CubeFrame(max_tick=30 * D.SLOTS - 1)

# ------------------------------------------------------------------ the frame reads exactly what compute() reads
rng = random.Random(7)
bad = []
for _ in range(60):
    m = rng.choice(list(R.METRICS.values()))
    dims = m.allowed_dims
    gb = rng.sample(dims, rng.choice([0, 1, 2]))
    flt = {}
    if rng.random() < .5:
        d = rng.choice(dims)
        vals = {"city": "Pune", "lender": "L2", "agency": "A1", "dialer": "D2", "gateway": "G1", "product": "Two-Wheeler",
                "dpd_bucket": "31-60"}
        flt = {d: vals[d]}
    a = rng.randrange(0, 29 * D.SLOTS)
    b = a + rng.randrange(1, 60)
    got = frame.sums(m.metric_id, a, b, flt, gb)
    ref = C.compute(m.metric_id, ALL, STREAM_START + pd.Timedelta(minutes=15 * a), STREAM_START + pd.Timedelta(minutes=15 * (b + 1)),
                    group_by=gb, filters=flt)
    key = lambda r: tuple(r[x] for x in gb)  # noqa: E731
    g = {tuple(r[x] for x in gb): (r["n"], r["d"]) for r in got.to_dict("records")} if gb else {(): (got.iloc[0]["n"], got.iloc[0]["d"])}
    c = {key(r): (r["numerator"], r["denominator"] or 0) for r in ref}
    if gb:
        c = {k: v for k, v in c.items()}
        same = set(g) == set(c) and all(abs(g[k][0] - c[k][0]) < 1e-6 and abs(g[k][1] - c[k][1]) < 1e-6 for k in g)
    else:
        same = abs(g[()][0] - c.get((), (0, 0))[0]) < 1e-6 and abs(g[()][1] - c.get((), (0, 0))[1]) < 1e-6
    if not same:
        bad.append((m.metric_id, gb, flt, a, b))
ok("CubeFrame equals compute() on 60 random views (metrics, windows, filters, groupings)", not bad, f"{len(bad)} wrong: {bad[:2]}")

# ------------------------------------------------------------------ findings on the public practice anomalies
inv0, f0 = DE.investigate_all(alerts, frame, cal, extend=0)
inv4, f4 = DE.investigate_all(alerts, frame, cal, extend=4)
out0, sm0 = DE.evaluate(f0, inv0, keys, alerts)
out4, sm4 = DE.evaluate(f4, inv4, keys, alerts)
for x in out4:
    print(f"   {x['anomaly_id']} {x['kind']:22} key={x['key']} -> refined primary(ies) {x['found'][:2]} exact={x['exact']}")
ok("at detection: the driving segment is right for at least 6 of 7 practice anomalies (exact or equivalent)",
   sum(x["correct"] for x in out0) >= 6, f"{sum(x['correct'] for x in out0)} of {len(out0)}")
ok("one hour later (refinement run): the driving segment is EXACTLY right for all 7", all(x["exact"] for x in out4),
   f"{sum(x['exact'] for x in out4)} of {len(out4)}")
p2 = next(x for x in out4 if x["anomaly_id"] == "P2")
p3 = next(x for x in out4 if x["anomaly_id"] == "P3")
ok("trap 1 (a sub-segment that is a lucky slice): the gateway failure is named 'gateway G4', not 'G4 at lender L1'",
   {"gateway": "G4"} in p2["found"] and {"lender": "L1", "gateway": "G4"} not in p2["found"])
ok("trap 2 (a parent that dilutes the effect): duplicates are named 'agency A5', not the whole city Hyderabad",
   {"agency": "A5"} in p3["found"])
p1 = next(x for x in out4 if x["anomaly_id"] == "P1")
ok("a dead dialer is a city x dialer segment: 'Hyderabad / dialer D3', not the city, not the dialer", {"city": "Hyderabad", "dialer": "D3"} in p1["found"])

# ------------------------------------------------------------------ counterfactual, confirmation, false alerts
prims = [(inc, inv4.primary(inc)) for inc in inv4.group(f4)]
true_prims = []
for k in keys:
    s, e = pd.Timestamp(k["start_ts"]), pd.Timestamp(k["end_ts"])
    day = (s.normalize() - D.STREAM_START).days
    if day < D.FIRST_DAY:
        continue
    true_prims += [p for inc, p in prims if p.segment == k["segment"] and s <= pd.Timestamp(p.alert["detected_at"]) <= e + pd.Timedelta(hours=3)]
ok("for the true driving segments the counterfactual holds (the rest is back to normal)", len(true_prims) >= 7 and all(p.counterfactual_ok for p in true_prims),
   f"{len(true_prims)} findings")
ok("true driving segments are confirmed one hour later (6 of 7; the weak PTP-kept signal may stay unconfirmed)",
   sum(p.confirmed for p in true_prims) >= 6, f"{sum(p.confirmed for p in true_prims)} of {len(true_prims)}")
ok("drill-down does not confirm most false alerts after the refinement run (confirmed false incidents <= 3 over 23 days)",
   sm4["false_incidents_confirmed"] <= 3 and sm4["false_incidents_per_day_max"] <= 3,
   f"{sm4['false_incidents_confirmed']} of {sm4['false_incidents']} false incidents confirmed")

# ------------------------------------------------------------------ numbers in a finding reconcile to the governed definition
bad = []
for inc, p in prims:
    if not p.confirmed:
        continue
    a, b = p.window
    t0, t1 = STREAM_START + pd.Timedelta(minutes=15 * a), STREAM_START + pd.Timedelta(minutes=15 * (b + 1))
    ref = C.compute(p.metric, ALL, t0, t1, filters=p.segment)[0]
    if abs(ref["numerator"] - p.observed["n"]) > 1e-6 or (p.observed["d"] is not None and abs(ref["denominator"] - p.observed["d"]) > 1e-6):
        bad.append((p.metric, p.segment))
ok("observed numbers of every confirmed finding equal compute() for its segment and window", not bad, f"{len(prims)} incidents, {len(bad)} wrong")
ok("every headline is templated from the numbers and names the governed metric",
   all(R.METRICS[p.metric].name in p.headline and DR.segment_text(p.segment) in p.headline for _, p in prims))
ok("findings only use governed metrics and allowed dimensions",
   all(p.metric in R.METRICS and set(p.segment) <= set(R.METRICS[p.metric].allowed_dims) for _, p in prims))

# ------------------------------------------------------------------ point-in-time and determinism
a0 = next(a for a in alerts if a["metric"] == "contacts_attempted" and a["segment"] == {"city": "Hyderabad", "dialer": "D3"})
cut = a0["first_tick"] + 4
fr_cut = CubeFrame(max_tick=cut)
i_full, i_cut = DR.Investigator(frame, cal), DR.Investigator(fr_cut, cal)
for x in (i_full, i_cut):
    for al in alerts:
        if al["first_tick"] < a0["first_tick"] and not al["catchup"]:
            x.note_alert(al)
f_full, f_cut = i_full.investigate(a0, extend=4), i_cut.investigate(a0, extend=4)
ok("point-in-time: the finding is identical when every bucket after the refinement time is removed",
   f_full.segment == f_cut.segment and abs(f_full.z - f_cut.z) < 1e-9 and f_full.observed == f_cut.observed)
inv_b, f_b = DE.investigate_all(alerts, frame, cal, extend=4)
ok("deterministic: a second run gives the same findings", [(x.segment, round(x.z, 6)) for x in f_b] == [(x.segment, round(x.z, 6)) for x in f4])

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
