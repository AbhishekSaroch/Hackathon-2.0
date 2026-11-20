"""
LC-04 step 6 check: the seasonal detector, on the PUBLIC days only.
 - every practice anomaly is found (and the fast ones within the hour), at most 3 false alerts per day
 - seasonality decoys (Sunday, regional festival) do not alert
 - point-in-time: poisoning the future does not change any alert that was already raised
 - deterministic, built from governed numbers, never reads the answer key
No LLM. Run:  python backend/tests/test_lc04_detect.py        (needs lc04.db loaded; ~1 minute)
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "metrics", "agents/lc04"):
    sys.path.insert(0, str(ROOT / p))
import detect as D  # noqa: E402
import detect_eval as E  # noqa: E402
import registry as R  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


series, cal, params = D.build_series(), D.load_calendar(), D.load_params()
alerts = D.run(series, cal, params)
rep = E.evaluate(alerts, E.load_key(), verbose=False)
sm = rep["summary"]
print("summary:", sm)
for x in rep["anomalies"]:
    print(f"   {x['anomaly_id']} {x['kind']:22} detected={x['detected']} minutes={x['minutes_to_detect']}")

# ------------------------------------------------------------------ recall and false alerts (public tuning set)
ok("all 7 practice anomalies are detected (every one of the 6 kinds)", sm["detected"] == sm["anomalies"] == 7,
   f"{sm['detected']} of {sm['anomalies']}")
fast = [x["anomaly_id"] for x in rep["anomalies"] if x.get("within_hour")]
ok("at least 5 of 7 are detected within an hour of onset (dialer outages, duplicates, mis-dispositioning, feed stall)",
   sm["within_hour"] >= 5, f"{fast}")
slow = [(x["anomaly_id"], x["minutes_to_detect"]) for x in rep["anomalies"] if x["evaluated"] and not x.get("within_hour")]
print("   detected later than an hour (too few events in the segment to be sure sooner):", slow)
ok("false alerts: never more than 3 in a day, and under 1 a day on average (days 8-30)",
   sm["false_alerts_per_day_max"] <= 3 and sm["false_alerts_per_day_mean"] < 1, f"max {sm['false_alerts_per_day_max']}, mean {sm['false_alerts_per_day_mean']}")
ok("false incidents (grouped) never more than 3 in a day", sm["false_incidents_per_day_max"] <= 3)

# ------------------------------------------------------------------ seasonality decoys
live = [a for a in alerts if not a["catchup"]]
day = lambda s: (pd.Timestamp(s) - D.STREAM_START).days  # noqa: E731
sundays = {day("2026-10-11"), day("2026-10-18"), day("2026-10-25")}
sun_false = [a for a in rep["false_alerts"] if a["day"] in sundays]
ok("Sundays (contact volume 35%): at most 1 false alert in total over 3 Sundays", len(sun_false) <= 1, f"{len(sun_false)}")
fest = [a for a in live if a["day"] in range(day("2026-10-17"), day("2026-10-21")) and "Kolkata" in a["segment"].values()]
ok("regional festival (Kolkata, Oct 17-20): no alert on a Kolkata segment", not fest)
a6 = [a for a in live if a["day"] in range(day("2026-10-17"), day("2026-10-21")) and a["metric"] in D.VOLUME_METRICS
      and a["segment"].get("agency") == "A6"]
ok("regional festival: the Kolkata-based agency A6 does not alert on volume either", not a6)
nat = [a for a in rep["false_alerts"] if a["day"] == day("2026-10-20")]
ok("national festival (Oct 20, contacts 45%, payments 125%): at most 1 false alert", len(nat) <= 1, f"{len(nat)}")
ok("no alert before the warm-up week", all(a["day"] >= D.FIRST_DAY for a in alerts))

# ------------------------------------------------------------------ point-in-time: poison the future
CUT_DAY, CUT_SLOT = 17, 56                                   # Oct 15, 14:00: right after the P1 outage window ends
cut_tick = CUT_DAY * D.SLOTS + CUT_SLOT
rng = np.random.default_rng(0)
poisoned = []
for s in series:
    n, d = s.n.copy(), s.d.copy()
    n[cut_tick + 1:] = rng.permutation(n[cut_tick + 1:]) * 37 + 5          # garbage future, wildly different
    d[cut_tick + 1:] = np.maximum(d[cut_tick + 1:], n[cut_tick + 1:]) if s.is_ratio else rng.permutation(d[cut_tick + 1:]) * 37 + 5
    poisoned.append(D.Series(s.metric, s.dims, s.vals, n, d, s.is_ratio))
sig = lambda al, until: sorted((a["metric"], str(a["segment"]), a["detected_at"], a["score"], a["direction"])  # noqa: E731
                               for a in al if pd.Timestamp(a["detected_at"]) <= until)
until = D.bucket_end(cut_tick)
a_true = D.run(series, cal, params, last_day=CUT_DAY)
a_pois = D.run(poisoned, cal, params, last_day=CUT_DAY)
ok("point-in-time: every alert raised up to Oct 15 14:00 is identical when everything after it is replaced by garbage",
   sig(a_true, until) == sig(a_pois, until) and len(sig(a_true, until)) > 0, f"{len(sig(a_true, until))} alerts compared")
a_early = D.run(series, cal, params, last_day=CUT_DAY - 3)
ok("point-in-time: alerts of the first 14 days are identical whether or not later days exist",
   sig(a_early, D.STREAM_START + pd.Timedelta(days=CUT_DAY - 3)) == sig(a_true, D.STREAM_START + pd.Timedelta(days=CUT_DAY - 3)))

# ------------------------------------------------------------------ governance, determinism, discipline
ok("deterministic: a second run gives exactly the same alerts", sig(D.run(series, cal, params), pd.Timestamp("2027-01-01")) == sig(alerts, pd.Timestamp("2027-01-01")))
ok("only governed, monitored (key) metrics are watched", {m for m, _ in D.SCAN} <= {m.metric_id for m in R.key_metrics()})
import compute as C  # noqa: E402
s0 = next(s for s in series if s.metric == "contacts_attempted" and s.dims == ("city",) and s.vals == ("Pune",))
gov = C.compute("contacts_attempted", C.Scope(D.LENDERS), D.STREAM_START, D.STREAM_START + pd.Timedelta(days=30), filters={"city": "Pune"})[0]
ok("the detector's numbers are the governed numbers (a series sums to compute())", abs(s0.n.sum() - gov["numerator"]) < 1e-6)
src = (ROOT / "agents" / "lc04" / "detect.py").read_text(encoding="utf-8")
ok("the detector never touches an answer key or hidden data", "anomaly_key" not in src and "hidden" not in src.replace("hidden days", ""))
ok("series cover only the public days (no hidden day in the database)", max(len(s.n) for s in series) == D.SLOTS * 30)
ok("tuned parameters are saved with the code", D.PARAMS_PATH.exists() and "phi" in D.load_params() and D.load_params()["phi"])

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
