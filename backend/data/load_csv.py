"""
Load CSVs into SQLite through db.py (tenant_id is added automatically).

Run:  python load_csv.py [--tenant abc_bank] [--csv <dir>] [--reset]
Same loader works for real datasets later: just point --csv at the real files
(column names must match schema.sql, minus tenant_id and auto ids).
"""
import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "db"))
import db  # noqa: E402

# Order matters: accounts first (other tables reference it)
TABLES = ["accounts", "repayments", "calls", "promises", "statement_txns", "gold_labels"]

# Columns that must be numbers / NULL when empty
NUMERIC = {
    "accounts": ["loan_amount", "emi_amount", "dpd", "outstanding"],
    "repayments": ["amount_paid"],
    "promises": ["promised_amount", "kept"],
    "statement_txns": ["pull_no", "amount", "balance"],
    "gold_labels": ["is_ambiguous"],
}
NULLABLE_TEXT = {"repayments": ["paid_date"]}


def read_rows(path: Path, table: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for col in NUMERIC.get(table, []):
                v = row.get(col)
                row[col] = None if v in (None, "") else float(v) if "." in v else int(v)
            for col in NULLABLE_TEXT.get(table, []):
                if row.get(col) == "":
                    row[col] = None
            rows.append(row)
    return rows


def reset_tenant(tenant: str) -> None:
    """Delete this tenant's input data (children first, then accounts)."""
    for t in reversed(TABLES):
        db.execute(tenant, f"DELETE FROM {t} WHERE tenant_id = :tenant_id")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="abc_bank")
    ap.add_argument("--csv", default=str(HERE / "csv"))
    ap.add_argument("--reset", action="store_true", help="delete this tenant's existing data first")
    args = ap.parse_args()

    csv_dir = Path(args.csv)
    missing = [t for t in TABLES if not (csv_dir / f"{t}.csv").exists()]
    if missing:
        sys.exit(f"Missing CSV files in {csv_dir}: {missing}")

    db.init_db()
    if args.reset:
        reset_tenant(args.tenant)

    for t in TABLES:
        n = db.insert_many(args.tenant, t, read_rows(csv_dir / f"{t}.csv", t))
        print(f"{t:15s} loaded {n:6d} rows")

    print(f"\nTenant '{args.tenant}' ready in {db.DB_PATH}")


if __name__ == "__main__":
    main()
