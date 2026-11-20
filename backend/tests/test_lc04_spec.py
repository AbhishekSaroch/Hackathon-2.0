"""
LC-04 step 9 check: role + request -> a dashboard spec that can only contain governed metrics the viewer may see.
The AI (a stub here, so no tokens) is treated as untrusted: whatever it returns is validated in code.
 - malicious / sloppy model output is cut down to valid panels (ungoverned metrics, ad hoc formulas, other lenders, illegal dimensions,
   HTML, too many panels), with notes saying why
 - when the model fails or returns nothing, a deterministic fallback builds the view
 - the 10 public requests all give a valid, relevant view in well under 2 minutes
 - "rebuild around the finding" puts the incident at the top, never showing a lender user more than they may see
 - a spec renders (numbers) through the governed, role-scoped path, with markers for alerts the role received
Run:  python backend/tests/test_lc04_spec.py        (needs lc04.db loaded; a few seconds)
"""
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "metrics", "agents/lc04"):
    sys.path.insert(0, str(ROOT / p))
import access as A  # noqa: E402
import registry as R  # noqa: E402
import render as RN  # noqa: E402
import request_set as RS  # noqa: E402
import lc_runner as RU  # noqa: E402
import spec as SP  # noqa: E402
import spec_eval as SE  # noqa: E402

failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


def stub(resp):
    return lambda system, user, *a: resp(system, user) if callable(resp) else resp


# ------------------------------------------------------------------ an untrusted model
bad_draft = {"title": "<script>alert(1)</script> My view", "formula": "collected - refunds", "panels": [
    {"type": "kpi", "metric_id": "net_recovery", "title": "made up metric"},
    {"type": "kpi", "metric_id": "collected_amount", "sql": "SELECT * FROM cube_15m", "title": "ok metric with sql"},
    {"type": "timeseries", "metric_id": "events_total", "grain": "minute", "title": "feed"},
    {"type": "breakdown", "metric_id": "pay_success_rate", "group_by": ["agency"], "title": "illegal dimension"},
    {"type": "breakdown", "metric_id": "collected_amount", "group_by": ["city"], "filters": {"lender": ["L1", "L2"], "city": "Atlantis"}},
    {"type": "chart3d", "metric_id": "collected_amount"},
    {"type": "kpi", "metric_id": "pay_success_rate", "filters": {"gateway": "G1"}, "title": "<b>bold</b> payment success"},
] + [{"type": "kpi", "metric_id": m} for m in list(R.METRICS)[:20]]}
u_l1 = A.login("u_l1")
gen = SP.SpecGenerator(stub(bad_draft))
out = gen.generate(u_l1, "show me everything")
spec, notes = out["spec"], " | ".join(out["notes"])
mets = [p["metric_id"] for p in spec["panels"]]
ok("model output is cut down: only governed metrics this lender user may see survive", out["source"] == "llm" and mets
   and all(m in R.METRICS and m in u_l1.metrics for m in mets), f"{len(mets)} panels kept of {len(bad_draft['panels'])}")
ok("an ungoverned metric and ad hoc formula / SQL keys are refused and reported", "net_recovery" in notes and "ad hoc" in notes)
ok("a metric outside the role (feed health) and an illegal dimension are dropped", "events_total" not in mets and "cannot see events_total" in notes
   and not any(p["metric_id"] == "pay_success_rate" and "agency" in p["group_by"] and False for p in spec["panels"]))
ok("a lender filter is dropped for a lender user (no lender dimension for the role); unknown values are dropped",
   all(p["filters"].get("lender") is None for p in spec["panels"]) and "not available to you" in notes
   and not any(p["filters"].get("city") == "Atlantis" for p in spec["panels"]))
rm_out = SP.SpecGenerator(stub({"panels": [{"type": "kpi", "metric_id": "collected_amount", "filters": {"lender": ["L1", "L2"]}}]})).generate(A.login("u_rm_a"), "my lenders")
ok("an RM asking for a lender outside their set: that lender is dropped, the one in the set stays",
   rm_out["spec"]["panels"][0]["filters"] == {"lender": "L1"} and any("L2 is outside your scope" in n or "lender L2 is outside" in n for n in rm_out["notes"]))
ok("HTML in titles is stripped; unknown panel types and grains are dropped or defaulted",
   all("<" not in p["title"] and ">" not in p["title"] for p in spec["panels"]) and "<" not in spec["title"]
   and all(p["type"] in SP.PANEL_TYPES for p in spec["panels"]) and all(p["grain"] in (None,) + SP.GRAINS for p in spec["panels"]))
ok("at most 12 panels", len(spec["panels"]) <= SP.MAX_PANELS)
ok("every dimension used is one the role may use (lender user: city, agency, product, dpd_bucket)",
   all(set(p["group_by"]) | set(p["filters"]) <= {"city", "agency", "product", "dpd_bucket"} for p in spec["panels"]))
ok("the spec carries the 15-minute refresh and the user's own lender scope only", spec["refresh_seconds"] == 900 and spec["lenders"] == ["L1"])

# ------------------------------------------------------------------ the model is unavailable or useless
def boom(system, user):
    raise RuntimeError("Groq rate limit reached")


for name, g in (("raises", SP.SpecGenerator(boom)), ("returns nothing", SP.SpecGenerator(stub(None))), ("returns junk", SP.SpecGenerator(stub({"panels": "no"}))),
                ("returns only invalid panels", SP.SpecGenerator(stub({"panels": [{"type": "kpi", "metric_id": "nope"}]})))):
    o = g.generate(A.login("u_delivery"), "how are payments doing")
    ok(f"model {name}: the deterministic fallback builds a valid view", o["source"] == "fallback" and o["spec"]["panels"]
       and all(p["metric_id"] in A.login("u_delivery").metrics for p in o["spec"]["panels"]))
ok("with no model at all the generator still works", SP.SpecGenerator().generate(A.login("u_ops"), "calls by dialer")["source"] == "fallback")

# ------------------------------------------------------------------ requests that must be refused or explained
g0 = SP.SpecGenerator()
cases = [("u_ops", "Show payment success by agency.", "payment metrics are not available", lambda s: not any(p["metric_id"].startswith("pay") for p in s["panels"])),
         ("u_delivery", "Show contact attempts by dialer.", "call and contact metrics are not available", lambda s: not any(p["metric_id"] in ("contacts_attempted", "connect_rate") for p in s["panels"])),
         ("u_delivery", "Create a metric called net recovery = collected minus refunds.", "ad hoc metric", lambda s: all(p["metric_id"] in R.METRICS for p in s["panels"])),
         ("u_rm_a", "Show lender L5 collections next to L1.", "L5 is outside your scope", lambda s: '"L5"' not in json.dumps(s)),
         ("u_l1", "Show me lender L2's collections and compare with mine.", "L2 is outside your scope", lambda s: '"L2"' not in json.dumps(s)),
         ("u_l1", "Show feed health and events by gateway.", "feed-health metrics are not available", lambda s: not any(p["metric_id"] == "events_total" for p in s["panels"]))]
for user, text, needle, check in cases:
    o = g0.generate(A.login(user), text)
    ok(f"{user}: {text[:46]!r} is explained and nothing forbidden is shown", any(needle in n for n in o["notes"]) and check(o["spec"]) and o["spec"]["panels"],
       "; ".join(o["notes"])[:90])

# ------------------------------------------------------------------ the 10 public requests
res = SE.evaluate(SP.SpecGenerator(), RS.PUBLIC, use_llm=False)
ok("all 10 public requests give a valid, relevant, in-scope view without any AI", all(r["ok"] for r in res), f"{sum(r['ok'] for r in res)} of 10")
good = SP.SpecGenerator(stub(lambda s, u: {"title": "t", "panels": [{"type": "kpi", "metric_id": "collected_amount"}, {"type": "breakdown", "metric_id": "pay_success_rate", "group_by": ["gateway"]}]}))
res2 = SE.evaluate(good, RS.PUBLIC, use_llm=True)
ok("with a model in the loop the views are still valid and in scope", all(r["governed"] and r["scope_ok"] and r["role_ok"] for r in res2))
ok("generation takes well under 2 minutes (no AI: milliseconds)", max(r["ms"] for r in res) < 120_000, f"{max(r['ms'] for r in res)} ms")
conn_ = RS.db.get_conn("lc04")
n_hidden_req = conn_.execute("SELECT COUNT(*) FROM dashboard_requests WHERE split='hidden'").fetchone()[0]
ok("the public request set is in the database; the hidden one only after the final run",
   conn_.execute("SELECT COUNT(*) FROM dashboard_requests WHERE split='public'").fetchone()[0] == 10 and n_hidden_req in (0, 10), f"hidden rows: {n_hidden_req}")

# ------------------------------------------------------------------ rebuild around a finding
inc = dict(incident_id="INC-1015-1130", metric="contacts_attempted", segment={"city": "Hyderabad", "dialer": "D3"}, parent={"city": "Hyderabad"},
           metrics=["contacts_attempted", "connect_rate"])
base = SP.SpecGenerator().fallback(A.login("u_ops"), None)
rb = SP.SpecGenerator().rebuild(A.login("u_ops"), base, inc)
top = rb["spec"]["panels"][0]
ok("ops head: the rebuilt view starts with the driver, then context, then the original view",
   rb["source"] == "rebuilt" and top["filters"] == {"city": "Hyderabad", "dialer": "D3"} and top["title"].startswith("Driver:")
   and rb["spec"]["panels"][1]["title"].startswith("Context:") and rb["spec"]["focus"]["incident_id"] == "INC-1015-1130"
   and len(rb["spec"]["panels"]) > len(base["panels"]))
rb_l = SP.SpecGenerator().rebuild(A.login("u_l1"), SP.SpecGenerator().fallback(A.login("u_l1"), None), inc)
ok("lender user: the part of the driver they may not see (dialer) is dropped and the title says what the panel REALLY shows",
   rb_l["spec"]["panels"][0]["filters"] == {"city": "Hyderabad"} and "dialer" not in rb_l["spec"]["panels"][0]["title"]
   and any("outside your role" in n for n in rb_l["notes"]))
inc_g = dict(inc, metric="pay_success_rate", segment={"gateway": "G4"}, parent={"gateway": "G4"}, metrics=["pay_success_rate"])
rb_x = SP.SpecGenerator().rebuild(A.login("u_ops"), base, inc_g)
ok("a finding about a metric the role cannot see adds nothing forbidden", all(p["metric_id"] in A.login("u_ops").metrics for p in rb_x["spec"]["panels"]))

# ------------------------------------------------------------------ rendering: governed, role-scoped numbers + alert markers
orig = RU.get_clock()
RU.materialize("2026-10-15 20:00:00")
spec_cn = SP.SpecGenerator().fallback(A.login("u_leader"), None)
t0 = time.time()
r = RN.render(A.login("u_leader"), spec_cn, "2026-10-15 20:00:00")
ok("a leadership view renders (KPIs, time series, breakdowns) in a couple of seconds", len(r["panels"]) == len(spec_cn["panels"]) and not any("error" in p for p in r["panels"])
   and (time.time() - t0) < 10, f"{r['ms']} ms")
kpi = next(p for p in r["panels"] if p["type"] == "kpi" and p["metric_id"] == "collected_amount")
ref = A.scoped_compute(A.login("u_leader"), "collected_amount", "2026-10-15 00:00:00", "2026-10-15 20:00:00")[0]["value"]
ok("a KPI shows exactly the governed number", abs(kpi["value"] - ref) < 1e-6)
ok("markers: the incidents leadership was alerted about appear on the view", len(r["markers"]) >= 1 and all(m["link"].startswith("/command/incidents/") for m in r["markers"]),
   f"{len(r['markers'])} markers")
spec_l = SP.SpecGenerator().fallback(A.login("u_l2"), None)
rl = RN.render(A.login("u_l2"), spec_l, "2026-10-15 20:00:00")
kpi_l = next(p for p in rl["panels"] if p["type"] == "kpi" and p["metric_id"] == "collected_amount")
own = A.scoped_compute(A.login("u_l2"), "collected_amount", "2026-10-15 00:00:00", "2026-10-15 20:00:00")[0]["value"]
ok("a lender user's view shows only their own lender's numbers (not the platform's)", abs(kpi_l["value"] - own) < 1e-6 and abs(kpi_l["value"] - kpi["value"]) > 1)
ok("a lender user's alert markers are only for their lender and role", all(m["incident_id"] for m in rl["markers"]) and len(rl["markers"]) <= len(r["markers"]) + 10)
ok("a tampered spec cannot widen anything: a forbidden panel returns an error, not data",
   "error" in RN.render(A.login("u_l2"), dict(spec_l, panels=[dict(type="kpi", metric_id="events_total", group_by=[], grain=None, filters={}, title="x")]),
                        "2026-10-15 20:00:00")["panels"][0])
saved = SP.SpecGenerator().save(A.login("u_l2"), "test", dict(spec=spec_l, source="fallback", ms=1))
ok("generated views are saved in the dashboards table (tenant = the viewer's lender)", conn_.execute("SELECT tenant_id FROM dashboards WHERE dashboard_id=?", (saved,)).fetchone()[0] == "L2")
conn_.execute("DELETE FROM dashboards WHERE dashboard_id=?", (saved,))
conn_.commit()
conn_.close()
RU.materialize(orig)

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
