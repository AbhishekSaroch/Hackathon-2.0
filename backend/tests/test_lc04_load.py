"""
LC-04 step 3 check: lc04.db matches the CSVs, the 15-minute cube is exactly what the raw events say (recomputed
independently with pandas), the cube is point-in-time and idempotent, tenants are isolated, and the hidden days are NOT
in the database. No LLM. Temporarily writes one test row (tenant L5) and removes it again.
Run:  python backend/tests/test_lc04_load.py        (after: python backend/data/load_lc04.py --reset)
"""
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "agents" / "lc04"))
sys.path.insert(0, str(ROOT / "data"))
import db  # noqa: E402
import cube  # noqa: E402
import lc04_world as W  # noqa: E402

CSV = ROOT / "data" / "csv_lc04"
DBN = "lc04"
failed = 0


def ok(name, cond, extra=""):
    global failed
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        failed += 1


conn = db.get_conn(DBN)
q1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

# ------------------------------------------------------------------ DB = CSV
acct = pd.read_csv(CSV / "accounts.csv")
pub = W.read_events(CSV / "events_public.csv")
ok("accounts: DB rows = CSV rows", q1("select count(*) from lc_accounts") == len(acct), f"{len(acct):,}")
HIDDEN_ON = db.query_one("GLOBAL", "select value from lc_meta where tenant_id=:tenant_id and key='hidden_loaded'", database="lc04")["value"] == "1"
n_hidden = len(W.read_events(CSV / "events_hidden.csv")) if HIDDEN_ON else 0
n_legal = q1("select count(*) from lc_events where event_type='legal'")           # the demo legal-communications scenario (seed_demo_legal.py)
ok("events: DB rows = CSV rows (public days" + (" + the hidden days, which the final run loaded)" if HIDDEN_ON else " only)")
   + (f" + {n_legal} seeded legal notices" if n_legal else ""),
   q1("select count(*) from lc_events") == len(pub) + n_hidden + n_legal, f"{len(pub) + n_hidden + n_legal:,}")
ok("d13 / calendar / practice key rows match",
   q1("select count(*) from lc_d13") == len(pd.read_csv(CSV / "d13.csv"))
   and q1("select count(*) from lc_calendar") == len(pd.read_csv(CSV / "calendar.csv"))
   and q1("select count(*) from anomaly_key") == len(pd.read_csv(CSV / "anomaly_key.csv")) == 7)
ok("every account sits under its own lender (tenant)",
   q1("select count(*) from lc_accounts a join lc_events e on e.account_id=a.account_id where e.tenant_id != a.tenant_id") == 0)

# row-by-row spot check: 300 random events equal the CSV
samp = pub.sample(300, random_state=1)
bad = 0
for _, r in samp.iterrows():
    got = conn.execute("select count(*) from lc_events where account_id=? and ts=? and event_type=? and coalesce(outcome,'')=? "
                       "and coalesce(gateway_id,'')=? and coalesce(dialer_id,'')=?",
                       (r["account_id"], r["ts"].strftime("%Y-%m-%d %H:%M:%S"), r["event_type"], r["outcome"] or "",
                        r["gateway_id"] if isinstance(r["gateway_id"], str) else "",
                        r["dialer_id"] if isinstance(r["dialer_id"], str) else "")).fetchone()[0]
    bad += got < 1
ok("300 random events match the CSV row by row", bad == 0, f"{bad} mismatches")

# ------------------------------------------------------------------ the hidden days are not in the database
pe = db.query_one("GLOBAL", "select value from lc_meta where tenant_id=:tenant_id and key='public_end'", database=DBN)["value"]
hl = db.query_one("GLOBAL", "select value from lc_meta where tenant_id=:tenant_id and key='hidden_loaded'", database=DBN)["value"]
if HIDDEN_ON:
    ok("hidden mode is on (the single final run loaded it): the 6 hidden answer-key rows are present and the load is in the ledger",
       q1("select count(*) from anomaly_key_hidden") == 6 and "hidden_data_loaded" in (CSV.parent / "lc04_hidden_eval_ledger.json").read_text())
else:
    ok("hidden mode is off: no hidden answer key, no event after the public period", hl == "0"
       and q1("select count(*) from anomaly_key_hidden") == 0 and q1("select max(ts) from lc_events") < pe,
       f"last event {q1('select max(ts) from lc_events')}")
r = subprocess.run([sys.executable, str(ROOT / "data" / "load_lc04.py"), "--add-hidden"], capture_output=True, text=True)
ok("--add-hidden is refused without --confirm", r.returncode != 0 and "--confirm" in (r.stdout + r.stderr))
n0 = q1("select count(*) from lc_events")
r = subprocess.run([sys.executable, str(ROOT / "data" / "load_lc04.py")], capture_output=True, text=True)
ok("loading again without --reset is refused and changes nothing", r.returncode != 0 and q1("select count(*) from lc_events") == n0)

# ------------------------------------------------------------------ the cube equals the raw events (independent recompute)
all_events = pd.concat([pub, W.read_events(CSV / "events_hidden.csv")]) if HIDDEN_ON else pub        # what the database holds
if n_legal:                                                                                           # + the seeded legal notices (read back from the DB)
    leg = pd.read_sql_query("select e.tenant_id, e.account_id, e.ts, e.ingested_at, e.event_type, e.outcome, e.channel, e.agency_id, e.dialer_id, "
                            "e.gateway_id, e.amount from lc_events e where e.event_type='legal'", conn, parse_dates=["ts", "ingested_at"])
    all_events = pd.concat([all_events, leg])
ev = all_events.merge(acct[["account_id", "city", "product", "bucket"]].rename(columns={"city": "c2", "product": "p2", "bucket": "b2"}),
                      on="account_id")
ev["bucket_ts"] = ev["ingested_at"].dt.floor("15min").dt.strftime("%Y-%m-%d %H:%M:%S")
for c in ("agency_id", "dialer_id", "gateway_id"):
    ev[c] = ev[c].fillna("-")
et, oc = ev["event_type"], ev["outcome"]
cnt = {"ev_total": np.ones(len(ev), int), "contacts_attempted": et == "contact",
       "contacts_connected": (et == "contact") & (oc == "connected"), "disp_total": et == "disposition",
       "disp_rpc": (et == "disposition") & (oc == "rpc"), "disp_ptp": (et == "disposition") & (oc == "ptp"),
       "disp_refused": (et == "disposition") & (oc == "refused"), "disp_dispute": (et == "disposition") & (oc == "dispute"),
       "disp_other": (et == "disposition") & oc.isin(["wrong_number", "callback"]),
       "ptp_created": (et == "ptp") & (oc == "created"), "ptp_kept": (et == "ptp") & (oc == "kept"),
       "ptp_broken": (et == "ptp") & (oc == "broken"), "pay_attempts": et == "payment",
       "pay_success": (et == "payment") & (oc == "success"), "pay_failed": (et == "payment") & (oc == "failed"),
       "legal_sent": et == "legal",
       "legal_out_of_window": (et == "legal") & ((ev["ts"].dt.hour < 8) | (ev["ts"].dt.hour >= 20))}
for k, v in cnt.items():
    ev[k] = np.asarray(v).astype(int)
ev["ptp_amount"] = np.where((et == "ptp") & (oc == "created"), ev["amount"].fillna(0), 0.0)
ev["pay_amount"] = np.where((et == "payment") & (oc == "success"), ev["amount"].fillna(0), 0.0)
keys = ["tenant_id", "bucket_ts", "c2", "agency_id", "dialer_id", "gateway_id", "p2", "b2"]
exp = ev.groupby(keys, as_index=False)[cube.COUNTER_COLS].sum()
got = pd.read_sql_query("select * from cube_15m", conn)
got = got.rename(columns={"city": "c2", "product": "p2", "bucket": "b2"})
m = exp.merge(got, on=keys, how="outer", suffixes=("_x", "_c"), indicator=True)
ok("cube has exactly the segments the events imply (none missing, none extra)", (m["_merge"] == "both").all(),
   f"{len(exp):,} segments")
both = m[m["_merge"] == "both"]
diffs = {c: int((~np.isclose(both[f"{c}_x"], both[f"{c}_c"])).sum()) for c in cube.COUNTER_COLS}
ok("every counter of every cube row equals the independent recompute from raw events", not any(diffs.values()),
   str({k: v for k, v in diffs.items() if v}))
cross = (ev["ts"].dt.floor("15min") != ev["ingested_at"].dt.floor("15min")).sum()
ok("late arrivals are counted in their ARRIVAL bucket (test is meaningful: some events cross a boundary)", cross > 0,
   f"{cross:,} events arrive in a later bucket than they happened")
ok("slot and weekday columns are right",
   q1("select count(*) from cube_15m where slot != cast(substr(bucket_ts,12,2) as int)*4 + cast(substr(bucket_ts,15,2) as int)/15") == 0
   and q1("select count(*) from cube_15m where bucket_ts like '2026-10-04%' and dow != 6") == 0
   and q1("select count(*) from cube_15m where bucket_ts like '2026-09-28%' and dow != 0") == 0)

# ------------------------------------------------------------------ cube behaviour: idempotent, point-in-time, isolated
before = pd.read_sql_query("select * from cube_15m where tenant_id='L5' and bucket_ts >= '2026-10-12 10:00:00' "
                           "and bucket_ts < '2026-10-12 11:00:00' order by 1,2,6,7,8,9,10,11", conn)
cube.build_range("L5", "2026-10-12 10:00:00", "2026-10-12 11:00:00")
after = pd.read_sql_query("select * from cube_15m where tenant_id='L5' and bucket_ts >= '2026-10-12 10:00:00' "
                          "and bucket_ts < '2026-10-12 11:00:00' order by 1,2,6,7,8,9,10,11", conn)
ok("rebuilding a bucket twice gives the same rows (idempotent)", len(before) > 0 and before.equals(after), f"{len(before)} rows")

db.insert("L5", "cube_15m", {"bucket_ts": "2026-10-12 10:15:00", "day": "2026-10-12", "slot": 41, "dow": 0, "city": "ZZZ",
                             "agency_id": "-", "dialer_id": "-", "gateway_id": "-", "product": "x", "bucket": "0"}, database=DBN)
cube.refresh_bucket("L5", "2026-10-12 10:15:00")
ok("rebuilding a bucket removes stale rows", q1("select count(*) from cube_15m where city='ZZZ'") == 0)

aid = q1("select account_id from lc_accounts where tenant_id='L5' limit 1")
n_b = q1("select ev_total from (select sum(ev_total) ev_total from cube_15m where tenant_id='L5' and bucket_ts='2026-10-12 10:15:00')")
eid = db.insert("L5", "lc_events", {"account_id": aid, "ts": "2026-10-12 10:20:00", "ingested_at": "2026-10-12 10:40:00",
                                    "event_type": "contact", "outcome": "no_answer", "channel": "call",
                                    "agency_id": "A1", "dialer_id": "D1"}, database=DBN)
cube.refresh_bucket("L5", "2026-10-12 10:15:00")
n_a = q1("select sum(ev_total) from cube_15m where tenant_id='L5' and bucket_ts='2026-10-12 10:15:00'")
ok("point-in-time: an event that happened in a bucket but ARRIVED later is not in that bucket", n_a == n_b, f"{n_b} -> {n_a}")
db.execute("L5", "delete from lc_events where tenant_id=:tenant_id and event_id=:i", {"i": eid}, database=DBN)
ok("test event cleaned up", q1("select count(*) from lc_events") == n0)
try:
    cube.build_range("L5", "2026-10-12 10:05:00", "2026-10-12 10:15:00")
    ok("a range off the 15-minute grid is refused", False)
except ValueError:
    ok("a range off the 15-minute grid is refused", True)

tot = sum(db.query_one(t, "select count(*) n from cube_15m where tenant_id=:tenant_id", database=DBN)["n"]
          for t in ("L1", "L2", "L3", "L4", "L5"))
ok("every cube row belongs to one of the 5 lenders; an unknown lender sees nothing",
   tot == q1("select count(*) from cube_15m")
   and db.query_one("NOPE", "select count(*) n from cube_15m where tenant_id=:tenant_id", database=DBN)["n"] == 0, f"{tot:,} rows")
try:
    db.query("L1", "select * from cube_15m", database=DBN)
    ok("unscoped cube query refused", False)
except db.TenantError:
    ok("unscoped cube query refused", True)

# ------------------------------------------------------------------ speed (10k accounts)
plan = " ".join(str(tuple(r)) for r in conn.execute(
    "explain query plan select * from lc_events x where x.tenant_id='L1' and x.ingested_at >= '2026-10-12 10:00:00' "
    "and x.ingested_at < '2026-10-12 10:15:00'").fetchall())
ok("refresh reads events through the arrival-time index (no full scan)", "ix_lc_events_ing" in plan)
busy = conn.execute("select bucket_ts from cube_15m group by bucket_ts order by sum(ev_total) desc limit 1").fetchone()[0]
t0 = time.time()
rows = sum(cube.refresh_bucket(t, busy) for t in ("L1", "L2", "L3", "L4", "L5"))
dt = time.time() - t0
ok("refresh of the busiest 15-minute bucket for all 5 lenders takes under 1 s at 10k accounts", dt < 1, f"{dt * 1000:.0f} ms, {rows} rows, bucket {busy}")
conn.close()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
