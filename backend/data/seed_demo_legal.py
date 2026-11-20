"""
DEMO scenario (not part of any evaluation): legal communications and the permitted 08:00-20:00 window.

  python seed_demo_legal.py            seed it
  python seed_demo_legal.py --reset    remove what it seeded first, then seed again
  python seed_demo_legal.py --remove   remove it (every event_type 'legal' row, the cube rows of those days, the demo marker)

The story (the simulated "now" is 2026-11-20 06:17):
  * Nov 17-19, 09:00-18:30: a normal flow of legal notices from all eight agencies to deeply delinquent accounts of all five lenders
    (about 45 a day), all inside the permitted window.
  * Nov 20, 03:02-03:19: 38 legal notices sent by ONE agency (A4) to accounts of lenders L2 and L3, in the middle of the night, outside the
    window. The sender's batch log reached the platform between 06:05 and 06:14, so the platform learns of it at 06:15, three hours late.
Everything is synthetic. The rows are ordinary lc_events rows (event_type 'legal', outcome 'sent', channel 'legal_notice'); lc_meta key
'demo_legal_day' = 2026-11-20 lets the replay clock run into that day. The hidden-evaluation numbers are untouched; the seed is logged in the
ledger file as 'demo_legal_seeded' so nobody mistakes it for evaluation data.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "db"))
sys.path.insert(0, str(HERE.parent / "agents" / "lc04"))
import db  # noqa: E402
import cube  # noqa: E402

DB = "lc04"
LEDGER = HERE / "lc04_hidden_eval_ledger.json"
DAY = "2026-11-20"
BASELINE_DAYS = ["2026-11-17", "2026-11-18", "2026-11-19"]
VIOLATING_AGENCY = "A4"
N_VIOLATIONS = 38
LENDERS = ["L1", "L2", "L3", "L4", "L5"]
AGENCIES = [f"A{i}" for i in range(1, 9)]


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _log(event, **kw):
    rows = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    rows.append({"event": event, "at": datetime.now().isoformat(timespec="seconds"), **kw})
    LEDGER.write_text(json.dumps(rows, indent=2))


def _days_to_rebuild():
    return BASELINE_DAYS + [DAY]


def remove():
    conn = db.get_conn(DB)
    n = conn.execute("SELECT COUNT(*) FROM lc_events WHERE event_type='legal'").fetchone()[0]
    conn.execute("DELETE FROM lc_events WHERE event_type='legal'")
    conn.execute("DELETE FROM lc_meta WHERE key='demo_legal_day'")
    conn.commit()
    conn.close()
    for t in LENDERS:
        cube.build_range(t, f"{BASELINE_DAYS[0]} 00:00:00", "2026-11-21 00:00:00", DB)
    if n:
        _log("demo_legal_removed", events=n)
    return n


def seed():
    rng = random.Random(2026_11_20)
    conn = db.get_conn(DB)
    if conn.execute("SELECT COUNT(*) FROM lc_events WHERE event_type='legal'").fetchone()[0]:
        sys.exit("legal events already exist: use --reset to replace them or --remove to delete them")
    pool = {t: [r[0] for r in conn.execute("SELECT account_id FROM lc_accounts WHERE tenant_id=? AND bucket IN ('61-90','90+')", (t,))]
            for t in LENDERS}
    conn.close()
    rows = {t: [] for t in LENDERS}

    def add(lender, account, ts, ingested, agency):
        rows[lender].append(dict(account_id=account, ts=_fmt(ts), ingested_at=_fmt(ingested), event_type="legal", outcome="sent",
                                 channel="legal_notice", agency_id=agency))

    # normal days: inside the window, all agencies, all lenders
    for d in BASELINE_DAYS:
        base = datetime.fromisoformat(d)
        for _ in range(45):
            lender = rng.choice(LENDERS)
            ts = base + timedelta(minutes=rng.randint(9 * 60, 18 * 60 + 30), seconds=rng.randint(0, 59))
            add(lender, rng.choice(pool[lender]), ts, ts + timedelta(seconds=rng.randint(2, 60)), rng.choice(AGENCIES))
    # the violation: one agency, lenders L2 and L3, 03:02-03:19, logged at 06:05-06:14
    base = datetime.fromisoformat(DAY)
    for i in range(N_VIOLATIONS):
        lender = "L2" if i % 2 == 0 else "L3"
        ts = base + timedelta(minutes=3 * 60 + 2, seconds=rng.randint(0, 17 * 60 + 30))
        add(lender, rng.choice(pool[lender]), ts, base + timedelta(hours=6, minutes=5, seconds=rng.randint(0, 9 * 60)), VIOLATING_AGENCY)
    for lender, rs in rows.items():
        db.insert_many(lender, "lc_events", rs, DB)
    db.execute("GLOBAL", "DELETE FROM lc_meta WHERE tenant_id=:tenant_id AND key='demo_legal_day'", database=DB)
    db.insert("GLOBAL", "lc_meta", {"key": "demo_legal_day", "value": DAY}, DB)
    for t in LENDERS:
        cube.build_range(t, f"{BASELINE_DAYS[0]} 00:00:00", "2026-11-21 00:00:00", DB)
    total = sum(len(v) for v in rows.values())
    _log("demo_legal_seeded", events=total, violation=dict(day=DAY, agency=VIOLATING_AGENCY, notices=N_VIOLATIONS, sent="03:02-03:19", logged="06:05-06:14"),
         note="demo scenario seeded by us; not part of the hidden evaluation")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()
    db.init_db(DB)
    if a.remove:
        print(f"removed {remove():,} legal events")
    else:
        if a.reset:
            print(f"removed {remove():,} legal events")
        print(f"seeded {seed():,} legal events (135 normal, {N_VIOLATIONS} outside the window at 03:02-03:19 on {DAY})")
