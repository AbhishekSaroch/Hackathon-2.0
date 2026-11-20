"""
BI-03 nudge engine tests.   python test_bi03_nudge.py   (exit code 1 on any failure)

Runs on a small separate tenant (the real tenant's nudges are never touched):
  A mapping     the dominant signal picks the right nudge type, wording comes only from approved templates
  B gate        every stored nudge re-passes the gate independently; frequency cap and past-due rules hold
  C agent       a rejected wording makes the agent try the next approved variant; a rejected situation stops it
  D dispatch    only gate-passed nudges can be sent; tampering and wrong hours are refused at send time
  E safety      the whole thing runs with the outcome tables blocked at database level
"""
import shutil
import sqlite3
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db"), str(BACKEND / "gates")]
import compliance as G  # noqa: E402
import db  # noqa: E402
import features as F  # noqa: E402
import nudge as N  # noqa: E402
import runner as R  # noqa: E402

REAL, TEN = "abc_bank", "t_nudge"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


count = lambda tenant, t: db.query_one(tenant, f"SELECT COUNT(*) n FROM {t} WHERE tenant_id=:tenant_id", database="bi03")["n"]
real_before = (count(REAL, "nudges"), count(REAL, "flags"))

# ---------------------------------------------------------------- set up a small tenant, run the daily runner, then the nudge engine
SCR = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\mini_nudge")
shutil.rmtree(SCR, ignore_errors=True)
py = lambda *a: subprocess.run([sys.executable, *map(str, a)], cwd=BACKEND / "data", capture_output=True, text=True)
ok("small tenant generated and loaded", py("generate_bi03.py", "--n", 800, "--seed", 12, "--id-start", 9_000_000, "--out", SCR).returncode == 0
   and py("load_bi03.py", "--csv", SCR, "--tenant", TEN, "--reset").returncode == 0)

BLOCKED = ("outcome_labels", "outcome_labels_hidden")
orig = db.get_conn
def guarded(database="main"):
    c = orig(database)
    c.set_authorizer(lambda action, a1, a2, dbn, src: sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and a1 in BLOCKED) else sqlite3.SQLITE_OK)
    return c

db.get_conn = guarded                                      # E: everything below runs with the outcome tables blocked
try:
    R.run_range(TEN, 61, 90, reset=True, use_cache=False, quiet=True)
    eng = F.FeatureEngine.from_db(TEN)
    summary = N.process_flags(TEN, eng=eng)
    ran = True
except sqlite3.DatabaseError:
    ran, summary = False, {}
finally:
    db.get_conn = orig
ok("E flags and nudges are produced with the outcome tables blocked at database level", ran and summary.get("flags", 0) > 20, str(summary))

q = lambda sql, **p: db.query_df(TEN, sql, p, database="bi03")
nfl = q("SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id").n[0]
nn = q("SELECT COUNT(*) n FROM nudges WHERE tenant_id=:tenant_id").n[0]
ok("A every flag has exactly one nudge", nfl == nn == summary["flags"] and q("SELECT COUNT(*) n FROM (SELECT flag_id FROM nudges WHERE tenant_id=:tenant_id GROUP BY 1 HAVING COUNT(*)>1)").n[0] == 0, f"{nfl} flags, {nn} nudges")

m = q("SELECT f.dominant_signal s, n.nudge_type t FROM nudges n JOIN flags f ON f.flag_id=n.flag_id WHERE n.tenant_id=:tenant_id")
exp = m.s.map(lambda s: N.SIGNAL_TO_NUDGE.get(s, N.DEFAULT_NUDGE))
ok("A the dominant signal decides the nudge type (late salary -> EMI date, bounce / late EMI -> auto-pay, else reminder)", (m.t == exp).all(), str(m.groupby(["s", "t"]).size().to_dict()))
ok("A at least 2 different nudge types are used (the agent really chooses)", m.t.nunique() >= 2)

nd = q("SELECT * FROM nudges WHERE tenant_id=:tenant_id")
tpl = {r.template_id: r for r in q("SELECT template_id, body FROM service_templates WHERE tenant_id=:tenant_id").itertuples()}
import json
good = nd[nd.gate_passed == 1]
rerun_ok = True
for r in good.itertuples():
    meta = json.loads(r.gate_result)
    expected = N.render(tpl[r.template_id].body, meta["params"], meta["language"])
    res = G.evaluate(r.message, expected, datetime.fromisoformat(r.created_at), True, 0)
    rerun_ok &= not res["content_failed"] and r.message == expected
ok("B every passed nudge is exactly an approved template with its blanks filled, and re-passes every content check", rerun_ok and len(good) > 0, f"{len(good)} nudges")
ok("B passed nudges wait for approval, nothing is sent on its own", set(good.status) == {"pending_approval"})
ok("B no placeholder or empty name is left in any message", not good.message.str.contains(r"\{").any() and good.message.str.match(r"^(Hi|Dear|Namaste|Hello) [A-Z][a-z]+").all())
ok("B customer-facing text is formal English (Roman letters) by default, and every message ends with a thank-you", good.template_id.str.contains("-E").mean() > 0.95 and good.message.str.contains(r"Thank you\.$|Dhanyavaad\.$").all(), f"{good.template_id.str.contains('-E').mean():.0%}")
ok("B the message never says which distress signal was seen", not good.message.str.contains(r"bounce|utili[sz]ation|enquir|overdue|dues", case=False, regex=True).any())

cap = q("SELECT COUNT(*) n FROM nudges a JOIN nudges b ON a.account_id=b.account_id AND a.tenant_id=b.tenant_id AND b.nudge_id>a.nudge_id AND a.gate_passed=1 AND b.gate_passed=1 "
        "AND julianday(b.created_at)-julianday(a.created_at) < 7 WHERE a.tenant_id=:tenant_id").n[0]
ok("B no account gets two nudges within 7 days", cap == 0)
st = nd[nd.gate_passed == 1].merge(q("SELECT flag_id, account_id, flagged_on FROM flags WHERE tenant_id=:tenant_id"), on="flag_id", suffixes=("", "_f"))
gone = set()
for day, grp in st.groupby("flagged_on"):
    ev = eng.events[eng.events.account_id.isin(grp.account_id) & (eng.events.available_from <= pd.Timestamp(day))]
    gone |= set(R.not_current_accounts(ev, pd.Timestamp(day)))
ok("B nobody who is already past due gets a nudge", not (set(good.account_id) & gone))
fl_status = q("SELECT f.status s, n.gate_passed g FROM flags f JOIN nudges n ON n.flag_id=f.flag_id WHERE f.tenant_id=:tenant_id")
ok("B a flag whose nudge passed the gate is marked 'nudged' (or 'closed' if the account later missed)", set(fl_status[fl_status.g == 1].s) <= {"nudged", "closed"})

# ---------------------------------------------------------------- C the agent's behaviour when the gate objects
templates = N.load_templates(TEN)
params = {"name": "Asha", "emi_day": 12, "new_day": 20}
MORN = datetime(2026, 8, 10, 10, 0)
hi = N.SIGNAL_TO_NUDGE["salary_delay"]
bad_first = {k: [dict(v[0], body=v[0]["body"] + " Your account is overdue.")] + v[1:] if k == (hi, N.LANGUAGE) else v for k, v in templates.items()}
n1 = N.draft_and_gate(bad_first, "ACC-1", "salary_delay", params, MORN, True, 0)
n2 = N.draft_and_gate(bad_first, "ACC-2", "salary_delay", params, MORN, True, 0)
tried = [json.loads(n["gate_result"])["attempts"] for n in (n1, n2)]
ok("C when a wording is rejected the agent tries the next approved variant and still sends a clean nudge",
   all(n["gate_passed"] for n in (n1, n2)) and any(len(t) == 2 and not t[0]["passed"] and "no_dues_language" in t[0]["failed"] for t in tried), str([[a["passed"] for a in t] for t in tried]))
all_bad = {k: [dict(x, body=x["body"] + " Pay the overdue dues immediately.") for x in v] for k, v in templates.items()}
nb = N.draft_and_gate(all_bad, "ACC-3", "salary_delay", params, MORN, True, 0)
meta = json.loads(nb["gate_result"])
ok("C if EVERY approved wording is rejected the nudge is blocked, with reasons, and nothing is sendable", nb["status"] == "blocked" and nb["gate_passed"] == 0 and "no_dues_language" in meta["failed"] and len(meta["attempts"]) >= 4, f"{len(meta['attempts'])} attempts")
nh = N.draft_and_gate(templates, "ACC-4", "salary_delay", params, datetime(2026, 8, 10, 7, 0), True, 0)
ok("C wrong send time: blocked at once, different wording is not tried (it cannot help)", nh["status"] == "blocked" and len(json.loads(nh["gate_result"])["attempts"]) == 1 and json.loads(nh["gate_result"])["failed"] == ["allowed_hours"])
ok("C a nudge sent in the last 7 days blocks the next one", N.draft_and_gate(templates, "ACC-5", "salary_delay", params, MORN, True, 1)["status"] == "blocked")
ok("C an account already past due is never nudged", N.draft_and_gate(templates, "ACC-6", "salary_delay", params, MORN, False, 0)["status"] == "blocked")
eng_msg = N.draft_and_gate(templates, "ACC-7", "salary_delay", params, MORN, True, 0, language="english")["message"]
ok("C English works too and uses ordinals ('20th')", "20th" in eng_msg, eng_msg[:80])

# ---------------------------------------------------------------- D dispatch (mock)
pend = q("SELECT nudge_id, message FROM nudges WHERE tenant_id=:tenant_id AND status='pending_approval' ORDER BY nudge_id")
a, b, c = int(pend.nudge_id[0]), int(pend.nudge_id[1]), int(pend.nudge_id[2])
res = N.nudge_dispatch(TEN, a)
row = q("SELECT status, sent_at, dispatch_ref FROM nudges WHERE tenant_id=:tenant_id AND nudge_id=:i", i=a).iloc[0]
ok("D a gate-passed nudge is sent (mock): status sent, time and reference recorded", row.status == "sent" and row.sent_at and row.dispatch_ref.startswith("MOCK-WA-") and res["to"].startswith("test number"))
try:
    N.nudge_dispatch(TEN, a); twice = False
except N.GateError:
    twice = True
ok("D the same nudge cannot be sent twice", twice)
db.execute(TEN, "UPDATE nudges SET gate_passed = 0, status = 'blocked' WHERE tenant_id=:tenant_id AND nudge_id=:i", {"i": b}, database="bi03")
try:
    N.nudge_dispatch(TEN, b); refused = False
except N.GateError as e:
    refused = "compliance gate" in str(e)
ok("D a nudge that did not pass the gate is refused by the dispatch tool", refused)
db.execute(TEN, "UPDATE nudges SET message = message || ' Pay the pending dues now.' WHERE tenant_id=:tenant_id AND nudge_id=:i", {"i": c}, database="bi03")
try:
    N.nudge_dispatch(TEN, c); caught = False
except N.GateError as e:
    caught = "refused at dispatch" in str(e)
ok("D a message edited after the gate (tampering) is caught again at send time", caught)
d_ = int(pend.nudge_id[3])
try:
    N.nudge_dispatch(TEN, d_, send_at=datetime(2026, 8, 10, 21, 30)); late = False
except N.GateError as e:
    late = "allowed_hours" in str(e)
ok("D sending at 21:30 is refused at dispatch (hours are re-checked)", late)
try:
    N.nudge_dispatch(TEN, 10 ** 9); missing = False
except N.GateError:
    missing = True
ok("D an unknown nudge id is refused", missing)

# ---------------------------------------------------------------- auto-send path and idempotency
N.reset_nudges(TEN)
s2 = N.process_flags(TEN, eng=eng, auto_send=True)
ok("auto-send mode: every gate-passed nudge is sent and every blocked one stays blocked", s2["sent"] == s2["pending_approval"] and q("SELECT COUNT(*) n FROM nudges WHERE tenant_id=:tenant_id AND status='sent'").n[0] == s2["sent"], str(s2))
ok("processing again creates nothing new (idempotent)", N.process_flags(TEN, eng=eng).get("flags") == 0)

ok("the real tenant's flags and nudges were not touched", (count(REAL, "nudges"), count(REAL, "flags")) == real_before)

for tb in ("nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs", "events", "bureau_snapshots", "outcome_labels",
           "outcome_labels_hidden", "service_templates", "bp_meta", "bp_accounts"):
    db.execute(TEN, f"DELETE FROM {tb} WHERE tenant_id=:tenant_id", database="bi03")
shutil.rmtree(SCR, ignore_errors=True)

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
