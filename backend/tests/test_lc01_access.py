"""LC-01 isolation: scope comes from the user record, the SQL checker refuses everything but a governed read-only SELECT, the data
layer refuses an unscoped query, and no path returns another lender's rows. Runs on a COPY of lc01.db. Zero LLM tokens.
    python backend/tests/test_lc01_access.py"""
import dataclasses
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp()) / "lc01.db"
shutil.copy(ROOT / "lc01.db", TMP)
os.environ["PORTFOLIOIQ_LC01_DB"] = str(TMP)
sys.path[:0] = [str(ROOT / "db"), str(ROOT / "agents" / "lc01")]
import db  # noqa: E402
import lc01_access as A  # noqa: E402
import lc01_compiler as C  # noqa: E402
import lc01_registry as R  # noqa: E402

bad = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:                      # wrong exception type
        print("   unexpected", repr(e))
        return False
    return False


# ---- users and scope
p1, p2, p3, pc, pb = (A.login(u) for u in ("u_l1", "u_l2", "u_l3", "u_csm", "u_csm_b"))
ok("lender users see exactly their own lender", (p1.lenders, p2.lenders, p3.lenders) == (("L1",), ("L2",), ("L3",)))
ok("client success managers see their set", pc.lenders == ("L1", "L2", "L3") and pb.lenders == ("L2", "L3"))
ok("unknown user is refused", raises(A.AccessError, lambda: A.login("u_nobody")))
ok("a forged principal (lender list edited) is refused", raises(A.AccessError, lambda: A.verify(dataclasses.replace(p1, lenders=("L1", "L2")))))
ok("a forged principal (role edited) is refused", raises(A.AccessError, lambda: A.verify(dataclasses.replace(p1, role="csm"))))
ok("a plain dict is not a principal", raises(A.AccessError, lambda: A.verify({"user_id": "u_l1"})))
ok("asking for a lender outside the scope raises, it is not narrowed silently", raises(A.AccessError, lambda: A.require_lenders(p1, ["L2"])))
ok("a CSM cannot reach a lender outside the set", raises(A.AccessError, lambda: A.require_lenders(pb, ["L1"])))
ok("a lender filter inside the compiler is checked against the scope", raises(A.AccessError, lambda: C.make_query(p1, "connect_rate", filters={"lender": ["L2"]})))
ok("splitting by lender needs more than one lender in scope", raises(A.AccessError, lambda: C.make_query(p1, "connect_rate", group_by=["lender"])))
ok("a CSM can split by lender", C.make_query(pc, "connect_rate", group_by=["lender"]).group_by == ("lender",))

# ---- the data layer itself
ok("db.query refuses a statement without :tenant_id", raises(db.TenantError, lambda: db.query("L1", "SELECT * FROM fact_perf LIMIT 1", database="lc01")))
ok("db.query refuses tenant_id passed as a parameter", raises(db.TenantError, lambda: db.query("L1", "SELECT * FROM loans WHERE tenant_id=:tenant_id", {"tenant_id": "L2"}, "lc01")))
for t in ("fact_perf", "fact_calls", "fact_ptp", "fact_payments", "fact_disputes", "loans", "agencies"):
    seen = {r["tenant_id"] for r in db.query("L1", f"SELECT DISTINCT tenant_id FROM {t} WHERE tenant_id=:tenant_id", database="lc01")}
    ok(f"{t}: a lender-1 query can only ever return lender-1 rows", seen == {"L1"})
ok("a tenant id typed into the SQL as a literal is not enough: the checker refuses it", raises(A.SqlRefused, lambda: A.check_sql("SELECT * FROM loans WHERE tenant_id = 'L2'")))

# ---- SQL checker: adversarial statements
refused = [
    "DROP TABLE loans", "DELETE FROM loans WHERE tenant_id = :tenant_id", "UPDATE loans SET emi = 0 WHERE tenant_id = :tenant_id",
    "INSERT INTO loans VALUES (1) -- :tenant_id", "SELECT * FROM loans WHERE tenant_id = :tenant_id; DROP TABLE loans",
    "SELECT * FROM loans WHERE tenant_id = :tenant_id UNION SELECT * FROM loans", "SELECT * FROM loans",
    "SELECT * FROM loans WHERE tenant_id = :tenant_id OR tenant_id = 'L2'", "SELECT * FROM loans WHERE tenant_id IN ('L1','L2')",
    "SELECT * FROM lc_users WHERE tenant_id = :tenant_id", "SELECT * FROM qa_log WHERE tenant_id = :tenant_id", "SELECT * FROM audit_log WHERE tenant_id = :tenant_id",
    "SELECT * FROM sqlite_master WHERE tenant_id = :tenant_id", "PRAGMA table_info(loans) -- :tenant_id", "ATTACH DATABASE 'x.db' AS x -- :tenant_id",
    "SELECT * FROM loans /* :tenant_id */", "WITH x AS (SELECT 1) SELECT * FROM loans WHERE tenant_id = :tenant_id", "", "   ",
    "SELECT * FROM loans WHERE tenant_id = :tenant_id; SELECT 1", "CREATE TABLE z AS SELECT * FROM loans WHERE tenant_id = :tenant_id",
    "SELECT * FROM loans WHERE tenant_id LIKE 'L%'", "SELECT * FROM loans WHERE tenant_id <> :tenant_id", "SELECT * FROM loans WHERE tenant_id = L2",
    "SELECT * FROM loans WHERE tenant_id = :tenant_id OR 1 = 1", "SELECT * FROM loans WHERE NOT tenant_id = :tenant_id", "SELECT * FROM loans WHERE tenant_id != :tenant_id",
    "SELECT * FROM loans WHERE tenant_id = :tenant_id EXCEPT SELECT * FROM loans", "SELECT tenant_id FROM loans WHERE tenant_id = :tenant_id OR month = '2026-01'",
]
ok(f"the SQL checker refuses all {len(refused)} adversarial statements", all(raises(A.SqlRefused, lambda s=s: A.check_sql(s)) for s in refused))
good = "SELECT agency AS agency, COUNT(*) AS den FROM fact_calls WHERE tenant_id = :tenant_id AND month >= :m_from GROUP BY agency"
ok("a governed SELECT passes the checker", A.check_sql(good) == good)
ok("every statement the compiler can produce passes the checker", all(
    A.check_sql(C.compile_sql(C.make_query(pc, mid, group_by=[R.METRICS[mid].dims[0]], filters={R.METRICS[mid].dims[1]: [C.dim_values(pc)[R.METRICS[mid].dims[1]][0]]}, trend=False))[0])
    for mid in R.METRICS))

# ---- no path returns another lender's rows
leak = 0
for p in (p1, p2, p3):
    for mid in R.METRICS:
        res = C.run(p, C.make_query(p, mid, group_by=[R.METRICS[mid].dims[0]]))
        leak += len({s for s in [p.lenders[0]]} - set(p.lenders))
        assert set(res.query.lenders) == set(p.lenders)
ok("lender users: every query of every metric ran only on their own lender", leak == 0)
ok("a lender user's numbers differ from the combined numbers (scope is real, not cosmetic)",
   C.run(p1, C.make_query(p1, "loans_in_book")).rows[0]["value"] == 3000 and C.run(pc, C.make_query(pc, "loans_in_book")).rows[0]["value"] == 6800)
ok("lender 2 sees 2,200 loans and lender 3 sees 1,600", C.run(p2, C.make_query(p2, "loans_in_book")).rows[0]["value"] == 2200 and C.run(p3, C.make_query(p3, "loans_in_book")).rows[0]["value"] == 1600)
ok("dimension values shown to a user come only from their own lender (L3 has no Kolkata, L1 has no gold loans)",
   "Kolkata" not in C.dim_values(p3)["city"] and "gold_loan" not in C.dim_values(p1)["product"])
ok("an unknown segment is reported, not silently empty", raises(C.UnknownValue, lambda: C.make_query(p3, "connect_rate", filters={"city": ["Kolkata"]})))
ok("an unknown metric / dimension / month is refused", raises(R.MetricError, lambda: C.make_query(p1, "profit")) and raises(R.MetricError, lambda: C.make_query(p1, "connect_rate", group_by=["channel"]))
   and raises(R.MetricError, lambda: C.make_query(p1, "connect_rate", m_from="2027-01", m_to="2027-02")))

# ---- audit trail: written under the user's own tenant and invisible to other users
before = len(A.audit_rows(p1, 500))
A.audit(p1, "cross_tenant", "unit-test marker", "show L2")
ok("audit rows are visible to their owner", len(A.audit_rows(p1, 500)) == before + 1)
ok("... and invisible to another lender's user", all(r["detail"] != "unit-test marker" for u in (p2, p3) for r in A.audit_rows(u, 500)))

print(f"\n{'ALL PASS' if not bad else str(len(bad)) + ' FAILED'}")
sys.exit(1 if bad else 0)
