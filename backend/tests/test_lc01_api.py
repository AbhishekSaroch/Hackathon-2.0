"""LC-01 API: login by header, scope from the user record, refusals are normal answers with an audit row, history and audit are per
user, the evaluation endpoint serves the saved report, and BI-02 / BI-03 / LC-04 still start if LC-01 cannot be imported.
Runs on a COPY of lc01.db and WITHOUT the model (LC01_NO_LLM=1). Zero tokens.   python backend/tests/test_lc01_api.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp()) / "lc01.db"
shutil.copy(ROOT / "lc01.db", TMP)
os.environ["PORTFOLIOIQ_LC01_DB"] = str(TMP)
os.environ["LC01_NO_LLM"] = "1"
sys.path[:0] = [str(ROOT), str(ROOT / "db")]
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

bad = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


c = TestClient(main.app)
H = lambda u: {"X-User-Id": u}
ask = lambda u, q, **kw: c.post("/lc01/ask", headers=H(u), json={"question": q, **kw})

ok("no header: 401", c.get("/lc01/me").status_code == 401 and c.post("/lc01/ask", json={"question": "x"}).status_code == 401)
ok("unknown user: 401", c.get("/lc01/me", headers=H("u_nobody")).status_code == 401)
ok("a lender cannot be chosen by a parameter: ?lender= and a body field are ignored", c.get("/lc01/me?lender=L2", headers=H("u_l1")).json()["lenders"] == ["L1"])
me = c.get("/lc01/me", headers=H("u_csm_b")).json()
ok("/me reports the role and lenders from the user record", me["lenders"] == ["L2", "L3"] and me["role"] == "csm" and me["multi"])
ok("/users lists the 5 demo users", len(c.get("/lc01/users").json()) == 5)
cat = c.get("/lc01/metrics", headers=H("u_l1")).json()
ok("/metrics: 38 governed metrics, overlapping ones marked with who they overlap", len(cat) == 38 and sum(1 for m in cat if m["overlap"]) == 16
   and all(m["overlaps_with"] for m in cat if m["overlap"]))
ex = c.get("/lc01/examples", headers=H("u_l1")).json()
ok("/examples: public questions for this user, no refusal samples", len(ex) >= 5 and all(e["kind"] != "refuse" for e in ex))

r = ask("u_l1", "What was our NACH bounce rate last month?")
j = r.json()
ok("/ask answers with value, chart-less single figure, SQL and steps", r.status_code == 200 and j["status"] == "answered" and j["sql"] and j["steps"] and j["data"][0]["value"] > 0)
ok("answer scope is the user's lender only", j["scope"]["lenders"] == ["L1"] and all(s["lenders"] == ["L1"] for s in j["sql"]))
j = ask("u_l1", "What is our collection efficiency?").json()
ok("/ask: ambiguous question returns a clarifying question with the definitions", j["status"] == "clarify" and len(j["clarification"]["options"]) == 4)
pick = j["clarification"]["options"][1]["metric_id"]
j2 = ask("u_l1", "What is our collection efficiency?", choice=pick).json()
ok("/ask with choice answers with the chosen definition", j2["status"] == "answered" and j2["metric"]["id"] == pick)
ok("a choice that is not a governed metric is a 400", ask("u_l1", "x", choice="profit").status_code == 400)
j = ask("u_l1", "What is Lakshya Capital's PAR 90? (marker 77)").json()
ok("cross-lender question: normal 200 answer with status refused", j["status"] == "refused" and j["refusal"]["category"] == "cross_tenant")
ok("... the refusal shows no number and no SQL", j["sql"] == [] and not j.get("data"))
au = c.get("/lc01/audit", headers=H("u_l1")).json()
ok("/audit shows my refusal", any("marker 77" in a["question"] for a in au))
ok("/audit of another lender's user does not show it", all("marker 77" not in a["question"] for a in c.get("/lc01/audit", headers=H("u_l2")).json()))
ok("/audit of a CSM does not show a lender user's rows", all("marker 77" not in a["question"] for a in c.get("/lc01/audit", headers=H("u_csm")).json()))
h1 = c.get("/lc01/history", headers=H("u_l1")).json()
ok("/history lists my questions, newest first", len(h1) >= 4 and h1[0]["id"] > h1[1]["id"])
one = c.get(f"/lc01/history/{h1[0]['id']}", headers=H("u_l1"))
ok("/history/{id} returns the full saved answer", one.status_code == 200 and "headline" in one.json())
ok("another user cannot open my history item (404)", c.get(f"/lc01/history/{h1[0]['id']}", headers=H("u_l2")).status_code == 404)
ok("a question too long is rejected politely", ask("u_l1", "x" * 700).json()["status"] == "error")
ok("/evaluation works before and after a run", c.get("/lc01/evaluation", headers=H("u_l1")).status_code == 200)

# ---- isolation of the rest of the product: LC-01 must not change BI-02 / BI-03 / LC-04 routes
paths = set(main.app.openapi()["paths"])
ok("BI-02, BI-03, LC-04 and LC-01 routes are all mounted", {"/lc01/ask", "/health"} <= paths and any(p.startswith("/bi03") for p in paths) and any(p.startswith("/lc04") for p in paths) and any(p.startswith("/bi02") for p in paths))
code = ("import sys; sys.path[:0]=[r'%s', r'%s']; sys.modules['routers.lc01']=None\n"
        "import main; paths=set(main.app.openapi()['paths'])\n"
        "assert any(p.startswith('/bi02') for p in paths) and any(p.startswith('/bi03') for p in paths) and any(p.startswith('/lc04') for p in paths) and not any(p.startswith('/lc01') for p in paths) and '/health' in paths\n"
        "print('started without LC-01')") % (str(ROOT), str(ROOT / "db"))
res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
ok("if LC-01 cannot be imported, BI-02 / BI-03 / LC-04 still start and /health works", res.returncode == 0 and "started without LC-01" in res.stdout)
if res.returncode:
    print(res.stderr[-600:])

print(f"\n{'ALL PASS' if not bad else str(len(bad)) + ' FAILED'}")
sys.exit(1 if bad else 0)
