"""
LC-04 speed test: can the cube be refreshed every 15 minutes over 100,000 accounts?
Builds a SEPARATE 100,000-account database (backend/lc04_speed.db, 3 days of events ~ 700k rows, same as the 10k x 30-day
dev set), times the refresh of the busiest 15-minute bucket for all 5 lenders and typical dashboard reads, then deletes it.
The real lc04.db is never touched (PORTFOLIOIQ_LC04_DB points elsewhere).
Run:  python backend/tests/test_lc04_speed.py [--keep]        (takes a few minutes)
Honest scope: 3 days of history, not 40. Refresh cost depends on the events in one bucket (index lookup), not on history length;
dashboard reads depend on the window they read, so those are timed on a 24-hour window.
"""
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEED_DB = ROOT / "lc04_speed.db"
SPEED_CSV = ROOT / "data" / "csv_lc04_speed"
os.environ["PORTFOLIOIQ_LC04_DB"] = str(SPEED_DB)          # must be set BEFORE db.py is imported

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "agents" / "lc04"))
sys.path.insert(0, str(ROOT / "data"))
import db  # noqa: E402
import cube  # noqa: E402
import lc04_world as W  # noqa: E402
import load_lc04 as L  # noqa: E402

N, DAYS = 100_000, 3
failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


assert Path(db.LC04_DB_PATH) == SPEED_DB, "speed test must not use the real database"
for p in (SPEED_DB, Path(str(SPEED_DB) + "-wal"), Path(str(SPEED_DB) + "-shm")):
    p.unlink(missing_ok=True)
shutil.rmtree(SPEED_CSV, ignore_errors=True)
SPEED_CSV.mkdir(parents=True)

t0 = time.time()
rng = np.random.default_rng(7)
acct = W.make_accounts(N, rng)
ev = W.simulate(acct, rng, days=DAYS)
ev = ev[ev["ts"] < W.START_TS + pd.Timedelta(days=DAYS)]
print(f"world: {N:,} accounts, {len(ev):,} events over {DAYS} days, generated in {time.time() - t0:.0f}s")
acct.to_csv(SPEED_CSV / "accounts.csv", index=False)
pd.DataFrame(W.d13_rows()).to_csv(SPEED_CSV / "d13.csv", index=False)
pd.DataFrame(W.calendar_rows()).assign(tenant_id="GLOBAL").to_csv(SPEED_CSV / "calendar.csv", index=False)
W.write_events(ev, SPEED_CSV / "events_public.csv")
pd.DataFrame(columns=["anomaly_id", "tenant_id", "kind", "start_ts", "end_ts", "metric_id", "segment", "note"]).to_csv(
    SPEED_CSV / "anomaly_key.csv", index=False)
(SPEED_CSV / "meta.json").write_text('{"seed": 7, "accounts": %d, "stream_start": "%s", "days": 40, "public_days": 30, "hidden_days": 10}'
                                     % (N, W.START.isoformat()))
db.init_db("lc04")
t1 = time.time()
loaded, tenants = L.load_public(SPEED_CSV)
print(f"loaded {loaded['lc_events']:,} events in {time.time() - t1:.0f}s")
t2 = time.time()
rows = sum(cube.build_all(t) for t in tenants)
print(f"full cube build: {rows:,} rows in {time.time() - t2:.0f}s")

conn = db.get_conn("lc04")
busy = conn.execute("select bucket_ts from cube_15m group by bucket_ts order by sum(ev_total) desc limit 1").fetchone()[0]
ev_in_bucket = conn.execute("select sum(ev_total) from cube_15m where bucket_ts=?", (busy,)).fetchone()[0]
t0 = time.time()
rows_b = sum(cube.refresh_bucket(t, busy) for t in tenants)
dt = time.time() - t0
ok("refresh of the busiest 15-minute bucket (all 5 lenders) under 5 s at 100,000 accounts", dt < 5,
   f"{dt * 1000:.0f} ms, {ev_in_bucket:,} events -> {rows_b:,} cube rows, bucket {busy}")
print(f"  (cube compression in the busiest bucket: {ev_in_bucket / rows_b:.1f} events per row)")

day = busy[:10]
t0 = time.time()
r1 = db.query("L1", "select bucket_ts, city, sum(contacts_attempted) a, sum(contacts_connected) c from cube_15m "
              "where tenant_id=:tenant_id and day=:d group by bucket_ts, city", {"d": day}, database="lc04")
d1 = time.time() - t0
t0 = time.time()
r2 = [db.query(t, "select bucket_ts, sum(pay_success) s, sum(pay_attempts) n from cube_15m where tenant_id=:tenant_id "
               "and day=:d group by bucket_ts", {"d": day}, database="lc04") for t in tenants]
d2 = time.time() - t0
t0 = time.time()
r3 = db.query("L1", "select agency_id, dialer_id, city, sum(contacts_attempted) a from cube_15m where tenant_id=:tenant_id "
              "and bucket_ts >= :s and bucket_ts < :e group by agency_id, dialer_id, city",
              {"s": f"{day} 10:00:00", "e": f"{day} 11:00:00"}, database="lc04")
d3 = time.time() - t0
ok("dashboard reads over one day: city time series (one lender) under 2 s", d1 < 2, f"{d1 * 1000:.0f} ms, {len(r1):,} rows")
ok("dashboard reads over one day: payment success, all 5 lenders, under 2 s", d2 < 2, f"{d2 * 1000:.0f} ms")
ok("drill-down read: one hour grouped by agency x dialer x city under 1 s", d3 < 1, f"{d3 * 1000:.0f} ms, {len(r3)} rows")
conn.close()

if "--keep" not in sys.argv:
    import gc
    gc.collect()                       # db.get_conn() connections are only closed when garbage collected (Windows locks the file)
    for p in (SPEED_DB, Path(str(SPEED_DB) + "-wal"), Path(str(SPEED_DB) + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except PermissionError:
            print(f"note: could not delete {p.name} (still locked); delete it by hand")
    shutil.rmtree(SPEED_CSV, ignore_errors=True)
print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
