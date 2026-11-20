"""
LC-04 step 2 check: the synthetic data is sound, seasonality is real, the practice and hidden anomalies are really in
the stream, and a naive fixed-threshold detector would drown in false alerts. No LLM, no DB (reads the CSVs).
Run:  python backend/tests/test_lc04_data.py        (exit code 1 on failure)
Needs: python backend/data/generate_lc04.py  and  python backend/data/inject_hidden_lc04.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
sys.path.insert(0, str(DATA))
import lc04_world as W  # noqa: E402

CSV = DATA / "csv_lc04"
failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


need = ["accounts.csv", "d13.csv", "calendar.csv", "events_public.csv", "events_hidden_clean.csv", "anomaly_key.csv",
        "events_hidden.csv", "anomaly_key_hidden.csv", "meta.json"]
missing = [f for f in need if not (CSV / f).exists()]
ok("all generated files exist", not missing, str(missing))
if missing:
    sys.exit(1)

acct = pd.read_csv(CSV / "accounts.csv")
pub = W.read_events(CSV / "events_public.csv")
clean = W.read_events(CSV / "events_hidden_clean.csv")
hid = W.read_events(CSV / "events_hidden.csv")
key = pd.read_csv(CSV / "anomaly_key.csv")
keyh = pd.read_csv(CSV / "anomaly_key_hidden.csv")
cal = pd.read_csv(CSV / "calendar.csv")
meta = json.loads((CSV / "meta.json").read_text())
T = pd.Timestamp

# ------------------------------------------------------------------ accounts and dimensions
ok("accounts: unique ids", acct["account_id"].is_unique, f"{len(acct):,} accounts")
share = acct["tenant_id"].value_counts(normalize=True)
ok("5 lenders, shares close to design", len(share) == 5 and all(abs(share[k] - v) < .03 for k, v in W.LENDERS.items()),
   str(share.round(2).to_dict()))
ok("12 cities, 8 agencies", acct["city"].nunique() == 12 and acct["agency_id"].nunique() == 8)
ag_pune = acct[acct["city"] == "Pune"]["agency_id"].value_counts(normalize=True)
ok("agency is concentrated in its home city (confounder for drill-down)", ag_pune[["A1", "A3"]].sum() > .7,
   str(ag_pune.round(2).to_dict()))
ok("calendar has weekend, salary_day, month_end and festival rows (incl. a city-specific one)",
   {"weekend", "salary_day", "month_end", "festival"} <= set(cal["kind"]) and (cal["city"] != "*").any())

# ------------------------------------------------------------------ event integrity (public)
for name, ev, lo, hi in (("public", pub, W.START_TS, W.PUBLIC_END_TS), ("hidden", hid, W.PUBLIC_END_TS, W.END_TS)):
    ok(f"{name}: all timestamps inside its period", ev["ts"].min() >= lo and ev["ts"].max() < hi)
    ok(f"{name}: account ids exist", ev["account_id"].isin(acct["account_id"]).all())
ok("public: ingested_at >= ts, median arrival delay under 60 s",
   (pub["ingested_at"] >= pub["ts"]).all() and (pub["ingested_at"] - pub["ts"]).dt.total_seconds().median() < 60)
valid = {"contact": {"connected", "no_answer", "failed"}, "disposition": set(W.DISP_NAMES),
         "ptp": {"created", "kept", "broken"}, "payment": {"success", "failed"}}
ok("public: outcomes valid for each event type", all(set(g["outcome"]) <= valid[t] for t, g in pub.groupby("event_type")))
ok("public: contacts/dispositions carry agency + dialer, payments carry a gateway and no agency",
   pub[pub["event_type"].isin(["contact", "disposition"])][["agency_id", "dialer_id"]].notna().all().all()
   and pub[pub["event_type"] == "payment"]["gateway_id"].notna().all()
   and pub[pub["event_type"] == "payment"]["agency_id"].isna().all())
conn = set(pub[(pub["src"] == "contact") & (pub["outcome"] == "connected")]["chain"])
disp = set(pub[pub["src"] == "dispo"]["chain"])
ok("public: every connected call has a disposition and vice versa", conn == disp, f"{len(conn):,} / {len(disp):,}")
kept = pub[(pub["src"] == "ptp_res") & (pub["outcome"] == "kept")]["chain"]
paid = set(pub[pub["src"] == "ptp_pay"]["chain"])
ok("public: a kept PTP comes with a payment (99%+)", kept.isin(paid).mean() > .99, f"{kept.isin(paid).mean():.3f}")
hid_pub = pd.concat([pub, clean]).sort_values("ts")

# ------------------------------------------------------------------ seasonality is real
c = pub[pub["event_type"] == "contact"]
daily = c.groupby(c["ts"].dt.date).size()
d = lambda s: T(s).date()  # noqa: E731
weekday_base = np.mean([daily[d(x)] for x in ["2026-10-06", "2026-10-07", "2026-10-13", "2026-10-14", "2026-10-21"]])
sun = np.mean([daily[d(x)] for x in ["2026-10-11", "2026-10-18", "2026-10-25"]])
ok("Sunday contact volume is about 35% of a normal weekday", .25 < sun / weekday_base < .5, f"{sun / weekday_base:.2f}")
me = np.mean([daily[d(x)] for x in ["2026-09-28", "2026-09-29", "2026-09-30"]]) / np.mean(
    [daily[d(x)] for x in ["2026-10-06", "2026-10-07", "2026-10-13", "2026-10-14"]])
ok("month-end days (Sep 28-30) have about 30% more contacts", 1.15 < me < 1.5, f"{me:.2f}")
tue = lambda city, x: ((c["city"] == city) & (c["ts"].dt.date == d(x))).sum()  # noqa: E731
kol_base = np.mean([tue("Kolkata", x) for x in ["2026-10-06", "2026-10-13", "2026-10-27"]])
ok("regional festival: Kolkata contacts on Oct 20 under 45% of a normal Tuesday", tue("Kolkata", "2026-10-20") / kol_base < .45,
   f"{tue('Kolkata', '2026-10-20') / kol_base:.2f}")
nat_base = np.mean([daily[d(x)] for x in ["2026-10-06", "2026-10-13", "2026-10-27"]])
ok("national festival: all-city contacts on Oct 20 under 60% of a normal Tuesday", daily[d("2026-10-20")] / nat_base < .6,
   f"{daily[d('2026-10-20')] / nat_base:.2f}")
p = pub[(pub["event_type"] == "payment") & (pub["src"] == "self_pay")]
pd_ = p.groupby(p["ts"].dt.date).size()
sal = np.mean([pd_[d(x)] for x in ["2026-10-01", "2026-10-02", "2026-10-05"]]) / np.mean(
    [pd_[d(x)] for x in ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-21"]])
ok("salary days (1st-5th): self/EMI payments are well above mid-month", sal > 1.8, f"{sal:.2f}x")


# ------------------------------------------------------------------ anomalies are really in the stream
def win_count(ev, cond, s, e, back=(7, 14)):
    """count in the window vs the mean of the same clock window on the same weekday 1 and 2 weeks earlier"""
    s, e = T(s), T(e)
    obs = (cond(ev) & (ev["ts"] >= s) & (ev["ts"] < e)).sum()
    base = np.mean([(cond(ev) & (ev["ts"] >= s - pd.Timedelta(days=b)) & (ev["ts"] < e - pd.Timedelta(days=b))).sum()
                    for b in back])
    return obs, base


def fail_rate(ev, g, s, e, back=(7, 14)):
    cond = lambda x: (x["event_type"] == "payment") & (x["gateway_id"] == g)  # noqa: E731
    f = lambda x: cond(x) & (x["outcome"] == "failed")  # noqa: E731
    o_f, b_f = win_count(ev, f, s, e, back)
    o_n, b_n = win_count(ev, cond, s, e, back)
    return o_f / o_n, b_f / b_n


ok("public key has exactly the 7 practice anomalies (P1-P7), no hidden ids",
   sorted(key["anomaly_id"]) == [f"P{i}" for i in range(1, 8)])
ok("the practice anomalies cover all 6 kinds",
   set(key["kind"]) == {"dialer_outage", "agency_misdisposition", "gateway_failure", "feed_stall", "ptp_collapse",
                        "duplicate_events"})
ok("every practice anomaly ends by day 26, so its effects stay out of the hidden days", key["end_ts"].max() < "2026-10-24")
o, b = win_count(pub, lambda x: (x["src"] == "contact") & (x["city"] == "Hyderabad") & (x["dialer_id"] == "D3"),
                 "2026-10-15 11:00", "2026-10-15 14:00")
ok("P1 dialer outage (Hyderabad, D3): contacts fall by 70%+", o / b < .3, f"{o} vs normal {b:.0f}")
o, b = fail_rate(pub, "G4", "2026-10-19 15:00", "2026-10-19 18:00")
ok("P2 gateway failure (G4): failure rate above 40% vs under 15% normally", o > .4 and b < .15, f"{o:.2f} vs {b:.2f}")
o, b = win_count(pub, lambda x: (x["event_type"] == "contact") & (x["agency_id"] == "A5"), "2026-10-23 10:30", "2026-10-23 13:30")
ok("P3 duplicate events (A5): contact rows almost double", o / b > 1.7, f"{o} vs normal {b:.0f}")
cond_a2 = lambda x: (x["event_type"] == "disposition") & (x["agency_id"] == "A2") & (x["outcome"] == "ptp")  # noqa: E731
cond_c2 = lambda x: (x["event_type"] == "contact") & (x["agency_id"] == "A2") & (x["outcome"] == "connected")  # noqa: E731
o1, b1 = win_count(pub, cond_a2, "2026-10-14 10:00", "2026-10-14 16:00")
o2, b2 = win_count(pub, cond_c2, "2026-10-14 10:00", "2026-10-14 16:00")
ok("P4 mis-dispositioning (A2): PTP dispositions per connected call at least 1.8x normal", (o1 / o2) / (b1 / b2) > 1.8,
   f"{o1 / o2:.2f} vs {b1 / b2:.2f}")
l3 = pub[(pub["tenant_id"] == "L3") & (pub["ts"] >= T("2026-10-21 14:00")) & (pub["ts"] < T("2026-10-21 16:00"))]
ok("P5 feed stall (L3): events of the stall window all arrive after it ends", len(l3) > 100 and (l3["ingested_at"] >= T("2026-10-21 16:00")).all(),
   f"{len(l3)} events")
o, b = win_count(pub, lambda x: (x["src"] == "ptp_res") & (x["outcome"] == "kept") & (x["tenant_id"] == "L2")
                 & (x["product"] == "Two-Wheeler"), "2026-10-22 10:00", "2026-10-22 19:00")
ok("P6 PTP collapse (L2, Two-Wheeler): kept PTPs fall by 70%+", o / b < .3, f"{o} vs normal {b:.0f}")
o, b = win_count(pub, lambda x: (x["src"] == "contact") & (x["city"] == "Mumbai") & (x["dialer_id"] == "D1"),
                 "2026-10-09 12:00", "2026-10-09 16:00", back=(7,))
ok("P7 partial dialer outage (Mumbai, D1): contacts fall by about 50%", .25 < o / b < .6, f"{o} vs normal {b:.0f}")

ok("hidden key has the 6 seeded anomalies (H1-H6), all inside days 31-40",
   sorted(keyh["anomaly_id"]) == [f"H{i}" for i in range(1, 7)] and keyh["start_ts"].min() >= str(W.PUBLIC_END_TS)
   and keyh["end_ts"].max() < str(W.END_TS))
ok("hidden kinds cover outage, mis-disposition, gateway, feed stall, PTP collapse, duplicates",
   set(keyh["kind"]) == {"dialer_outage", "agency_misdisposition", "gateway_failure", "feed_stall", "ptp_collapse",
                         "duplicate_events"})
ok("hidden anomalies differ from the practice ones (different lender / segment)",
   not (set(zip(keyh["tenant_id"], keyh["segment"])) & set(zip(key["tenant_id"], key["segment"]))))
ok("injected hidden stream is identical to the clean one before the first anomaly",
   len(hid[hid["ts"] < T("2026-10-28 10:30")]) == len(clean[clean["ts"] < T("2026-10-28 10:30")])
   and (hid[hid["ts"] < T("2026-10-28 10:30")].groupby("outcome").size()
        == clean[clean["ts"] < T("2026-10-28 10:30")].groupby("outcome").size()).all())

full = pd.concat([pub, hid]).sort_values("ts")
o, b = win_count(full, lambda x: (x["src"] == "contact") & (x["city"] == "Pune") & (x["dialer_id"] == "D2"),
                 "2026-10-28 10:30", "2026-10-28 13:00")
ok("H1 dialer outage (Pune, D2): contacts fall by 70%+", o / b < .3, f"{o} vs normal {b:.0f}")
cond_a3 = lambda x: (x["event_type"] == "disposition") & (x["agency_id"] == "A3") & (x["outcome"] == "ptp")  # noqa: E731
cond_conn = lambda x: (x["event_type"] == "contact") & (x["agency_id"] == "A3") & (x["outcome"] == "connected")  # noqa: E731
o1, b1 = win_count(full, cond_a3, "2026-10-29 09:30", "2026-10-29 17:30")
o2, b2 = win_count(full, cond_conn, "2026-10-29 09:30", "2026-10-29 17:30")
ok("H2 mis-dispositioning (A3): PTP dispositions per connected call at least 1.8x normal", (o1 / o2) / (b1 / b2) > 1.8,
   f"{o1 / o2:.2f} vs {b1 / b2:.2f}")
o, b = fail_rate(full, "G2", "2026-10-30 14:00", "2026-10-30 17:00")
ok("H3 gateway failure (G2): failure rate above 35% vs under 15% normally", o > .35 and b < .15, f"{o:.2f} vs {b:.2f}")
l4 = hid[(hid["tenant_id"] == "L4") & (hid["ts"] >= T("2026-11-02 11:00")) & (hid["ts"] < T("2026-11-02 13:15"))]
ok("H4 feed stall (L4): events of the stall window all arrive after it ends", len(l4) > 100 and (l4["ingested_at"] >= T("2026-11-02 13:15")).all(),
   f"{len(l4)} events")
o, b = win_count(full, lambda x: (x["src"] == "ptp_res") & (x["outcome"] == "kept") & (x["tenant_id"] == "L1")
                 & (x["product"] == "Two-Wheeler"), "2026-11-03 10:00", "2026-11-03 19:00")
ok("H5 PTP collapse (L1, Two-Wheeler): kept PTPs fall by 70%+", o / b < .3, f"{o} vs normal {b:.0f}")
o, b = win_count(full, lambda x: (x["event_type"] == "contact") & (x["agency_id"] == "A6"), "2026-11-04 12:00", "2026-11-04 15:00")
ok("H6 duplicate events (A6): contact rows almost double", o / b > 1.7, f"{o} vs normal {b:.0f}")

# ------------------------------------------------------------------ the naive detector
# Time-of-day aware, but NOT weekday / salary / festival aware: alert when a segment's 15-min value is more than
# 3 standard deviations from the same slot over the previous 7 days, for 2 buckets in a row.
c = pub[pub["event_type"] == "contact"].copy()
c["day"] = ((c["ts"] - W.START_TS).dt.days).astype(int)
c["slot"] = c["ts"].dt.hour * 4 + c["ts"].dt.minute // 15
pay = pub[pub["event_type"] == "payment"].copy()
pay["day"] = ((pay["ts"] - W.START_TS).dt.days).astype(int)
pay["slot"] = pay["ts"].dt.hour * 4 + pay["ts"].dt.minute // 15
series = {}
for dim in ("city", "agency_id", "dialer_id"):
    for val, g in c.groupby(dim):
        a = np.zeros((W.PUBLIC_DAYS, 96))
        np.add.at(a, (g["day"].values, g["slot"].values), 1)
        series[("contacts", dim, val)] = (a, 1.0)
for val, g in pay.groupby("gateway_id"):
    n = np.zeros((W.PUBLIC_DAYS, 96)); f = np.zeros((W.PUBLIC_DAYS, 96))
    np.add.at(n, (g["day"].values, g["slot"].values), 1)
    np.add.at(f, (g[g["outcome"] == "failed"]["day"].values, g[g["outcome"] == "failed"]["slot"].values), 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(n >= 3, f / n, np.nan)
    series[("fail_rate", "gateway_id", val)] = (r, .05)
import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
ANOMALY_DAYS = {11, 16, 17, 21, 23, 24, 25}  # 0-based day index of the practice anomaly days (Oct 9, 14, 15, 19, 21, 22, 23)


def naive_runs(z_thr):
    runs = []  # (day, start slot, series key): 2 buckets in a row beyond z_thr standard deviations
    for k, (a, floor) in series.items():
        for dd in range(7, W.PUBLIC_DAYS):
            ref = a[dd - 7:dd]
            m, sd = np.nanmean(ref, 0), np.nanstd(ref, 0, ddof=1)
            hit = np.abs(np.nan_to_num((a[dd] - m) / np.maximum(sd, floor))) > z_thr
            for s in range(95):
                if hit[s] and hit[s + 1] and (s == 0 or not hit[s - 1]):
                    runs.append((dd, s, k))
    return runs


def evaluate(z_thr):
    runs = naive_runs(z_thr)
    per_day = pd.Series([r[0] for r in runs], dtype=int).value_counts().reindex(range(7, W.PUBLIC_DAYS), fill_value=0)
    normal = per_day[[x for x in per_day.index if x not in ANOMALY_DAYS]]

    def caught(day, start_slot, matches):  # an alert on the right segment within an hour of onset
        return any(r[0] == day and start_slot <= r[1] <= start_slot + 4 and matches(r[2]) for r in runs)

    got = [caught(17, 44, lambda k: k == ("contacts", "dialer_id", "D3") or k == ("contacts", "city", "Hyderabad")),
           caught(21, 60, lambda k: k == ("fail_rate", "gateway_id", "G4")),
           caught(25, 42, lambda k: k == ("contacts", "agency_id", "A5"))]
    return normal, got


print("\nnaive detector (same slot over the previous 7 days, 2 buckets in a row), by threshold:")
best_ok = False
for z in (2.0, 2.5, 3.0, 4.0):
    normal, got = evaluate(z)
    sun = [x for x in normal.index if (W.START_TS + pd.Timedelta(days=x)).weekday() == 6]
    print(f"  {z:.1f} sd: {normal.mean():5.1f} false alerts/day (Sundays {normal[sun].mean():.1f}, worst day {normal.max()}), "
          f"practice anomalies caught within an hour: {sum(got)} of 3")
    best_ok |= (sum(got) == 3 and normal.mean() <= 3)
ok("naive fixed threshold has no setting that catches all 3 practice anomalies with at most 3 false alerts/day "
   "(so seasonality-aware detection is needed)", not best_ok)

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
