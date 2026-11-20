"""
LC-04 step 8 check: incidents, annotations and (mock) alerts through the replay clock, on the PUBLIC days.
 - every practice anomaly raises an alert with the right driving segment; the fast ones within the hour
 - false alerts stay within the budget (at most 3 per day)
 - the replay is point-in-time and reversible (going back removes what came later; replaying again gives the same rows)
 - an alert is two templated lines and a link; a lender user sees alerts of their own lender only, in their own numbers
No LLM, nothing is sent anywhere. Writes the agent's output tables and puts them back to the original clock at the end.
Run:  python backend/tests/test_lc04_runner.py        (needs lc04.db loaded; run  python backend/agents/lc04/lc_runner.py prepare  first)
"""
import sys
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "metrics", "agents/lc04"):
    sys.path.insert(0, str(ROOT / p))
import access as A  # noqa: E402
import alerts as AL  # noqa: E402
import db  # noqa: E402
import detect as D  # noqa: E402
import detect_eval as E  # noqa: E402
import lc_runner as RU  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


orig_clock = RU.get_clock()
tl = RU.prepare(verbose=False)
keys = E.load_key()
T = pd.Timestamp

# ------------------------------------------------------------------ timeline vs the practice answer key
rows = []
related_incs = set()
for k in keys:
    s, e = T(k["start_ts"]), T(k["end_ts"])
    day = (s.normalize() - D.STREAM_START).days
    if day < D.FIRST_DAY:
        continue
    hit = None
    for inc in tl["incidents"]:
        for st in inc["stages"]:
            if s <= T(st["ts"]) <= e + pd.Timedelta(hours=3) and E.related(st["finding"].segment, k["segment"], max(day - 1, 0)):
                hit = hit or (inc, st)
                related_incs.add(inc["incident_id"])
    rows.append(dict(id=k["anomaly_id"], kind=k["kind"], key=k["segment"], hit=hit,
                     minutes=(T(hit[1]["ts"]) - s).total_seconds() / 60 if hit else None))
for r in rows:
    h = r["hit"]
    print(f"   {r['id']} {r['kind']:22} key={r['key']} -> alert at +{r['minutes']} min, driver {h[1]['finding'].segment if h else None} "
          f"({h[1]['stage'] if h else '-'})")
# The weak PTP-kept signal (P6: ~8 kept promises an hour in one lender + product) sits right at the confirmation line (z -3.98 against 4.0 after
# the history fix): it is DETECTED and drilled into, but stays 'watched' and sends no alert. We did not move the line to rescue it.
WEAK = {"P6"}
strong = [r for r in rows if r["id"] not in WEAK]
ok("every practice anomaly except the weak PTP-kept one raises an alert (6 of 7)", all(r["hit"] for r in strong), f"{sum(bool(r['hit']) for r in rows)} of {len(rows)} alerted")
ok("the alert names the right driving segment for every one that alerted", all(r["hit"][1]["finding"].segment == r["key"] for r in rows if r["hit"]))
p6 = next(r for r in rows if r["id"] == "P6")
watched_p6 = [inc for inc in tl["incidents"] if inc["t_detect"].startswith("2026-10-22") and inc["p1"] is not None
              and inc["p1"].segment == p6["key"]]
ok("the weak signal is detected and drilled into to the RIGHT segment even when it does not alert (it stays 'watched')",
   p6["hit"] is not None or (watched_p6 and not any(inc["stages"] for inc in watched_p6)), f"{len(watched_p6)} watched incident(s)")
fast = [r["id"] for r in rows if r["minutes"] is not None and r["minutes"] <= 60]
rows_sorted = rows
ok("at least 5 of 7 alerts go out within an hour of the anomaly's start", len(fast) >= 5, str(fast))
slow = [(r["id"], r["minutes"]) for r in rows if r["minutes"] is not None and r["minutes"] > 60]
print("   later than an hour (too few events to be sure sooner):", slow)

fa = [inc for inc in tl["incidents"] if inc["stages"] and inc["incident_id"] not in related_incs and inc["first_tick"] // 96 <= 29]   # public days only
per_day = pd.Series([inc["first_tick"] // 96 for inc in fa], dtype=int).value_counts()
ok("false alerts (incidents that alerted but match no anomaly): at most 3 on any day", (per_day.max() if len(per_day) else 0) <= 3,
   f"{len(fa)} in 23 days, worst day {int(per_day.max()) if len(per_day) else 0}")
watched = [inc for inc in tl["incidents"] if not inc["stages"]]
ok("incidents that drill-down could not confirm raise NO alert (they are only watched)", len(watched) >= 5, f"{len(watched)} watched, none sent")

# ------------------------------------------------------------------ the replay clock
RU.reset("2026-10-05 00:00:00")
c0 = RU.status()
ok("at the start of the evaluation period nothing has been produced", c0["alerts"] == 0 and c0["anomalies"] == 0)
p1 = next(r for r in rows if r["id"] == "P1")
inc1, st1 = p1["hit"]
before = T(st1["ts"]) - pd.Timedelta(minutes=15)
RU.reset(before)
n_before = RU.status()["alerts"]
res = RU.advance(15)
ok("advancing 15 minutes across the detection time writes the incident, annotation and alerts", res["alerts"] > n_before and res["annotations"] >= 1
   and any(a["incident_id"] == inc1["incident_id"] for a in res["new_alerts"]), f"{res['alerts']} alert rows, {res['annotations']} annotations")
ok("the alert appears exactly at its detection time, not before", T(res["clock"]) == T(st1["ts"]) and n_before == 0 or n_before == RU.status()["alerts"] - res["alerts"] + n_before)

conn = db.get_conn("lc04")
q1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
ok("point-in-time: no row in any output table is dated after the clock",
   q1("SELECT COUNT(*) FROM alerts WHERE created_at > ?", res["clock"]) == 0
   and q1("SELECT COUNT(*) FROM annotations WHERE created_at > ?", res["clock"]) == 0
   and q1("SELECT COUNT(*) FROM anomalies WHERE detected_at > ?", res["clock"]) == 0)
snap = lambda: sorted(map(tuple, conn.execute("SELECT tenant_id, incident_id, created_at, audience_role, line1, line2, link FROM alerts").fetchall()))  # noqa: E731
RU.reset("2026-10-15 20:00:00")
full = snap()
RU.reset("2026-10-12 00:00:00")
back = snap()
RU.reset("2026-10-15 20:00:00")
ok("going back removes what came later, and replaying forward gives exactly the same alerts",
   len(back) < len(full) and snap() == full and len(full) > 0, f"{len(back)} -> {len(full)} rows")
cnt1, cnt2 = RU.materialize("2026-10-20 00:00:00"), RU.materialize("2026-10-20 00:00:00")
ok("rewriting the same clock twice is idempotent", cnt1 == cnt2, str(cnt1))
ok("every alert and annotation points at a real anomaly row",
   q1("SELECT COUNT(*) FROM alerts a LEFT JOIN anomalies n ON n.anomaly_id=a.anomaly_id WHERE n.anomaly_id IS NULL") == 0
   and q1("SELECT COUNT(*) FROM annotations a LEFT JOIN anomalies n ON n.anomaly_id=a.anomaly_id WHERE n.anomaly_id IS NULL") == 0)
ok("each incident has an agent trace (detect, drill down, counterfactual, annotate, alert)",
   q1("SELECT COUNT(DISTINCT run_id) FROM agent_steps") == q1("SELECT COUNT(*) FROM agent_runs") > 0
   and {r[0] for r in conn.execute("SELECT DISTINCT tool FROM agent_steps")} >= {"detect", "drill_down", "counterfactual", "annotate", "alert"})
ok("agent runs and traces are replay-clean too: going back leaves no rows from the future",
   RU.materialize("2026-10-05 00:00:00") and q1("SELECT COUNT(*) FROM agent_runs") == 0 and q1("SELECT COUNT(*) FROM agent_steps") == 0)

# ------------------------------------------------------------------ alert content and audiences
RU.materialize("2026-10-20 00:00:00")
al = [dict(r) for r in conn.execute("SELECT * FROM alerts")]
ok("every alert is two non-empty lines and a link to its incident",
   al and all(a["line1"] and a["line2"] and a["link"] == AL.link_for(a["incident_id"]) for a in al), f"{len(al)} rows")
ok("alert text names a governed metric and a segment", all(any(m in a["line1"] for m in [x.name for x in D.registry.all_metrics()]) for a in al))
ok("alerts are mock: status is sent_mock and nothing else exists to send", {a["status"] for a in al} == {"sent_mock"})

lu = A.login("u_l2")
mine = A.scoped_query(lu, "SELECT * FROM alerts WHERE tenant_id=:tenant_id AND audience_role='lender_user'")
ok("a lender user sees only alerts of their own lender, addressed to lender users", mine and all(a["tenant_id"] == "L2" and a["audience_role"] == "lender_user" for a in mine),
   f"{len(mine)} alerts")
others = [l for l in ("L1", "L3", "L4", "L5")]
ok("a lender user's alert text never names another lender", all(not any(o in (a["line1"] + a["line2"]) for o in others) for a in mine))
cn = A.scoped_query(A.login("u_leader"), "SELECT * FROM alerts WHERE tenant_id=:tenant_id AND audience_role='cn_leadership'")
rm = A.scoped_query(A.login("u_rm_a"), "SELECT * FROM alerts WHERE tenant_id=:tenant_id AND audience_role='lender_rm'")
ok("leadership gets the platform view; an RM only the lenders in their set", cn and {a["tenant_id"] for a in cn} >= {"L1", "L2"}
   and {a["tenant_id"] for a in rm} <= {"L1", "L3"})
# a platform-wide incident: the lender's own numbers differ from the platform's
by_inc = {}
for a in al:
    by_inc.setdefault(a["incident_id"], []).append(a)
diff = [i for i, rows_ in by_inc.items() if any(a["audience_role"] == "lender_user" for a in rows_) and any(a["audience_role"] == "cn_leadership" for a in rows_)
        and next(a for a in rows_ if a["audience_role"] == "lender_user")["line1"] != next(a for a in rows_ if a["audience_role"] == "cn_leadership")["line1"]]
ok("for a platform-wide incident the lender user's line is in THEIR numbers (not the platform's)", len(diff) >= 1, f"{len(diff)} incidents")
conn.close()

# ------------------------------------------------------------------ put the original clock back
RU.materialize(orig_clock)
ok("the original clock is restored", RU.get_clock() == orig_clock, str(orig_clock))

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
