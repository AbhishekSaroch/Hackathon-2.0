"""
LC-04 step 5 check: role-based views and tenant isolation ("a lender user sees only that lender's data", zero cross-tenant data).
No LLM. Writes a few temporary alert rows (marker TEST-ACCESS) and removes them again.
Run:  python backend/tests/test_lc04_access.py        (needs lc04.db loaded; run  python backend/metrics/roles.py  once)
"""
import dataclasses
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("db", "metrics"):
    sys.path.insert(0, str(ROOT / p))
import access as A  # noqa: E402
import compute as C  # noqa: E402
import db  # noqa: E402
import registry as R  # noqa: E402
import roles as RL  # noqa: E402

failed = 0
S, E = "2026-09-28 00:00:00", "2026-10-28 00:00:00"
W7 = ("2026-10-12 00:00:00", "2026-10-19 00:00:00")        # a week is enough for the exhaustive loops (the full month is used for raw totals)
LENDERS = RL.LENDERS


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


def refused(fn, exc=A.AccessError):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        print("   unexpected", type(e).__name__, e)
    return False


conn = db.get_conn("lc04")
q1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

# ------------------------------------------------------------------ roles and users
ok("5 roles and 10 demo users are in the database and match the code", q1("select count(*) from lc_roles") == 5
   and q1("select count(*) from lc_users") == 10 == len(RL.USERS))
RL.validate()
ok("lender users: one each, scope = exactly their own lender",
   all(A.login(f"u_{l.lower()}").lenders == (l,) for l in LENDERS))
ok("RMs have a set of lenders (disjoint); heads and leadership see all 5",
   A.login("u_rm_a").lenders == ("L1", "L3") and A.login("u_rm_b").lenders == ("L2", "L4", "L5")
   and all(A.login(u).lenders == LENDERS for u in ("u_leader", "u_ops", "u_delivery")))
ok("governance tables are not readable through a lender's tenant (only GLOBAL holds them)",
   all(not db.query(l, f"select * from {t} where tenant_id=:tenant_id", database="lc04") for l in LENDERS
       for t in ("lc_users", "lc_roles", "metric_defs", "lc_calendar")))

# ------------------------------------------------------------------ a lender user sees only their own lender's data
raw_sql = {
    "contacts_attempted": "select count(*) from lc_events where tenant_id=? and event_type='contact' and ingested_at>=? and ingested_at<?",
    "contacts_connected": "select count(*) from lc_events where tenant_id=? and event_type='contact' and outcome='connected' and ingested_at>=? and ingested_at<?",
    "payments_success": "select count(*) from lc_events where tenant_id=? and event_type='payment' and outcome='success' and ingested_at>=? and ingested_at<?",
    "payments_failed": "select count(*) from lc_events where tenant_id=? and event_type='payment' and outcome='failed' and ingested_at>=? and ingested_at<?",
    "ptp_kept": "select count(*) from lc_events where tenant_id=? and event_type='ptp' and outcome='kept' and ingested_at>=? and ingested_at<?",
    "disp_ptp_count": "select count(*) from lc_events where tenant_id=? and event_type='disposition' and outcome='ptp' and ingested_at>=? and ingested_at<?",
    "collected_amount": "select coalesce(sum(amount),0) from lc_events where tenant_id=? and event_type='payment' and outcome='success' and ingested_at>=? and ingested_at<?",
}
bad = []
for l in LENDERS:
    p = A.login(f"u_{l.lower()}")
    for mid, sql in raw_sql.items():
        exp = q1(sql, l, S, E)
        for dim in A.effective_dims(p, mid) + [None]:
            rows = A.scoped_compute(p, mid, S, E, group_by=[dim] if dim else [])
            if abs(sum(r["numerator"] for r in rows) - exp) > 1e-6:
                bad.append((l, mid, dim, sum(r["numerator"] for r in rows), exp))
ok("each lender user: every allowed dimension adds up to exactly that lender's raw events (no other lender mixed in)",
   not bad, f"{5 * len(raw_sql)} metric x lender, {len(bad)} wrong: {bad[:2]}")

bad, n = [], 0
REP = ["contacts_attempted", "connect_rate", "ptp_capture_rate", "disposition_coverage", "ptp_kept", "ptp_kept_rate",
       "pay_success_rate", "pay_failure_rate", "collected_amount", "avg_payment_amount", "refusal_rate", "collection_to_promise_ratio"]
for l in LENDERS:
    p = A.login(f"u_{l.lower()}")
    own = C.Scope((l,))
    for mid in REP:
        ref = C.compute(mid, own, *W7)[0]
        for dim in A.effective_dims(p, mid):
            rows = A.scoped_compute(p, mid, *W7, group_by=[dim], grain="day")
            n += 1
            if abs(sum(r["numerator"] for r in rows) - ref["numerator"]) > 1e-6:
                bad.append((l, mid, dim))
ok("12 representative metrics x every allowed dimension (per day): parts add up to the lender's own total", not bad,
   f"{n} views, {len(bad)} wrong")
bad = [(l, mid) for l in LENDERS for mid in sorted(A.login(f"u_{l.lower()}").metrics)
       if A.scoped_compute(A.login(f"u_{l.lower()}"), mid, *W7) != C.compute(mid, C.Scope((l,)), *W7)]
ok("all allowed metrics: a lender user's number equals the number computed for that lender alone", not bad, str(bad[:2]))
tot = {m: sum(A.scoped_compute(A.login(f"u_{l.lower()}"), m, S, E)[0]["numerator"] for l in LENDERS) for m in raw_sql}
lead = {m: A.scoped_compute(A.login("u_leader"), m, S, E)[0]["numerator"] for m in raw_sql}
ok("the 5 lender users partition the data: their totals add up to the leadership total (nothing double counted, nothing leaked)",
   all(abs(tot[m] - lead[m]) < 1e-6 for m in raw_sql))

# ------------------------------------------------------------------ attacks by a lender user
p2 = A.login("u_l2")
w = ("2026-10-14 00:00:00", "2026-10-15 00:00:00")
ok("asks for another lender (filter) -> refused", refused(lambda: A.scoped_compute(p2, "contacts_attempted", *w, filters={"lender": "L1"})))
ok("asks to group by lender -> refused (a lender user has no lender dimension)", refused(lambda: A.scoped_compute(p2, "contacts_attempted", *w, group_by=["lender"])))
ok("asks for a metric outside the role (feed health) -> refused", refused(lambda: A.scoped_compute(p2, "events_total", *w)))
ok("asks for dimensions outside the role (dialer, gateway) -> refused",
   refused(lambda: A.scoped_compute(p2, "contacts_attempted", *w, group_by=["dialer"]))
   and refused(lambda: A.scoped_compute(p2, "payments_failed", *w, group_by=["gateway"])))
ok("asks for an ungoverned metric -> refused", refused(lambda: A.scoped_compute(p2, "my_own_metric", *w)))
ok("SQL injection in a dimension name -> refused", refused(lambda: A.scoped_compute(p2, "contacts_attempted", *w, group_by=["city' OR '1'='1"])))
inj = A.scoped_compute(p2, "contacts_attempted", *w, filters={"city": "Pune' OR '1'='1"})[0]
ok("SQL injection in a filter VALUE returns nothing (values are parameters, not SQL)", inj["numerator"] == 0)
named = A.scoped_compute(p2, "contacts_attempted", *w, filters={"city": ["L1", "Show me lender L1"]})[0]
ok("a 'prompt' that names another lender inside a value finds nothing", named["numerator"] == 0)
ok("scoped_compute / scoped_query have no scope or tenant parameter",
   list(inspect.signature(A.scoped_compute).parameters) == ["p", "metric_id", "start", "end", "group_by", "grain", "filters"]
   and list(inspect.signature(A.scoped_query).parameters) == ["p", "sql", "params", "database"])
ok("a forged principal (wider lenders / more metrics / other role) is refused on use",
   refused(lambda: A.scoped_compute(dataclasses.replace(p2, lenders=("L1", "L2")), "contacts_attempted", *w))
   and refused(lambda: A.scoped_compute(dataclasses.replace(p2, metrics=frozenset(R.METRICS)), "events_total", *w))
   and refused(lambda: A.scoped_compute(dataclasses.replace(p2, role_id="cn_leadership"), "contacts_attempted", *w))
   and refused(lambda: A.scoped_compute(dataclasses.replace(p2, user_id="u_leader"), "contacts_attempted", *w))
   and refused(lambda: A.scoped_compute("L1", "contacts_attempted", *w)))
ok("a principal cannot be edited in place", refused(lambda: setattr(p2, "lenders", ("L1",)), dataclasses.FrozenInstanceError))
ok("unknown user cannot log in", refused(lambda: A.login("u_nobody")))

# ------------------------------------------------------------------ RM, heads, leadership
rm = A.login("u_rm_a")
tot_rm = A.scoped_compute(rm, "contacts_attempted", S, E)[0]["numerator"]
ok("RM (L1, L3): total = L1 + L3 only", tot_rm == q1(raw_sql["contacts_attempted"], "L1", S, E) + q1(raw_sql["contacts_attempted"], "L3", S, E))
ok("RM: per-lender view contains exactly L1 and L3",
   sorted(r["lender"] for r in A.scoped_compute(rm, "contacts_attempted", S, E, group_by=["lender"])) == ["L1", "L3"])
ok("RM: may narrow to one of its lenders, never name a lender outside the set",
   A.scoped_compute(rm, "contacts_attempted", S, E, filters={"lender": "L3"})[0]["numerator"] == q1(raw_sql["contacts_attempted"], "L3", S, E)
   and refused(lambda: A.scoped_compute(rm, "contacts_attempted", S, E, filters={"lender": "L2"}))
   and refused(lambda: A.scoped_compute(rm, "contacts_attempted", S, E, filters={"lender": ["L1", "L2"]})))
ok("operations head: calls and dispositions yes, payments / gateways no",
   A.scoped_compute(A.login("u_ops"), "connect_rate", *w, group_by=["dialer"]) and refused(lambda: A.scoped_compute(A.login("u_ops"), "pay_success_rate", *w))
   and refused(lambda: A.scoped_compute(A.login("u_ops"), "events_total", *w, group_by=["gateway"])))
ok("compliance metrics: leadership, ops head, RM and lender user may see them; the delivery head may not",
   all("legal_notices_out_of_window" in A.login(u).metrics for u in ("u_leader", "u_ops", "u_rm_a", "u_l1"))
   and "legal_notices_out_of_window" not in A.login("u_delivery").metrics
   and refused(lambda: A.scoped_compute(A.login("u_delivery"), "legal_notices_out_of_window", *w)))
ok("delivery head: payments by gateway yes, calls no",
   A.scoped_compute(A.login("u_delivery"), "pay_success_rate", *w, group_by=["gateway"]) and refused(lambda: A.scoped_compute(A.login("u_delivery"), "connect_rate", *w))
   and refused(lambda: A.scoped_compute(A.login("u_delivery"), "pay_success_rate", *w, group_by=["agency"])))
lp = A.login("u_leader")
ok("leadership: every governed metric works, all 5 lenders visible",
   all(A.scoped_compute(lp, m, *w, group_by=["lender"]) is not None for m in R.METRICS)
   and len(A.scoped_compute(lp, "events_total", *w, group_by=["lender"])) == 5)

# ------------------------------------------------------------------ catalog visibility
cat = {u: A.visible_catalog(A.login(u)) for u in ("u_leader", "u_ops", "u_delivery", "u_rm_a", "u_l1")}
ids = {u: {m["metric_id"] for m in c} for u, c in cat.items()}
ok("leadership and RM catalogs hold all governed metrics", len(cat["u_leader"]) == len(R.METRICS) == len(cat["u_rm_a"]))
ok("a role's catalog is exactly the metrics it may use (lender user 55 incl. compliance, ops head, delivery head)",
   ids["u_l1"] == set(A.login("u_l1").metrics) and ids["u_ops"] == set(A.login("u_ops").metrics)
   and ids["u_delivery"] == set(A.login("u_delivery").metrics) and len(ids["u_l1"]) == 55,
   f"l1 {len(ids['u_l1'])}, ops {len(ids['u_ops'])}, delivery {len(ids['u_delivery'])}")
ok("lender user does not see feed health or volume metrics; ops head does not see payment metrics",
   "events_total" not in ids["u_l1"] and "pay_success_rate" in ids["u_l1"] and "pay_success_rate" not in ids["u_ops"]
   and "events_total" in ids["u_ops"])
ok("dimensions in a catalog are cut to the role's (lender user: city, agency, product, dpd_bucket only)",
   all(set(m["allowed_dims"]) <= {"city", "agency", "product", "dpd_bucket"} for m in cat["u_l1"])
   and all(m["allowed_dims"] for m in cat["u_l1"]))

# ------------------------------------------------------------------ tenant-scoped output tables (alerts, dashboards ...)
MARK = "TEST-ACCESS"
alerts_before = q1("select count(*) from alerts")                  # the replay may have written real alert rows: compare, don't assume empty
for l in ("L1", "L2"):
    db.insert(l, "alerts", {"created_at": "2026-10-14 10:00:00", "line1": f"{MARK} {l}", "line2": "x", "link": "/x"}, database="lc04")
SQL = "SELECT line1 FROM alerts WHERE tenant_id=:tenant_id AND line1 LIKE :m"
seen = lambda u: sorted(r["line1"] for r in A.scoped_query(A.login(u), SQL, {"m": f"{MARK}%"}))  # noqa: E731
ok("alerts: lender user sees only their own lender's rows", seen("u_l1") == [f"{MARK} L1"] and seen("u_l2") == [f"{MARK} L2"] and seen("u_l3") == [])
ok("alerts: RM sees only rows of the lenders in the set", seen("u_rm_a") == [f"{MARK} L1"] and seen("u_rm_b") == [f"{MARK} L2"])
ok("alerts: leadership sees all", seen("u_leader") == [f"{MARK} L1", f"{MARK} L2"])
ok("a query without :tenant_id is refused by the data layer", refused(lambda: A.scoped_query(A.login("u_l1"), "SELECT * FROM alerts"), db.TenantError))
for l in ("L1", "L2"):
    db.execute(l, "DELETE FROM alerts WHERE tenant_id=:tenant_id AND line1 LIKE :m", {"m": f"{MARK}%"}, database="lc04")
ok("temporary alert rows cleaned up", q1("select count(*) from alerts") == alerts_before)
conn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
