"""
Copy the tables of BI-02, BI-03 and LC-04 into ONE read-only database for "Ask your portfolio":  backend/ask_all.db

    python backend/data/sync_all.py            # build (or rebuild) ask_all.db
    python backend/data/sync_all.py --check    # only compare row counts of the copy with the three sources

What it does and does not do:
  - the three source databases are opened READ-ONLY (SQLite mode=ro): nothing in them can be changed or deleted
  - every table is copied under a prefix: bi02_*, bi03_*, lc04_* (names like `loans` or `agent_runs` exist in several modules)
  - tables without a tenant_id column are skipped (they could not be tenant-scoped)
  - answer keys and contact logs are never copied: gold_labels, outcome_labels_hidden, anomaly_key, anomaly_key_hidden, outreach_log
  - the new file is built beside the old one and swapped in only when every table's row count matches its source
  - the LC-01 warehouse (lc01.db) is NOT copied: Ask keeps reading it directly, so its evaluation is untouched
Run it again whenever BI-03 "advance one day" or the LC-04 replay has moved the data; the answers show "data as of" from this copy.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

SOURCES = {"bi02": db.DB_PATH, "bi03": db.BI03_DB_PATH, "lc04": db.LC04_DB_PATH}
NEVER_COPY = {"gold_labels", "outcome_labels_hidden", "anomaly_key", "anomaly_key_hidden", "outreach_log"}


def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def source_tables(prefix: str, path: Path) -> list[tuple[str, int]]:
    """[(table, rows)] that would be copied from one source (read-only)."""
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        out = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1").fetchall():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            if "tenant_id" not in cols or t in NEVER_COPY:
                continue
            out.append((t, con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]))
        return out
    finally:
        con.close()


def check(target: Path | None = None) -> list[str]:
    """Problems found comparing the copy with the sources (empty list = identical row counts)."""
    target = target or db.ASK_DB_PATH
    if not target.exists():
        return [f"{target.name} does not exist; run sync_all.py"]
    con = sqlite3.connect(_ro_uri(target), uri=True)
    bad = []
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for prefix, path in SOURCES.items():
            for t, n in source_tables(prefix, path):
                name = f"{prefix}_{t}"
                if name not in have:
                    bad.append(f"{name} is missing")
                    continue
                m = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                if m != n:
                    bad.append(f"{name}: copy has {m} rows, source has {n} (source moved since the last sync)")
    finally:
        con.close()
    return bad


def build() -> dict:
    target = db.ASK_DB_PATH
    tmp = target.with_name(target.name + ".new")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(tmp) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.time()
    con = sqlite3.connect(f"{tmp.resolve().as_uri()}?mode=rwc", uri=True)
    report = {}
    try:
        for prefix, path in SOURCES.items():
            if not path.exists():
                raise SystemExit(f"source database {path} not found")
            con.execute("ATTACH DATABASE ? AS src", (_ro_uri(path),))   # the source is attached read-only: nothing can be written to it
            for t, n in source_tables(prefix, path):
                name = f"{prefix}_{t}"
                con.execute(f'DROP TABLE IF EXISTS "{name}"')
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM src."{t}"')
                con.execute(f'CREATE INDEX "ix_{name}_tenant" ON "{name}" (tenant_id)')
                got = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                if got != n:
                    raise SystemExit(f"{name}: copied {got} rows but the source has {n}; nothing was swapped in")
                report[name] = got
                print(f"  {name:34s} {got:>9,d} rows")
            con.commit()
            con.execute("DETACH DATABASE src")
        con.execute("CREATE TABLE ask_meta (tenant_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT)")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [("GLOBAL", "synced_at", stamp)] + [("GLOBAL", f"rows:{k}", str(v)) for k, v in report.items()]
        con.executemany("INSERT INTO ask_meta VALUES (?,?,?)", rows)
        con.commit()
        con.execute("PRAGMA journal_mode = DELETE")
    finally:
        con.close()
    bad = check(tmp)
    if bad:
        raise SystemExit("copy does not match the sources:\n  " + "\n  ".join(bad))
    for suffix in ("-wal", "-shm"):                                  # an old copy's side files must not outlive it
        old = Path(str(target) + suffix)
        if old.exists():
            old.unlink()
    os.replace(tmp, target)
    return dict(tables=len(report), rows=sum(report.values()), seconds=round(time.time() - t0, 1), synced_at=stamp)


if __name__ == "__main__":
    if "--check" in sys.argv:
        problems = check()
        print("copy matches the sources" if not problems else "\n".join(problems))
        sys.exit(1 if problems else 0)
    print(f"copying read-only from {', '.join(f'{k} ({v.name})' for k, v in SOURCES.items())} into {db.ASK_DB_PATH.name}")
    print(build())
