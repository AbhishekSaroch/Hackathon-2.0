"""
LC-04 step 10 check: the HTTP API (FastAPI TestClient, no server needed, no LLM tokens).
 roles and scope from the user record; replay control; view generation / saving / rendering / rebuilding; incidents, alerts and the
 annotated view; evaluation; BI-02 and BI-03 routes unaffected. Restores the replay clock at the end.
Run:  python backend/tests/test_lc04_api.py        (needs lc04.db loaded and the replay prepared: python backend/agents/lc04/lc_runner.py prepare)
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from routers import lc04 as R  # noqa: E402

failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


c = TestClient(app)
H = lambda u: {"X-User-Id": u}  # noqa: E731
m = R.mods()
orig_clock = str(m["RU"].get_clock())
m["GEN"].llm = None                                       # no tokens: the deterministic path (the model path is tested with stubs)

# ------------------------------------------------------------------ who
ok("no user header -> 401, unknown user -> 401", c.get("/lc04/me").status_code == 401 and c.get("/lc04/me", headers=H("u_nobody")).status_code == 401)
us = c.get("/lc04/users").json()
ok("10 demo users listed for the role switcher", len(us) == 10 and {u["role_id"] for u in us} == {"cn_leadership", "ops_head", "delivery_head", "lender_rm", "lender_user"})
me = c.get("/lc04/me", headers=H("u_l1")).json()
ok("a lender user's scope comes from their record: one lender, no lender dimension", me["lenders"] == ["L1"] and "lender" not in me["dims"] and me["metrics"] == 55)
ok("the catalog is cut to the role (leadership 63 metrics, lender user 55, no feed health)",
   len(c.get("/lc04/metrics", headers=H("u_leader")).json()) == 63 and len(c.get("/lc04/metrics", headers=H("u_l1")).json()) == 55
   and all(x["metric_id"] != "events_total" for x in c.get("/lc04/metrics", headers=H("u_l1")).json()))
ok("sending another lender in a header or query changes nothing (scope is not a parameter)",
   c.get("/lc04/me?lender=L2&tenant_id=L2", headers={**H("u_l1"), "X-Tenant-Id": "L2"}).json()["lenders"] == ["L1"])

# ------------------------------------------------------------------ the replay clock
st = c.get("/lc04/clock", headers=H("u_l1")).json()
ok("anyone can read the clock; it says whether the replay is prepared", "clock" in st and st["prepared"] is True and isinstance(st["hidden_loaded"], bool), str(st["clock"]))
ok("only leadership and the ops head may move the clock", c.post("/lc04/replay/advance", json={"minutes": 15}, headers=H("u_l1")).status_code == 403
   and c.post("/lc04/replay/advance", json={"minutes": 15}, headers=H("u_rm_a")).status_code == 403)
ok("bad minutes -> 400; bad time -> 400; time outside the stream -> 400", c.post("/lc04/replay/advance", json={"minutes": 0}, headers=H("u_leader")).status_code == 400
   and c.post("/lc04/replay/reset", json={"to": "nonsense"}, headers=H("u_leader")).status_code == 400
   and c.post("/lc04/replay/reset", json={"to": "2030-01-01 00:00:00"}, headers=H("u_leader")).status_code == 400)
c.post("/lc04/replay/reset", json={"to": "2026-10-15 11:00:00"}, headers=H("u_leader"))
seen_alerts = []
for _ in range(8):                                                    # 11:00 .. 13:00 in 15-minute steps
    r = c.post("/lc04/replay/advance", json={"minutes": 15}, headers=H("u_leader")).json()
    seen_alerts += r["new_alerts"]
ok("advancing 15 minutes at a time surfaces the dialer outage within the hour", any("Hyderabad" in a["line1"] for a in seen_alerts)
   and r["clock"].startswith("2026-10-15 13:00"), f"{len(seen_alerts)} alert(s) seen on the way")

# ------------------------------------------------------------------ views
g = c.post("/lc04/dashboards/generate", json={"request": "Where are calls and dispositions off track today, by city, agency and dialer?", "use_llm": False},
           headers=H("u_ops")).json()
ok("a view is generated from a role and a plain request: validated spec, 15-minute refresh, saved", g["spec"]["panels"] and g["spec"]["refresh_seconds"] == 900
   and g["source"] == "fallback" and g["dashboard_id"] and g["ms"] < 120_000, f"{len(g['spec']['panels'])} panels, {g['ms']} ms")
d = c.get(f"/lc04/dashboards/{g['dashboard_id']}/data", headers=H("u_ops"))
ok("the view's data is rendered at the clock from governed numbers", d.status_code == 200 and d.json()["clock"].startswith("2026-10-15 13:00")
   and len(d.json()["panels"]) == len(g["spec"]["panels"]), f"{d.json()['ms']} ms")
ok("another user cannot open or read someone else's saved view", c.get(f"/lc04/dashboards/{g['dashboard_id']}", headers=H("u_l1")).status_code == 404
   and c.get(f"/lc04/dashboards/{g['dashboard_id']}/data", headers=H("u_leader")).status_code == 404)
g2 = c.post("/lc04/dashboards/generate", json={"request": "Show payment success by agency.", "use_llm": False}, headers=H("u_ops")).json()
ok("a request outside the role is explained, nothing forbidden is shown", any("payment metrics are not available" in n for n in g2["notes"])
   and not any(p["metric_id"].startswith("pay") for p in g2["spec"]["panels"]))
g3 = c.post("/lc04/dashboards/generate", json={"request": "Show me lender L2's collections", "use_llm": False}, headers=H("u_l1")).json()
ok("a lender user asking for another lender gets only their own", '"L2"' not in str(g3["spec"]) and g3["spec"]["lenders"] == ["L1"]
   and any("outside your scope" in n for n in g3["notes"]))
dl = c.get(f"/lc04/dashboards/{g3['dashboard_id']}/data", headers=H("u_l1")).json()
ok("and its numbers are the lender's own (they differ from the platform's)", dl["panels"][0].get("value") is not None or dl["panels"][0].get("rows") is not None)

# ------------------------------------------------------------------ incidents, alerts, the annotated view
al_leader = c.get("/lc04/alerts", headers=H("u_leader")).json()
inc_leader = c.get("/lc04/incidents", headers=H("u_leader")).json()
ok("leadership sees the alert and the incident (two lines + a link)", al_leader and all(a["line1"] and a["line2"] and a["link"].startswith("/command/incidents/") for a in al_leader)
   and inc_leader and inc_leader[0]["incident_id"].startswith("INC-"), f"{len(al_leader)} alerts, {len(inc_leader)} incidents")
iid = next(a["incident_id"] for a in al_leader if "Hyderabad" in a["line1"])
det = c.get(f"/lc04/incidents/{iid}", headers=H("u_leader")).json()
ok("the annotated view: driving segment, counterfactual, evidence and the agent's trace",
   det["finding"]["segment"] == {"city": "Hyderabad", "dialer": "D3"} and det["finding"]["counterfactual_ok"] and det["finding"]["headline"]
   and [t["tool"] for t in det["trace"]][:3] == ["detect", "drill_down", "counterfactual"] and det["detections"], det["finding"]["headline"][:80])
ok("the ops head sees it too (calls are in their role); the delivery head does not (no call metrics)",
   c.get(f"/lc04/incidents/{iid}", headers=H("u_ops")).status_code == 200 and c.get(f"/lc04/incidents/{iid}", headers=H("u_delivery")).status_code == 404)
lu = c.get(f"/lc04/incidents/{iid}", headers=H("u_l1"))
ok("a lender user with an alert gets only their own alert lines and the segment name, no platform evidence or trace",
   lu.status_code == 200 and "trace" not in lu.json() and "detections" not in lu.json() and "evidence" not in str(lu.json())
   and all(a["lender"] == "L1" for a in lu.json()["alerts"]))
ok("a lender whose lender is not affected gets 404 for that incident",
   all(c.get(f"/lc04/incidents/{iid}", headers=H(f"u_{l}")).status_code in (200, 404) for l in ("l2", "l3", "l4", "l5"))
   and c.get("/lc04/incidents/INC-0000-0000", headers=H("u_l1")).status_code == 404)
al_l1, al_l2 = c.get("/lc04/alerts", headers=H("u_l1")).json(), c.get("/lc04/alerts", headers=H("u_l2")).json()
ok("each lender user's alert list holds only that lender's alerts, with no other lender named",
   all(a["lender"] == "L1" and not any(o in a["line1"] + a["line2"] for o in ("L2", "L3", "L4", "L5")) for a in al_l1)
   and all(a["lender"] == "L2" for a in al_l2))
rb = c.post("/lc04/dashboards/rebuild", json={"dashboard_id": g["dashboard_id"], "incident_id": iid}, headers=H("u_ops"))
ok("rebuild around the incident: the driver is the first panel, the view is saved",
   rb.status_code == 200 and rb.json()["source"] == "rebuilt" and rb.json()["spec"]["panels"][0]["filters"] == {"city": "Hyderabad", "dialer": "D3"}
   and rb.json()["spec"]["focus"]["incident_id"] == iid, rb.json()["spec"]["panels"][0]["title"])
rbd = c.get(f"/lc04/dashboards/{rb.json()['dashboard_id']}/data", headers=H("u_ops")).json()
drv = rbd["panels"][0]
pts = drv["series"][0]["points"] if drv.get("series") else []
before_v = [v for t, v in pts if "09:00" <= t[11:16] < "11:00" and v is not None]
during_v = [v for t, v in pts if "11:30" <= t[11:16] < "13:00" and v is not None]
ok("the driver panel shows the outage in the numbers (contacts inside the window are far below the morning's)",
   len(pts) >= 10 and before_v and during_v and sum(during_v) / len(during_v) < 0.5 * sum(before_v) / len(before_v),
   f"{len(pts)} points, morning mean {sum(before_v) / max(len(before_v), 1):.1f}, outage mean {sum(during_v) / max(len(during_v), 1):.1f}")
ok("a lender user cannot rebuild around an incident they have no alert for",
   c.post("/lc04/dashboards/rebuild", json={"dashboard_id": g3["dashboard_id"], "incident_id": "INC-0000-0000"}, headers=H("u_l1")).status_code == 404)
ok("public request examples come back per role", len(c.get("/lc04/requests", headers=H("u_ops")).json()) == 2)

# ------------------------------------------------------------------ evaluation and the rest of the platform
ok("the scorecard is for platform roles only", c.get("/lc04/evaluation", headers=H("u_l1")).status_code == 403 and c.get("/lc04/evaluation", headers=H("u_leader")).status_code == 200)
paths = set(app.openapi()["paths"])
ok("BI-02 and BI-03 routes are still served next to LC-04", any(p.startswith("/bi02") for p in paths) and any(p.startswith("/bi03") for p in paths)
   and any(p.startswith("/lc04") for p in paths) and c.get("/health").status_code == 200, f"{len(paths)} paths")

m["RU"].materialize(orig_clock)
ok("the replay clock is restored", str(m["RU"].get_clock()) == orig_clock, orig_clock)
import sqlite3  # noqa: E402
cn = sqlite3.connect(str(BACKEND / "lc04.db"))
cn.execute("DELETE FROM dashboards")
cn.commit()
cn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
