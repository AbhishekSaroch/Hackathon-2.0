"""
Load the BI-03 CSVs into bi03.db (through db.py, so tenant_id is added automatically).

  python load_bi03.py [--tenant abc_bank] [--csv <dir>] [--reset] [--map mapping.json]

- --reset   deletes ALL BI-03 rows of this tenant first (inputs AND outputs such as flags / nudges,
            because they would no longer match the new data). BI-02's database is never touched.
- --map     optional JSON {"table": {"csv_column": "db_column"}} so a real file with different column
            names can be loaded without changing code (the brief's real schema is unknown to us).
- Loading twice without --reset is refused (events have auto ids, so a second load would silently double them).
After loading it runs integrity checks and exits with code 1 if any fails.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "db"))
import db  # noqa: E402

DB = "bi03"
CHUNK = 50_000

# table -> (csv file, column types). Order matters: parents first (foreign keys).
TABLES = {
    "bp_accounts": ("bp_accounts.csv", {"loan_amount": float, "emi_amount": float, "dpd": int, "outstanding": float}),
    "service_templates": ("service_templates.csv", {}),
    "outcome_labels": ("outcome_labels.csv", {}),
    "outcome_labels_hidden": ("outcome_labels_hidden.csv", {}),
    "bureau_snapshots": ("bureau_snapshots.csv", {"card_utilisation": float, "enquiries_30d": int, "open_loans": int,
                                                  "other_loan_dpd": int, "other_loan_bounces_90d": int, "bureau_score": int}),
    "events": ("events.csv", {"amount": float, "value": float, "balance": float}),
    "bp_meta": ("bp_meta.csv", {}),
}
# every BI-03 table, children first, for --reset
ALL_TABLES = ["nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs",
              "events", "bureau_snapshots", "outcome_labels", "outcome_labels_hidden", "service_templates",
              "bp_meta", "bp_accounts"]


def read_rows(path: Path, types: dict, rename: dict):
    """Yield dicts: empty text becomes NULL, numbers are converted, columns renamed if a mapping was given."""
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = {}
            for col, val in raw.items():
                name = rename.get(col, col)
                if val == "":
                    row[name] = None
                elif name in types:
                    row[name] = types[name](float(val)) if types[name] is int else types[name](val)
                else:
                    row[name] = val
            yield row


def load_table(tenant: str, table: str, path: Path, types: dict, rename: dict) -> int:
    total, chunk = 0, []
    for row in read_rows(path, types, rename):
        chunk.append(row)
        if len(chunk) >= CHUNK:
            total += db.insert_many(tenant, table, chunk, database=DB)
            chunk = []
    if chunk:
        total += db.insert_many(tenant, table, chunk, database=DB)
    return total


def count(tenant: str, table: str) -> int:
    return db.query_one(tenant, f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = :tenant_id", database=DB)["n"]


def check(tenant: str, loaded: dict) -> bool:
    q = lambda sql: db.query_one(tenant, sql, database=DB)["n"]
    meta = {r["key"]: r["value"] for r in db.query(tenant, "SELECT key, value FROM bp_meta WHERE tenant_id=:tenant_id", database=DB)}
    results = []
    for t, n in loaded.items():
        results.append((count(tenant, t) == n, f"{t}: {n:,} rows loaded = rows in DB"))
    results += [
        (q("SELECT COUNT(*) n FROM bp_accounts WHERE tenant_id=:tenant_id AND (status != 'current' OR dpd != 0)") == 0, "every account is current at the start"),
        (q("SELECT COUNT(*) n FROM events WHERE tenant_id=:tenant_id AND available_from < event_date") == 0, "no event is available before it happened"),
        (q("SELECT COUNT(*) n FROM bureau_snapshots WHERE tenant_id=:tenant_id AND available_from <= snapshot_date") == 0, "every bureau snapshot arrives after its as-of date"),
        (q(f"SELECT COUNT(*) n FROM events WHERE tenant_id=:tenant_id AND event_date > '{meta.get('stream_end_date', '9999')}'") == 0, "no event after the stream end"),
        (q("SELECT COUNT(*) n FROM outcome_labels o JOIN outcome_labels_hidden h ON h.account_id = o.account_id "
           "AND h.tenant_id = o.tenant_id WHERE o.tenant_id=:tenant_id AND o.dpd1_date IS NOT NULL AND h.dpd1_date IS NOT NULL") == 0, "no account has both a public and a hidden miss"),
        (q(f"SELECT COUNT(*) n FROM outcome_labels WHERE tenant_id=:tenant_id AND dpd1_date > '{meta.get('public_end_date', '0')}'") == 0, "public labels stay inside the public period"),
        (q(f"SELECT COUNT(*) n FROM outcome_labels_hidden WHERE tenant_id=:tenant_id AND dpd1_date <= '{meta.get('public_end_date', '0')}' AND dpd1_date IS NOT NULL") == 0, "hidden labels stay inside the hidden period"),
        (q("SELECT COUNT(DISTINCT nudge_type) n FROM service_templates WHERE tenant_id=:tenant_id") == 3, "templates cover all 3 nudge types"),
        (all(k in meta for k in ("stream_start_date", "public_days", "hidden_days", "replay_date")), "stream settings present in bp_meta"),
    ]
    with db.get_conn(DB) as conn:
        results.append((not conn.execute("PRAGMA foreign_key_check").fetchall(), "foreign keys all resolve"))
    for ok, name in results:
        print(("PASS  " if ok else "FAIL  ") + name)
    return all(ok for ok, _ in results)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="abc_bank")
    ap.add_argument("--csv", default=str(HERE / "csv_bi03"))
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--map", help="JSON file: {table: {csv_column: db_column}}")
    args = ap.parse_args()
    csv_dir, T = Path(args.csv), args.tenant
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else {}

    missing = [f for f, _ in TABLES.values() if not (csv_dir / f).exists()]
    if missing:
        print(f"Missing CSV files in {csv_dir}: {missing}\nGenerate them first: python generate_bi03.py")
        return 2

    db.init_db(DB)
    if args.reset:
        for t in ALL_TABLES:
            db.execute(T, f"DELETE FROM {t} WHERE tenant_id = :tenant_id", database=DB)
        print(f"reset: all BI-03 rows of tenant '{T}' deleted")
    elif count(T, "bp_accounts"):
        print(f"bi03.db already has data for tenant '{T}'. Re-run with --reset to replace it (a second load would double the events).")
        return 2

    loaded, t0 = {}, time.time()
    for table, (fname, types) in TABLES.items():
        t1 = time.time()
        loaded[table] = load_table(T, table, csv_dir / fname, types, mapping.get(table, {}))
        print(f"{table:22s} loaded {loaded[table]:9,d} rows  ({time.time() - t1:5.1f}s)")
    print(f"total {time.time() - t0:.1f}s | {db._paths(DB)[0]} = {db._paths(DB)[0].stat().st_size / 1e6:.0f} MB\n")
    return 0 if check(T, loaded) else 1


if __name__ == "__main__":
    sys.exit(main())
