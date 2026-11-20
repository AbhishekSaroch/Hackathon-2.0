"""
LC-04 compliance check: legal communications sent outside the permitted 08:00-20:00 window (the Nov 20 06:17 demo scenario).
 - the window rule on the boundary (07:59:59 out, 08:00:00 in, 19:59:59 in, 20:00:00 out) and the 'legal' event type
 - the seeded scenario is what we say it is, numbers via the governed metrics, reconciled to the raw events
 - the incident appears when the sender's log ARRIVES (06:15), not at 03:00 and not before; alerts go to the right roles and lenders only
 - a lender user sees only their own lender's count; the delivery head (no compliance metrics) hears nothing
 - the statistical evaluation (hidden scorecard) is untouched and no legal event pollutes the other days
No LLM. Writes temporary rows (tenant TEST_LC), removes/re-seeds the demo scenario once, and restores the replay clock.
Run:  python backend/tests/test_lc04_compliance.py        (needs lc04.db migrated + seeded + the replay prepared)
"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parent.parent
for p in ("", "db", "metrics", "agents/lc04", "data"):
    sys.path.insert(0, str(BACKEND / p))
from fastapi.testclient import TestClient  # noqa: E402

import access as A  # noqa: E402
import compliance as CP  # noqa: E402
import compute as C  # noqa: E402
import cube  # noqa: E402
import db  # noqa: E402
import lc_runner as RU  # noqa: E402
import seed_demo_legal as S  # noqa: E402
from main import app  # noqa: E402
from routers import lc04 as R  # noqa: E402

failed = 0
ALL = C.Scope(("L1", "L2", "L3", "L4", "L5"))


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


conn = db.get_conn("lc04")
q1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
orig_clock = str(RU.get_clock())
if not q1("select count(*) from lc_events where event_type='legal'"):
    S.seed()

# ------------------------------------------------------------------ the rule on the boundary + the event type
try:
    db.insert("TEST_LC", "lc_events", {"account_id": "NOPE", "ts": "2026-12-01 03:00:00", "ingested_at": "2026-12-01 03:00:05", "event_type": "sms"}, "lc04")
    ok("an unknown event type is still refused (only 'legal' was added)", False)
except Exception:
    ok("an unknown event type is still refused (only 'legal' was added)", True)
conn.execute("DELETE FROM lc_events WHERE tenant_id='TEST_LC'")
conn.execute("DELETE FROM lc_accounts WHERE tenant_id='TEST_LC'")
conn.execute("DELETE FROM cube_15m WHERE tenant_id='TEST_LC'")
conn.execute("INSERT INTO lc_accounts (account_id, tenant_id, product, bucket, city, agency_id) VALUES ('TEST_LC_1','TEST_LC','Two-Wheeler','90+','Pune','A1')")
conn.commit()
for hms, expect_out in [("00:00:00", 1), ("07:59:59", 1), ("08:00:00", 0), ("12:00:00", 0), ("19:59:59", 0), ("20:00:00", 1), ("23:59:59", 1)]:
    db.insert("TEST_LC", "lc_events", {"account_id": "TEST_LC_1", "ts": f"2026-12-01 {hms}", "ingested_at": f"2026-12-01 {hms}", "event_type": "legal",
                                       "outcome": "sent", "channel": "legal_notice", "agency_id": "A1"}, "lc04")
cube.build_range("TEST_LC", "2026-12-01 00:00:00", "2026-12-02 00:00:00")
rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT substr(bucket_ts,12,5), legal_sent, legal_out_of_window FROM cube_15m WHERE tenant_id='TEST_LC'")}
ok("the window rule on the boundary: 07:59:59 out, 08:00:00 in, 19:59:59 in, 20:00:00 out (also 00:00 and 23:59:59 out)",
   rows == {"00:00": (1, 1), "07:45": (1, 1), "08:00": (1, 0), "12:00": (1, 0), "19:45": (1, 0), "20:00": (1, 1), "23:45": (1, 1)}, str(rows))
ok("a notice's window is judged on when it was SENT, not on when the log arrived",
   q1("SELECT legal_out_of_window FROM cube_15m WHERE tenant_id='TEST_LC' AND bucket_ts='2026-12-01 07:45:00'") == 1)
conn.execute("DELETE FROM lc_events WHERE tenant_id='TEST_LC'")
conn.execute("DELETE FROM lc_accounts WHERE tenant_id='TEST_LC'")
conn.execute("DELETE FROM cube_15m WHERE tenant_id='TEST_LC'")
conn.commit()

# ------------------------------------------------------------------ the seeded scenario, via the governed metrics
g = lambda m, s, e, **kw: C.compute(m, ALL, s, e, **kw)  # noqa: E731
ok("the scenario: 38 legal notices outside the window on Nov 20 (the governed metric), none on the normal days",
   g("legal_notices_out_of_window", "2026-11-20 00:00:00", "2026-11-21 00:00:00")[0]["value"] == 38
   and g("legal_notices_out_of_window", "2026-11-17 00:00:00", "2026-11-20 00:00:00")[0]["value"] == 0)
ok("normal days: 135 notices, all inside the window; on Nov 20 the share outside the window is 100%",
   g("legal_notices_in_window", "2026-11-17 00:00:00", "2026-11-20 00:00:00")[0]["value"] == 135
   and g("legal_out_of_window_rate", "2026-11-20 00:00:00", "2026-11-21 00:00:00")[0]["value"] == 100.0
   and g("legal_out_of_window_rate", "2026-11-17 00:00:00", "2026-11-20 00:00:00")[0]["value"] == 0.0)
by_ag = {r["agency"]: r["value"] for r in g("legal_notices_out_of_window", "2026-11-20 00:00:00", "2026-11-21 00:00:00", group_by=["agency"]) if r["value"]}
by_l = {r["lender"]: r["value"] for r in g("legal_notices_out_of_window", "2026-11-20 00:00:00", "2026-11-21 00:00:00", group_by=["lender"]) if r["value"]}
ok("all 38 came from one agency (A4) and went to two lenders (19 each)", by_ag == {"A4": 38.0} and by_l == {"L2": 19.0, "L3": 19.0}, f"{by_ag} {by_l}")
raw = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts), MIN(ingested_at), MAX(ingested_at) FROM lc_events WHERE event_type='legal' AND ts >= '2026-11-20'").fetchone()
ok("independent check on the raw events: 38 sent 03:02-03:19, all logged between 06:05 and 06:15",
   raw[0] == 38 and "03:02" <= raw[1][11:16] and raw[2][11:16] <= "03:19" and "06:05" <= raw[3][11:16] and raw[4][11:16] <= "06:15", str(tuple(raw)))
ok("legal events exist only on the demo days; no other day is touched",
   q1("SELECT COUNT(*) FROM cube_15m WHERE legal_sent > 0 AND day NOT IN ('2026-11-17','2026-11-18','2026-11-19','2026-11-20')") == 0)
ok("the statistical stream is untouched: the cube still equals the non-legal events for every stream day",
   q1("SELECT SUM(ev_total) FROM cube_15m WHERE bucket_ts < '2026-11-07'") == q1("SELECT COUNT(*) FROM lc_events WHERE event_type != 'legal' AND ingested_at < '2026-11-07'"))

# ------------------------------------------------------------------ remove and re-seed: reversible and deterministic
n0, c0, l0 = q1("SELECT COUNT(*) FROM lc_events"), q1("SELECT SUM(ev_total) FROM cube_15m"), q1("SELECT SUM(legal_out_of_window) FROM cube_15m")
removed = S.remove()
n1, c1, l1 = q1("SELECT COUNT(*) FROM lc_events"), q1("SELECT SUM(ev_total) FROM cube_15m"), q1("SELECT SUM(legal_out_of_window) FROM cube_15m")
ok("--remove deletes exactly the 173 legal events and their cube rows (the rest is as before)", removed == 173 and n1 == n0 - 173 and c1 == c0 - 173 and l1 == 0
   and q1("SELECT COUNT(*) FROM lc_meta WHERE key='demo_legal_day'") == 0)
S.seed()
ok("seeding again gives the identical scenario (same counts, same numbers)", q1("SELECT COUNT(*) FROM lc_events") == n0 and q1("SELECT SUM(ev_total) FROM cube_15m") == c0
   and q1("SELECT SUM(legal_out_of_window) FROM cube_15m") == l0 == 38)

# ------------------------------------------------------------------ the incident: found when the log arrives, not before
ok("at 06:00 (nothing has arrived yet) there is no incident, and the normal days raise none",
   CP.incidents("2026-11-20 06:00:00") == [] and CP.incidents("2026-11-20 00:00:00") == [])
inc = CP.incidents("2026-11-20 06:17:00")
ok("at 06:17 there is exactly one incident, found at 06:15 (when the batch log arrived), 3 hours after the sends",
   len(inc) == 1 and inc[0]["incident_id"] == "CMP-1120-0615" and inc[0]["t_detect"] == "2026-11-20 06:15:00" and inc[0]["count"] == 38)
i = inc[0]
ok("the driver is agency A4 (100%); the lenders affected are L2 and L3; the text states the window and both times",
   i["segment"] == {"agency": "A4"} and i["lenders"] == ["L2", "L3"] and "08:00-20:00" in i["line1"] and "03:0" in i["line1"] and "logged 06:" in i["line1"]
   and "agency A4" in i["line2"], i["line1"])
ok("numbers per lender add up to the total (19 + 19 = 38)", sum(v["count"] for v in i["per_lender"].values()) == 38)
ok("still one incident an hour later (not re-raised)", len(CP.incidents("2026-11-20 07:15:00")) == 1)

# ------------------------------------------------------------------ the replay writes it, point-in-time, to the right people
RU.reset("2026-11-20 06:00:00")
before = q1("SELECT COUNT(*) FROM alerts WHERE incident_id LIKE 'CMP-%'")
res = RU.advance(15)
ok("advancing 06:00 -> 06:15 writes the incident, its annotation and alerts; before that nothing",
   before == 0 and any("legal notices" in a["line1"] for a in res["new_alerts"]) and q1("SELECT COUNT(*) FROM anomalies WHERE incident_id='CMP-1120-0615'") == 1
   and q1("SELECT COUNT(*) FROM annotations a JOIN anomalies n ON n.anomaly_id=a.anomaly_id WHERE n.incident_id='CMP-1120-0615'") == 1, res["clock"])
RU.reset("2026-11-20 06:17:00")
ok("no row of the incident is dated after the clock", q1("SELECT COUNT(*) FROM alerts WHERE incident_id='CMP-1120-0615' AND created_at > '2026-11-20 06:17:00'") == 0)
al = [dict(r) for r in conn.execute("SELECT tenant_id, audience_role, line1, line2, link FROM alerts WHERE incident_id='CMP-1120-0615'")]
ok("alerts go to leadership, ops head, RM and lender user of L2 and L3 only (8 rows); nobody else: no delivery head, no L1 / L4 / L5",
   len(al) == 8 and {a["tenant_id"] for a in al} == {"L2", "L3"} and {a["audience_role"] for a in al} == {"cn_leadership", "ops_head", "lender_rm", "lender_user"},
   str(sorted({(a['tenant_id'], a['audience_role']) for a in al}))[:120])
seen = lambda u: A.scoped_query(A.login(u), "SELECT line1, line2 FROM alerts WHERE tenant_id=:tenant_id AND incident_id='CMP-1120-0615' AND audience_role=:r",  # noqa: E731
                                {"r": A.login(u).role_id})
ok("a lender user sees only their own lender's count (19), never the total, never the other lender",
   all("19 legal notices" in r["line1"] and "38" not in r["line1"] + r["line2"] and not any(o in r["line1"] + r["line2"] for o in ("L2", "L3", "L1", "L4", "L5"))
       for u in ("u_l2", "u_l3") for r in seen(u)) and seen("u_l2") and seen("u_l3") and not seen("u_l1") and not seen("u_l4"))
ok("leadership and the ops head get the platform view (38, both lenders); the delivery head gets nothing",
   all("38 legal notices" in r["line1"] and "L2, L3" in r["line2"] for u in ("u_leader", "u_ops") for r in seen(u))
   and seen("u_leader") and seen("u_ops") and not seen("u_delivery"))
ok("an RM sees only the lender of their set that was affected (RM A: L3; RM B: L2)", len(seen("u_rm_a")) >= 1 and len(seen("u_rm_b")) >= 1
   and all("19 legal" in r["line1"] for r in seen("u_rm_a") + seen("u_rm_b")))

# ------------------------------------------------------------------ API and views
c = TestClient(app)
H = lambda u: {"X-User-Id": u}  # noqa: E731
al_leader = c.get("/lc04/alerts", headers=H("u_leader")).json()
ok("leadership's alert list has the legal-window alert (two lines and a link)", any("legal notices" in a["line1"] and a["link"] == "/command/incidents/CMP-1120-0615" for a in al_leader))
d = c.get("/lc04/incidents/CMP-1120-0615", headers=H("u_leader")).json()
ok("the annotated view: the rule, the counts, who sent them, and the agent's trace (detect, drill down, counterfactual, annotate, alert)",
   d["finding"]["rule"]["count"] == 38 and d["finding"]["segment"] == {"agency": "A4"} and d["finding"]["counterfactual"]["kind"] == "rule"
   and [t["tool"] for t in d["trace"]] == ["detect", "drill_down", "counterfactual", "annotate", "alert"] and d["detections"])
ok("the ops head sees the incident; the delivery head and lenders L1 / L4 get 404",
   c.get("/lc04/incidents/CMP-1120-0615", headers=H("u_ops")).status_code == 200 and c.get("/lc04/incidents/CMP-1120-0615", headers=H("u_delivery")).status_code == 404
   and all(c.get("/lc04/incidents/CMP-1120-0615", headers=H(u)).status_code == 404 for u in ("u_l1", "u_l4", "u_l5")))
dl = c.get("/lc04/incidents/CMP-1120-0615", headers=H("u_l2")).json()
ok("a lender user's page has their own alert lines and the sender, but no totals, no rule counts, no trace",
   "trace" not in dl and "rule" not in dl["finding"] and dl["finding"]["segment"] == {"agency": "A4"} and all(a["lender"] == "L2" for a in dl["alerts"]) and "38" not in json.dumps(dl))
gen = c.post("/lc04/dashboards/generate", json={"request": "Show legal notices sent outside the allowed window by agency", "use_llm": False}, headers=H("u_ops")).json()
ok("a plain request about legal notices builds a compliance view (governed metrics only)",
   any(p["metric_id"].startswith("legal_notices_out") for p in gen["spec"]["panels"]) and all(p["metric_id"] in R.mods()["R"].METRICS for p in gen["spec"]["panels"]))
data = c.get(f"/lc04/dashboards/{gen['dashboard_id']}/data", headers=H("u_ops")).json()
kpi = next(p for p in data["panels"] if p["type"] == "kpi" and p["metric_id"] == "legal_notices_out_of_window")
ok("at 06:17 the KPI shows 38 for the platform", kpi["value"] == 38 and data["clock"].startswith("2026-11-20 06:17"), str(kpi["value"]))
gl = c.post("/lc04/dashboards/generate", json={"request": "", "use_llm": False}, headers=H("u_l2")).json()
dl2 = c.get(f"/lc04/dashboards/{gl['dashboard_id']}/data", headers=H("u_l2")).json()
kpi_l = next(p for p in dl2["panels"] if p["type"] == "kpi" and p["metric_id"] == "legal_notices_out_of_window")
ok("a lender user's default view shows THEIR count (19), not 38", kpi_l["value"] == 19)
rb = c.post("/lc04/dashboards/rebuild", json={"dashboard_id": gen["dashboard_id"], "incident_id": "CMP-1120-0615"}, headers=H("u_ops")).json()
ok("rebuilding a view around the incident puts agency A4's legal notices first", rb["spec"]["panels"][0]["filters"] == {"agency": "A4"}
   and rb["spec"]["panels"][0]["metric_id"] == "legal_notices_out_of_window")

# ------------------------------------------------------------------ the official evaluation is untouched
rep = json.loads((BACKEND / "data" / "lc04_eval_report.json").read_text())
ledger = (BACKEND / "data" / "lc04_hidden_eval_ledger.json").read_text()
ok("the hidden scorecard is as it was logged (3 of 6 official) and the seed is in the ledger as a demo, not as evaluation data",
   rep["hidden"]["summary"]["detected"] == 3 and "demo_legal_seeded" in ledger and ledger.count('"hidden_evaluation_run"') == 1)

conn.execute("DELETE FROM dashboards")
conn.commit()
RU.materialize(orig_clock)
ok("the replay clock is restored", str(RU.get_clock()) == orig_clock, orig_clock)
conn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
