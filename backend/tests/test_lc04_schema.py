"""LC-04 step 1 check: schema exists, tenant guard and isolation work on lc04.db. No LLM calls.
Run:  python backend/tests/test_lc04_schema.py      (exit code 1 on failure)
Writes two throw-away rows under tenant TEST_LC and removes them again.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))
import db  # noqa: E402

failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


db.init_db("lc04")
conn = db.get_conn("lc04")
tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")}
expected = {"lc_d13", "lc_calendar", "lc_accounts", "lc_events", "cube_15m", "metric_defs", "lc_roles", "lc_users",
            "dashboard_requests", "lc_meta", "anomaly_key", "anomaly_key_hidden", "agent_runs", "agent_steps",
            "anomalies", "annotations", "alerts", "dashboards"}
ok("all 18 tables exist", expected <= tables, f"missing {sorted(expected - tables)}")

# every table carries tenant_id (project rule)
no_tenant = [t for t in expected if "tenant_id" not in [r[1] for r in conn.execute(f"pragma table_info({t})")]]
ok("every table has tenant_id", not no_tenant, str(no_tenant))

# every foreign-key column has an index (BI-03 lesson: deletes scan child tables otherwise)
unindexed = []
for t in expected:
    for fk in conn.execute(f"pragma foreign_key_list({t})").fetchall():
        col = fk["from"]
        has = any(i2["seqno"] == 0 and i2["name"] == col
                  for idx in conn.execute(f"pragma index_list({t})").fetchall()
                  for i2 in conn.execute(f"pragma index_info({idx['name']})").fetchall())
        if not has:
            unindexed.append(f"{t}.{col}")
ok("every foreign-key column is the first column of an index", not unindexed, str(unindexed))
ok("foreign key check clean", conn.execute("pragma foreign_key_check").fetchall() == [])

# tenant guard + isolation
try:
    db.query("TEST_LC", "select * from lc_accounts", database="lc04")
    ok("unscoped query refused", False)
except db.TenantError:
    ok("unscoped query refused", True)
try:
    db.query("TEST_LC", "select * from lc_accounts where tenant_id=:tenant_id", {"tenant_id": "L2"}, database="lc04")
    ok("tenant_id in params refused", False)
except db.TenantError:
    ok("tenant_id in params refused", True)

db.execute("TEST_LC", "delete from lc_accounts where tenant_id=:tenant_id", database="lc04")
db.insert("TEST_LC", "lc_accounts", {"account_id": "TEST_ACC_1", "city": "Pune", "agency_id": "A1"}, database="lc04")
mine = db.query("TEST_LC", "select * from lc_accounts where tenant_id=:tenant_id", database="lc04")
other = db.query("TEST_LC_OTHER", "select * from lc_accounts where tenant_id=:tenant_id", database="lc04")
ok("own tenant sees its row, other tenant sees nothing", len(mine) == 1 and len(other) == 0, f"{len(mine)} / {len(other)}")

# the event type check constraint
try:
    db.insert("TEST_LC", "lc_events", {"account_id": "TEST_ACC_1", "ts": "2026-01-01 10:00:00",
              "ingested_at": "2026-01-01 10:00:05", "event_type": "bogus"}, database="lc04")
    ok("bad event_type refused", False)
except Exception:
    ok("bad event_type refused", True)

db.execute("TEST_LC", "delete from lc_accounts where tenant_id=:tenant_id", database="lc04")
left = conn.execute("select count(*) from lc_accounts where tenant_id='TEST_LC'").fetchone()[0]
ok("test rows cleaned up", left == 0)
conn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
