"""
One-time migration of an EXISTING lc04.db for legal communications (a fresh database gets this from schema_lc04.sql):
  - lc_events: event_type may now also be 'legal' (SQLite cannot alter a CHECK, so the table is rebuilt and the rows copied)
  - cube_15m: two new counters legal_sent and legal_out_of_window (existing rows get 0: there were no legal events before)
Safe to run again (it does nothing when the database is already migrated).    python backend/db/migrate_lc04_legal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402


def migrate(database="lc04"):
    done = []
    conn = db.get_conn(database)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='lc_events'").fetchone()
    if sql and "'legal'" not in sql[0]:
        conn.execute("PRAGMA foreign_keys = OFF")
        n = conn.execute("SELECT COUNT(*) FROM lc_events").fetchone()[0]
        conn.execute("ALTER TABLE lc_events RENAME TO lc_events_old")
        for ix in ("ix_lc_events_acct", "ix_lc_events_ts", "ix_lc_events_ing", "ix_lc_events_fk"):
            conn.execute(f"DROP INDEX IF EXISTS {ix}")
        conn.executescript((Path(db.__file__).resolve().parent / "schema_lc04.sql").read_text(encoding="utf-8"))   # recreates lc_events + indexes
        conn.execute("INSERT INTO lc_events SELECT * FROM lc_events_old")
        assert conn.execute("SELECT COUNT(*) FROM lc_events").fetchone()[0] == n, "row count changed during the migration"
        conn.execute("DROP TABLE lc_events_old")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        done.append(f"lc_events rebuilt ({n:,} rows kept)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cube_15m)")}
    for c in ("legal_sent", "legal_out_of_window"):
        if c not in cols:
            conn.execute(f"ALTER TABLE cube_15m ADD COLUMN {c} INTEGER NOT NULL DEFAULT 0")
            done.append(f"cube_15m.{c} added")
    conn.commit()
    conn.close()
    return done


if __name__ == "__main__":
    print(migrate() or "already migrated")
