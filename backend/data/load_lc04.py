"""
Load the LC-04 CSVs into lc04.db (through db.py, so tenant_id is bound automatically) and build the 15-minute cube.

  python load_lc04.py [--csv <dir>] [--reset]
  python load_lc04.py --add-hidden --confirm        STEP 12 ONLY: adds the hidden days 31-40 + the hidden answer key

- Default load = PUBLIC days 1-30 only. The hidden events and the hidden answer key are NOT in the database, so
  tuning and development cannot touch them. --add-hidden adds them (and writes a line to the hidden-evaluation ledger).
- --reset deletes the data and the agent output of every lender (accounts, events, cube, anomalies, alerts ...). It keeps
  the governance tables (metric_defs, roles, users, request set), which other steps fill from code.
- Loading twice without --reset is refused (events have auto ids, a second load would silently double them).
Runs integrity checks at the end and exits with code 1 if any fails.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "db"))
sys.path.insert(0, str(HERE.parent / "agents" / "lc04"))
import db  # noqa: E402
import cube  # noqa: E402

DB = "lc04"
CHUNK = 50_000
G = "GLOBAL"
LEDGER = HERE / "lc04_hidden_eval_ledger.json"
EVENT_COLS = ["account_id", "ts", "ingested_at", "event_type", "outcome", "channel", "agency_id", "dialer_id",
              "gateway_id", "amount"]
ACCT_COLS = ["account_id", "product", "loan_amount", "emi_amount", "dpd", "bucket", "outstanding", "city", "agency_id"]
# tables wiped by --reset, children first (governance tables are kept)
DATA_TABLES = ["alerts", "annotations", "dashboards", "anomalies", "agent_steps", "agent_runs", "cube_15m", "lc_events",
               "lc_accounts", "anomaly_key", "anomaly_key_hidden", "lc_calendar", "lc_d13", "lc_meta"]


def _records(df):
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _insert(tenant, table, df):
    n = 0
    for i in range(0, len(df), CHUNK):
        n += db.insert_many(tenant, table, _records(df.iloc[i:i + CHUNK]), database=DB)
    return n


def _fmt_times(df):
    df = df.copy()
    for c in ("ts", "ingested_at"):
        df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def _count(tenant, table):
    return db.query_one(tenant, f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = :tenant_id", database=DB)["n"]


def reset():
    with db.get_conn(DB) as conn:
        for t in DATA_TABLES:
            conn.execute(f"DELETE FROM {t}")


def load_events(csv_path: Path, tenants):
    ev = pd.read_csv(csv_path, usecols=["tenant_id"] + EVENT_COLS, keep_default_na=False, na_values=[""],
                     dtype={"agency_id": "object", "dialer_id": "object", "gateway_id": "object", "channel": "object"})
    ev = _fmt_times(ev)
    total = 0
    for t in tenants:
        total += _insert(t, "lc_events", ev[ev["tenant_id"] == t].drop(columns="tenant_id"))
    return total


def load_public(csv: Path):
    meta = json.loads((csv / "meta.json").read_text())
    acct = pd.read_csv(csv / "accounts.csv")
    tenants = sorted(acct["tenant_id"].unique())
    out = {}
    out["lc_d13"] = _insert(G, "lc_d13", pd.read_csv(csv / "d13.csv").drop(columns="tenant_id"))
    out["lc_calendar"] = _insert(G, "lc_calendar", pd.read_csv(csv / "calendar.csv").drop(columns="tenant_id"))
    start = pd.Timestamp(meta["stream_start"])
    meta_rows = {"stream_start": start.strftime("%Y-%m-%d %H:%M:%S"), "days": meta["days"], "public_days": meta["public_days"],
                 "hidden_days": meta["hidden_days"],
                 "public_end": (start + pd.Timedelta(days=meta["public_days"])).strftime("%Y-%m-%d %H:%M:%S"),
                 "clock": start.strftime("%Y-%m-%d %H:%M:%S"), "hidden_loaded": "0", "accounts": meta["accounts"],
                 "seed": meta["seed"]}
    out["lc_meta"] = db.insert_many(G, "lc_meta", [{"key": k, "value": str(v)} for k, v in meta_rows.items()], database=DB)
    out["lc_accounts"] = 0
    for t in tenants:
        out["lc_accounts"] += _insert(t, "lc_accounts", acct[acct["tenant_id"] == t][ACCT_COLS])
    out["lc_events"] = load_events(csv / "events_public.csv", tenants)
    key = pd.read_csv(csv / "anomaly_key.csv")
    out["anomaly_key"] = 0
    for t in key["tenant_id"].unique():
        out["anomaly_key"] += _insert(t, "anomaly_key", key[key["tenant_id"] == t].drop(columns="tenant_id"))
    return out, tenants


def add_hidden(csv: Path):
    """STEP 12 ONLY. Adds the hidden events, the hidden answer key and the cube for days 31-40; logs it."""
    acct = pd.read_csv(csv / "accounts.csv")
    tenants = sorted(acct["tenant_id"].unique())
    if db.query_one(G, "SELECT value FROM lc_meta WHERE tenant_id=:tenant_id AND key='hidden_loaded'", database=DB)["value"] == "1":
        sys.exit("hidden data is already loaded")
    n = load_events(csv / "events_hidden.csv", tenants)
    key = pd.read_csv(csv / "anomaly_key_hidden.csv")
    for t in key["tenant_id"].unique():
        _insert(t, "anomaly_key_hidden", key[key["tenant_id"] == t].drop(columns="tenant_id"))
    pe = db.query_one(G, "SELECT value FROM lc_meta WHERE tenant_id=:tenant_id AND key='public_end'", database=DB)["value"]
    rows = sum(cube.build_all(t, since=pe) for t in tenants)
    db.execute(G, "UPDATE lc_meta SET value='1' WHERE tenant_id=:tenant_id AND key='hidden_loaded'", database=DB)
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    ledger.append({"event": "hidden_data_loaded", "at": datetime.now().isoformat(timespec="seconds"),
                   "events": n, "cube_rows": rows, "note": "hidden days 31-40 and answer key added to lc04.db"})
    LEDGER.write_text(json.dumps(ledger, indent=2))
    print(f"hidden days loaded: {n:,} events, {rows:,} cube rows. Logged in {LEDGER.name}")


def check(tenants, loaded):
    ok_all = True

    def rep(cond, name):
        nonlocal ok_all
        print(("PASS  " if cond else "FAIL  ") + name)
        ok_all &= bool(cond)

    for t, n in loaded.items():
        if t in ("lc_d13", "lc_calendar", "lc_meta"):
            got = _count(G, t)
        elif t == "anomaly_key":
            with db.get_conn(DB) as conn:
                got = conn.execute("SELECT COUNT(*) FROM anomaly_key").fetchone()[0]
        else:
            got = sum(_count(x, t) for x in tenants)
        rep(got == n, f"{t}: {n:,} rows loaded = rows in DB")
    with db.get_conn(DB) as conn:
        rep(conn.execute("PRAGMA foreign_key_check").fetchall() == [], "foreign keys clean")
        ev = conn.execute("SELECT COUNT(*) FROM lc_events").fetchone()[0]
        cu = conn.execute("SELECT SUM(ev_total) FROM cube_15m").fetchone()[0]
        rep(ev == cu, f"cube ev_total adds up to the events ({cu:,} of {ev:,})")
        rep(conn.execute("SELECT COUNT(*) FROM lc_events WHERE ingested_at < ts").fetchone()[0] == 0, "no event arrives before it happened")
        rep(conn.execute("SELECT COUNT(*) FROM anomaly_key_hidden").fetchone()[0] == 0
            or db.query_one(G, "SELECT value FROM lc_meta WHERE tenant_id=:tenant_id AND key='hidden_loaded'", database=DB)["value"] == "1",
            "hidden answer key is not in the database (public development mode)")
    return ok_all


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(HERE / "csv_lc04"))
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--add-hidden", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args()
    csv = Path(a.csv)
    db.init_db(DB)
    if a.add_hidden:
        if not a.confirm:
            sys.exit("--add-hidden is for the single final evaluation (step 12). Add --confirm if you really mean it.")
        return add_hidden(csv)
    if a.reset:
        reset()
    elif db.query_one(G, "SELECT COUNT(*) AS n FROM lc_meta WHERE tenant_id=:tenant_id", database=DB)["n"]:
        sys.exit("lc04.db already has data. Use --reset to replace it.")
    t0 = time.time()
    loaded, tenants = load_public(csv)
    print(f"loaded in {time.time() - t0:.0f}s: " + ", ".join(f"{k} {v:,}" for k, v in loaded.items()))
    t1 = time.time()
    rows = sum(cube.build_all(t) for t in tenants)
    print(f"cube built: {rows:,} rows in {time.time() - t1:.0f}s")
    ok = check(tenants, loaded)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
