"""
Load the LC-01 CSVs into lc01.db (tenant ids are added by db.insert_many) and run integrity checks.

    python backend/data/load_lc01.py --reset      # delete lc01.db, rebuild the schema, load, check, write the users
Refuses to load twice without --reset. Touches only lc01.db.
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND / "db"))
sys.path.insert(0, str(BACKEND / "agents" / "lc01"))
import db  # noqa: E402
import lc01_access as A  # noqa: E402

CSV = HERE / "csv_lc01"
DB = "lc01"
TABLES = ["lender_config", "agencies", "loans", "fact_perf", "fact_calls", "fact_ptp", "fact_payments", "fact_disputes"]


def main(reset: bool):
    path, _ = db._paths(DB)
    if reset and path.exists():
        for ext in ("", "-wal", "-shm"):
            Path(str(path) + ext).unlink(missing_ok=True)
    db.init_db(DB)
    if db.query("L1", "SELECT COUNT(*) n FROM loans WHERE tenant_id=:tenant_id", database=DB)[0]["n"]:
        sys.exit("lc01.db already has data. Use --reset to rebuild it.")
    for t in TABLES:
        df = pd.read_csv(CSV / f"{t}.csv")
        df = df.astype(object).where(df.notna(), None)
        n = 0
        for tenant, g in df.groupby("tenant_id"):
            rows = g.drop(columns=["tenant_id"]).to_dict("records")
            n += db.insert_many(tenant, t, rows, DB)
        print(f"{t:15s} {n:>8,d} rows")
    print("users:", A.sync_users())
    checks()


def checks():
    bad = []

    def ok(name, cond):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            bad.append(name)

    with db.get_conn(DB) as c:
        q = lambda s: c.execute(s).fetchall()
        ok("3 lenders in every fact table", all(len(q(f"SELECT DISTINCT tenant_id FROM {t}")) == 3 for t in TABLES if t not in ("lender_config",)))
        ok("lender_config has 3 rows", q("SELECT COUNT(*) FROM lender_config")[0][0] == 3)
        ok("loans: 6,800", q("SELECT COUNT(*) FROM loans")[0][0] == 6800)
        ok("fact_perf = loans x 10 months", q("SELECT COUNT(*) FROM fact_perf")[0][0] == 68000)
        ok("every loan has exactly 10 months", q("SELECT COUNT(*) FROM (SELECT tenant_id, loan_id, COUNT(*) n FROM fact_perf GROUP BY 1,2 HAVING n<>10)")[0][0] == 0)
        ok("loan ids are prefixed with their own tenant", q("SELECT COUNT(*) FROM loans WHERE loan_id NOT LIKE tenant_id || '-%'")[0][0] == 0)
        for t in ("fact_perf", "fact_calls", "fact_ptp", "fact_payments", "fact_disputes"):
            ok(f"{t}: every row's loan belongs to the same tenant",
               q(f"SELECT COUNT(*) FROM {t} f LEFT JOIN loans l ON l.tenant_id=f.tenant_id AND l.loan_id=f.loan_id WHERE l.loan_id IS NULL")[0][0] == 0)
        ok("months span 2026-01..2026-10", [r[0] for r in q("SELECT DISTINCT month FROM fact_perf ORDER BY 1")] == [f"2026-{m:02d}" for m in range(1, 11)])
        ok("bucket matches DPD at start", q("""SELECT COUNT(*) FROM fact_perf WHERE bucket <>
            CASE WHEN dpd_start<=0 THEN 'current' WHEN dpd_start<=30 THEN '1-30' WHEN dpd_start<=60 THEN '31-60' WHEN dpd_start<=90 THEN '61-90' ELSE '90+' END""")[0][0] == 0)
        ok("a loan's dpd_start equals last month's dpd_end", q("""SELECT COUNT(*) FROM fact_perf a JOIN fact_perf b ON a.tenant_id=b.tenant_id AND a.loan_id=b.loan_id
            AND b.month = strftime('%Y-%m', date(a.month || '-01', '+1 month')) WHERE b.dpd_start <> a.dpd_end""")[0][0] == 0)
        ok("paid_full consistent with paid_emi", q("SELECT COUNT(*) FROM fact_perf WHERE (paid_full=1) <> (paid_emi >= emi_due - 0.000001)")[0][0] == 0)
        ok("no negative money", q("SELECT COUNT(*) FROM fact_perf WHERE paid_emi<0 OR recovered<0 OR arrears_start<0 OR outstanding_end<0")[0][0] == 0)
        ok("L2 grace: no DPD 1-5 at month end", q("SELECT COUNT(*) FROM fact_perf WHERE tenant_id='L2' AND dpd_end BETWEEN 1 AND 5")[0][0] == 0)
        ok("a kept PTP paid its full promise", q("SELECT COUNT(*) FROM fact_ptp WHERE kept=1 AND ABS(paid_amount-promised_amount)>0.01")[0][0] == 0)
        ok("a broken PTP paid less than promised", q("SELECT COUNT(*) FROM fact_ptp WHERE kept=0 AND paid_amount>=promised_amount")[0][0] == 0)
        ok("calls: outcome and disposition values valid", q("""SELECT COUNT(*) FROM fact_calls WHERE outcome NOT IN ('connected','no_answer','wrong_number')
            OR disposition NOT IN ('ptp','callback','refuses','dispute','paid_already','none') OR (outcome<>'connected' AND disposition<>'none')""")[0][0] == 0)
        ok("payments: channel and status values valid", q("""SELECT COUNT(*) FROM fact_payments WHERE channel NOT IN ('nach','upi','app','cash','cheque')
            OR status NOT IN ('success','failed','bounced') OR (status='bounced' AND channel<>'nach')""")[0][0] == 0)
        ok("resolved disputes have days, open ones do not", q("SELECT COUNT(*) FROM fact_disputes WHERE (resolved=1 AND days_to_resolve IS NULL) OR (resolved=0 AND days_to_resolve IS NOT NULL)")[0][0] == 0)
        ok("users written", q("SELECT COUNT(*) FROM lc_users")[0][0] == len(A.USERS))
        ok("no protected-attribute columns in the warehouse", not any(col in ("age", "gender", "religion", "caste", "name")
                                                                    for t in TABLES for col in [r[1] for r in c.execute(f"PRAGMA table_info({t})")]))
    print(f"\n{'ALL PASS' if not bad else str(len(bad)) + ' FAILED'}")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main("--reset" in sys.argv)
