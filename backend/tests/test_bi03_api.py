"""
BI-03 API tests.   python test_bi03_api.py   (exit code 1 on any failure)

  A read      every read endpoint on the real tenant answers, shapes are right, nothing is shown from after the replay pointer
  B isolation a second tenant sees none of the first tenant's rows (and the other way round)
  C actions   on a small separate tenant: approve (and the 409s), skip, gate tester, advance/reset consistency, guards
  D safety    the real tenant is not changed by any of this, BI-02 routes are still there, a broken BI-03 import cannot stop BI-02
"""
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import outcomes as O  # noqa: E402
import runner as R  # noqa: E402

T, TEN = "abc_bank", "t_api"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


client = TestClient(main.app)
get = lambda path, t=T, **p: client.get(path, params=p, headers={"X-Tenant-Id": t})
post = lambda path, body=None, t=T: client.post(path, json=body, headers={"X-Tenant-Id": t})
cnt = lambda t, tbl: db.query_one(t, f"SELECT COUNT(*) n FROM {tbl} WHERE tenant_id=:tenant_id", database="bi03")["n"]
real_before = {t: cnt(T, t) for t in ("flags", "nudges", "risk_scores", "flag_outcomes", "thresholds", "agent_runs")}
active_before = db.query_one(T, "SELECT threshold_version v FROM thresholds WHERE tenant_id=:tenant_id AND is_active=1", database="bi03")["v"]

# ================================================================ A  read endpoints, real tenant
s = get("/bi03/summary")
ok("A summary answers with the replay state, flag/nudge counts and the active threshold", s.status_code == 200 and {"replay", "flags_by_status", "nudges_by_status", "active_threshold"} <= set(s.json()), str(s.status_code))
ptr = s.json()["replay"]["replay_date"]
fl = get("/bi03/flags", page_size=5).json()
ok("A flags list: paged, total matches the DB, rows carry account, signal, nudge and outcome", fl["total"] == db.query_one(T, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id AND flagged_on<=:p", {"p": ptr}, database="bi03")["n"]
   and len(fl["items"]) == 5 and {"customer_name", "dominant_signal", "nudge_status", "went_dpd1"} <= set(fl["items"][0]))
p2 = get("/bi03/flags", page_size=5, page=2).json()
ok("A flags list: page 2 is a different set of rows", not ({i["flag_id"] for i in fl["items"]} & {i["flag_id"] for i in p2["items"]}))
sc = get("/bi03/flags", sort="score", page_size=20).json()["items"]
ok("A flags list: sort=score is descending", [i["score"] for i in sc] == sorted((i["score"] for i in sc), reverse=True))
sig = fl["items"][0]["dominant_signal"]
by_sig = get("/bi03/flags", signal=sig, page_size=100).json()
ok("A flags list: signal filter only returns that signal", by_sig["total"] > 0 and all(i["dominant_signal"] == sig for i in by_sig["items"]))
ok("A flags list: search by account id works", get("/bi03/flags", q=fl["items"][0]["account_id"]).json()["total"] >= 1)
ok("A flags list: a bad sort is a 400, not a crash", get("/bi03/flags", sort="nope").status_code == 400)

fid = fl["items"][0]["flag_id"]
d = get(f"/bi03/flags/{fid}")
dj = d.json()
ok("A flag detail: flag, signals, nudge with parsed gate result, timeline", d.status_code == 200 and dj["flag"]["flag_id"] == fid and "signals_json" not in dj["flag"]
   and dj["nudge"] is not None and isinstance(dj["nudge"]["gate_result"], dict) and {"risk", "events", "flags"} <= set(dj["timeline"]))
ok("A flag detail: nothing after the replay pointer is shown (risk scores, events, flags)",
   all(r["date"] <= ptr for r in dj["timeline"]["risk"]) and all(e["date"] <= ptr for e in dj["timeline"]["events"]) and all(f["flagged_on"] <= ptr for f in dj["timeline"]["flags"]))
ok("A flag detail: unknown flag is 404", get("/bi03/flags/99999999").status_code == 404)
acc = get(f"/bi03/accounts/{fl['items'][0]['account_id']}")
ok("A account view works and is cut at the pointer", acc.status_code == 200 and all(r["date"] <= ptr for r in acc.json()["risk"]))
ok("A account view: unknown account is 404", get("/bi03/accounts/NOPE").status_code == 404)

nq = get("/bi03/nudges", page_size=3).json()
ok("A nudge queue: pending nudges with gate summary", nq["total"] > 0 and all(i["status"] == "pending_approval" and i["gate"]["passed"] is True for i in nq["items"]) and "gate_result" not in nq["items"][0])
ok("A nudge queue: nothing is 'sent' on the real tenant", get("/bi03/nudges", status="sent").json()["total"] == 0)
th = get("/bi03/thresholds").json()
ok("A thresholds: exactly one active", sum(1 for t in th if t["is_active"]) == 1 and len(th) >= 2)
runs = get("/bi03/runs").json()
ok("A runs: list answers", len(runs) > 0)
rd = get(f"/bi03/runs/{ptr}")
ok("A run detail: steps with parsed input/output", rd.status_code == 200 and len(rd.json()["steps"]) > 0 and all(isinstance(x["step_no"], int) for x in rd.json()["steps"]))
ok("A run detail: a date with no run is 404", get("/bi03/runs/1999-01-01").status_code == 404)
m = get("/bi03/metrics").json()
ok("A metrics: backtest, the hidden-set ledger and the caveat that confirm was not in the hidden number", m["backtest"]["hidden"] is not None and len(m["hidden_set_looks"]) >= 1 and "WITHOUT" in m["backtest"]["note"])
rp = get("/bi03/replay").json()
ok("A replay status: pointer, bounds, can_advance", rp["replay_day"] == s.json()["replay"]["replay_day"] and rp["stream_end_day"] > rp["public_end_day"])

# ================================================================ guards that need no DB change
g1 = post("/bi03/gate/test", {"message": "Hi Asha, aapki EMI 12 tarikh ko hai."})
ok("C gate tester: a message that is not the approved template is rejected", g1.status_code == 200 and g1.json()["passed"] is False and "template_only" in g1.json()["failed"])
push = post("/bi03/gate/test", {"message": "Asha, turant Rs 5000 bharo warna legal action hoga. Aapka salary late hai."})
ok("C gate tester: a pushy message (dues, amount, urgency, signal) is rejected on several counts", push.json()["passed"] is False and len(push.json()["failed"]) >= 3, str(push.json()["failed"]))
tpl = post("/bi03/gate/test", {"message": g1.json()["expected_message"]})
ok("C gate tester: the exact template text passes", tpl.json()["passed"] is True)
ok("C gate tester: sending at 22:00 is rejected on timing even for the exact template", post("/bi03/gate/test", {"message": g1.json()["expected_message"], "send_hour": 22}).json()["passed"] is False)
ok("C gate tester: unknown template is 404", post("/bi03/gate/test", {"message": "x", "template_id": "T-NOPE"}).status_code == 404)
ok("C approve: nudge that does not exist is 404", post("/bi03/nudges/999999999/approve").status_code == 404)
ok("C hidden-period guard: outcomes past the last public day need include_hidden (403 otherwise)", post("/bi03/outcomes/record", {"as_of_day": 120}).status_code == 403)
ok("C hidden-period guard: tuning past the last public day is 403 too", post("/bi03/thresholds/tune", {"cutoff_day": 120}).status_code == 403)
ok("C activate: unknown threshold is 404", post("/bi03/thresholds/nope/activate").status_code == 404)
ok("C advance: days outside 1..10 is 400", post("/bi03/replay/advance", {"days": 11}).status_code == 400)
ok("C reset: a day outside the stream is 400", post("/bi03/replay/reset", {"to_day": 1}).status_code == 400)

# ================================================================ small tenant for the actions
SCR = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\mini_api")
shutil.rmtree(SCR, ignore_errors=True)
py = lambda *a: subprocess.run([sys.executable, *map(str, a)], cwd=BACKEND / "data", capture_output=True, text=True)
ok("C small test tenant generated and loaded", py("generate_bi03.py", "--n", 1000, "--seed", 37, "--id-start", 7_900_000, "--out", SCR).returncode == 0
   and py("load_bi03.py", "--csv", SCR, "--tenant", TEN, "--reset").returncode == 0)
R.run_range(TEN, 61, 80, reset=True, use_cache=False, quiet=True)
import nudge as N  # noqa: E402
N.process_flags(TEN)
st = get("/bi03/replay", TEN).json()
ok("C mini tenant: replay pointer is at day 80", st["replay_day"] == 80, str(st))

# isolation (B)
mine = get("/bi03/flags", TEN, page_size=100).json()
real_ids = {i["account_id"] for i in get("/bi03/flags", T, page_size=100).json()["items"]}
ok("B isolation: the mini tenant's flags are its own accounts only", mine["total"] > 0 and not ({i["account_id"] for i in mine["items"]} & real_ids))
ok("B isolation: the real tenant cannot read a mini-tenant account and the mini tenant cannot read a real one",
   get(f"/bi03/accounts/{mine['items'][0]['account_id']}", T).status_code == 404 and get(f"/bi03/accounts/{fl['items'][0]['account_id']}", TEN).status_code == 404)
ok("B isolation: a tenant with no BI-03 data gets 404, not someone else's data", get("/bi03/summary", "nobody").status_code == 404)

# approve / skip
pend = get("/bi03/nudges", TEN, page_size=10).json()["items"]
n1, n2 = pend[0]["nudge_id"], pend[1]["nudge_id"]
a1 = post(f"/bi03/nudges/{n1}/approve", None, TEN)
ok("C approve: a gate-passed nudge is sent (mock) with a dispatch reference, to a test number only", a1.status_code == 200 and a1.json()["status"] == "sent" and a1.json()["dispatch_ref"] and "mock" in a1.json()["to"], a1.text[:120])
ok("C approve: sending the same nudge twice is a 409", post(f"/bi03/nudges/{n1}/approve", None, TEN).status_code == 409)
ok("C approve: another tenant cannot approve this nudge (404)", post(f"/bi03/nudges/{n2}/approve", None, T).status_code in (404, 409) and get("/bi03/nudges", TEN, status="sent").json()["total"] == 1)
db.execute(TEN, "UPDATE nudges SET status='blocked', gate_passed=0 WHERE tenant_id=:tenant_id AND nudge_id=:i", {"i": n2}, database="bi03")
ok("C approve: a nudge the gate blocked can never be sent (409)", post(f"/bi03/nudges/{n2}/approve", None, TEN).status_code == 409)
n3 = pend[2]["nudge_id"]
ok("C skip: a waiting nudge can be skipped, a skipped one cannot be skipped again or sent",
   post(f"/bi03/nudges/{n3}/skip", None, TEN).status_code == 200 and post(f"/bi03/nudges/{n3}/skip", None, TEN).status_code == 409 and post(f"/bi03/nudges/{n3}/approve", None, TEN).status_code == 409)
ok("C skip: a nudge that was already sent cannot be skipped", post(f"/bi03/nudges/{n1}/skip", None, TEN).status_code == 409)

# advance / reset consistency
snap = lambda: (cnt(TEN, "risk_scores"), cnt(TEN, "flags"), cnt(TEN, "nudges"))
before = snap()
adv = post("/bi03/replay/advance", {"days": 2, "confirm": False}, TEN)
aj = adv.json()
ok("C advance: moves the pointer by the requested days and reports new flags/nudges", adv.status_code == 200 and aj["replay"]["replay_day"] == 82 and aj["ran_days"] == [81, 82], adv.text[:200])
ok("C advance: risk scores grew and every new flag got a nudge", cnt(TEN, "risk_scores") > before[0] and cnt(TEN, "nudges") - before[2] == aj["new_nudges"] and aj["new_flags"] == aj["new_nudges"])
f_after = snap()
rs = post("/bi03/replay/reset", {"to_day": 80}, TEN)
ok("C reset: back to day 80 removes what day 81-82 produced", rs.status_code == 200 and rs.json()["replay_day"] == 80 and snap()[:2] == before[:2] and get("/bi03/replay", TEN).json()["replay_day"] == 80, rs.text[:200])
ok("C reset: no flag, score or run from after day 80 is left", db.query_one(TEN, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id AND flagged_on>:d", {"d": rs.json()["replay_date"]}, database="bi03")["n"] == 0
   and db.query_one(TEN, "SELECT COUNT(*) n FROM agent_runs WHERE tenant_id=:tenant_id AND agent_name='bi03_daily' AND entity_id>:d", {"d": rs.json()["replay_date"]}, database="bi03")["n"] == 0)
adv2 = post("/bi03/replay/advance", {"days": 2, "confirm": False}, TEN).json()
ok("C replay again after a reset gives the same flags as the first time (deterministic)", (cnt(TEN, "risk_scores"), cnt(TEN, "flags")) == f_after[:2] and adv2["new_flags"] == aj["new_flags"])
ok("C summary on the mini tenant counts match the tables", get("/bi03/summary", TEN).json()["nudges_by_status"].get("sent", 0) == cnt(TEN, "nudges") - sum(v for k, v in get("/bi03/summary", TEN).json()["nudges_by_status"].items() if k != "sent"))

# outcomes + tuning through the API (public period only)
orc = post("/bi03/outcomes/record", {"as_of_day": 80}, TEN)
ok("C outcomes: recorded through the API for the public period", orc.status_code == 200 and orc.json()["flags"] > 0, orc.text[:150])
tu = post("/bi03/thresholds/tune", {"cutoff_day": 80}, TEN)
ok("C tuning: returns a candidate table and a recommendation and activates nothing", tu.status_code == 200 and len(tu.json()["table"]) > 0 and "recommended" in tu.json()
   and sum(1 for t in get("/bi03/thresholds", TEN).json() if t["is_active"]) == 1, tu.text[:150])
ver = tu.json().get("version")
ac = post(f"/bi03/thresholds/{ver}/activate", None, TEN)
ok("C activate: switches the active threshold on this tenant", ac.status_code == 200 and [t["threshold_version"] for t in get("/bi03/thresholds", TEN).json() if t["is_active"]] == [ver])

# ================================================================ D safety
real_after = {t: cnt(T, t) for t in real_before}
ok("D the real tenant was not changed by any of the above", real_after == real_before, str(real_after))
ok("D the real tenant's active threshold did not change", db.query_one(T, "SELECT threshold_version v FROM thresholds WHERE tenant_id=:tenant_id AND is_active=1", database="bi03")["v"] == active_before)
paths = set(main.app.openapi()["paths"])
ok("D BI-02 routes are still registered", any(p.startswith("/bi02") for p in paths) and client.get("/health").json() == {"ok": True})
ok("D every BI-03 route is under /bi03", all(p.startswith("/bi03") for p in paths if "bi03" in p) and any(p.startswith("/bi03") for p in paths))

# a BI-03 import failure must not stop BI-02: load main with the router import broken
code = ("import sys; sys.path.insert(0, r'%s'); import routers; import importlib, builtins; real = builtins.__import__\n"
        "def bad(name, *a, **k):\n    if name == 'routers.bi03' or (name == 'routers' and a and len(a) > 2 and a[2] and 'bi03' in a[2]):\n        raise ImportError('simulated')\n    return real(name, *a, **k)\n"
        "builtins.__import__ = bad\nimport main\np = set(main.app.openapi()['paths']); print('bi02' , any(x.startswith('/bi02') for x in p), 'bi03', any(x.startswith('/bi03') for x in p))" % BACKEND)
r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True)
ok("D if BI-03 cannot be imported, the app still starts with BI-02 routes", "bi02 True bi03 False" in r.stdout, (r.stdout + r.stderr)[-200:])

# cleanup the mini tenant
for tbl in ("nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs", "service_templates", "bureau_snapshots", "events", "outcome_labels", "outcome_labels_hidden", "bp_accounts", "bp_meta"):
    db.execute(TEN, f"DELETE FROM {tbl} WHERE tenant_id=:tenant_id", {}, database="bi03")
shutil.rmtree(SCR, ignore_errors=True)
ok("D mini tenant removed", cnt(TEN, "bp_accounts") == 0 and cnt(TEN, "flags") == 0)

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
