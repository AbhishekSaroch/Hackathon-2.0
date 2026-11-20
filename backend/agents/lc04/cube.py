"""
LC-04 15-minute cube. The ONLY thing dashboards and the detector read (never raw events).

Bucketing is by ARRIVAL time (lc_events.ingested_at), the moment the system saw the event. That makes a replay
point-in-time (a bucket is complete once the clock passes its end) and lets a stalled feed show up as a gap
followed by a catch-up burst. For normal events arrival is seconds after the event, so it matches event time.

Every cube row is one segment in one 15-minute bucket: (lender = tenant, city, agency, dialer, gateway, product, DPD bucket)
with base counters. Governed metrics (step 4) are formulas over these counters. '-' means "not applicable to this event".

    refresh_bucket(tenant, "2026-10-15 11:00:00")      rebuild one bucket (idempotent, one transaction)
    build_range(tenant, start, end)                    rebuild whole buckets in [start, end)
    build_all(tenant)                                  rebuild every bucket that has events
All SQL is tenant-scoped through db.py's rules (":tenant_id" required, tenant bound by the module).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db  # noqa: E402

DB = "lc04"
BUCKET_MIN = 15
FMT = "%Y-%m-%d %H:%M:%S"
LEGAL_START_HOUR, LEGAL_END_HOUR = 8, 20          # the permitted window for legal communications: 08:00 to 20:00 (send time)
_SEND_HOUR = "CAST(substr(e.ts,12,2) AS INT)"

# base counters, in cube column order: (column, SQL condition on the event row, or None for COUNT(*)), amount columns separate
COUNTERS = [
    ("ev_total", None),
    ("contacts_attempted", "e.event_type='contact'"),
    ("contacts_connected", "e.event_type='contact' AND e.outcome='connected'"),
    ("disp_total", "e.event_type='disposition'"),
    ("disp_rpc", "e.event_type='disposition' AND e.outcome='rpc'"),
    ("disp_ptp", "e.event_type='disposition' AND e.outcome='ptp'"),
    ("disp_refused", "e.event_type='disposition' AND e.outcome='refused'"),
    ("disp_dispute", "e.event_type='disposition' AND e.outcome='dispute'"),
    ("disp_other", "e.event_type='disposition' AND e.outcome IN ('wrong_number','callback')"),
    ("ptp_created", "e.event_type='ptp' AND e.outcome='created'"),
    ("ptp_kept", "e.event_type='ptp' AND e.outcome='kept'"),
    ("ptp_broken", "e.event_type='ptp' AND e.outcome='broken'"),
    ("pay_attempts", "e.event_type='payment'"),
    ("pay_success", "e.event_type='payment' AND e.outcome='success'"),
    ("pay_failed", "e.event_type='payment' AND e.outcome='failed'"),
    ("legal_sent", "e.event_type='legal'"),
    ("legal_out_of_window", f"e.event_type='legal' AND ({_SEND_HOUR} < {LEGAL_START_HOUR} OR {_SEND_HOUR} >= {LEGAL_END_HOUR})"),
]
AMOUNTS = [  # (column, SQL condition) -> SUM(amount)
    ("ptp_amount", "e.event_type='ptp' AND e.outcome='created'"),
    ("pay_amount", "e.event_type='payment' AND e.outcome='success'"),
]
DIMS = ["city", "agency_id", "dialer_id", "gateway_id", "product", "bucket"]
COUNTER_COLS = [c for c, _ in COUNTERS] + [c for c, _ in AMOUNTS]


def _sum(cond):
    return f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"


def _build_sql():
    cols = ["tenant_id", "bucket_ts", "day", "slot", "dow"] + DIMS + COUNTER_COLS
    sel = [":tenant_id", "e.bucket_ts", "substr(e.bucket_ts,1,10)",
           "CAST(substr(e.bucket_ts,12,2) AS INT)*4 + CAST(substr(e.bucket_ts,15,2) AS INT)/15",
           "(CAST(strftime('%w', substr(e.bucket_ts,1,10)) AS INT)+6)%7",          # 0 = Monday .. 6 = Sunday
           "a.city", "COALESCE(e.agency_id,'-')", "COALESCE(e.dialer_id,'-')", "COALESCE(e.gateway_id,'-')",
           "a.product", "a.bucket"]
    sel += ["COUNT(*)" if cond is None else _sum(cond) for _, cond in COUNTERS]
    sel += [f"COALESCE(SUM(CASE WHEN {cond} THEN e.amount ELSE 0 END),0)" for _, cond in AMOUNTS]
    return f"""
INSERT INTO cube_15m ({', '.join(cols)})
SELECT {', '.join(sel)}
FROM (SELECT x.*, strftime('%Y-%m-%d %H:', x.ingested_at)
             || substr('00153045', (CAST(strftime('%M', x.ingested_at) AS INT)/15)*2+1, 2) || ':00' AS bucket_ts
      FROM lc_events x
      WHERE x.tenant_id = :tenant_id AND x.ingested_at >= :s AND x.ingested_at < :e) e
JOIN lc_accounts a ON a.account_id = e.account_id AND a.tenant_id = e.tenant_id
GROUP BY 2, 6, 7, 8, 9, 10, 11
"""


SQL_INSERT = _build_sql()
SQL_DELETE = "DELETE FROM cube_15m WHERE tenant_id = :tenant_id AND bucket_ts >= :s AND bucket_ts < :e"


def floor_bucket(ts) -> str:
    return pd.Timestamp(ts).floor(f"{BUCKET_MIN}min").strftime(FMT)


def build_range(tenant: str, start, end, database: str = DB) -> int:
    """Rebuild whole 15-minute buckets in [start, end). Delete + insert run in ONE transaction. Returns rows written."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if s != s.floor(f"{BUCKET_MIN}min") or e != e.floor(f"{BUCKET_MIN}min"):
        raise ValueError("start and end must be on a 15-minute boundary")
    params = {"s": s.strftime(FMT), "e": e.strftime(FMT)}
    db._check_scoped(SQL_INSERT, params)           # same tenant rules as every other query
    params["tenant_id"] = tenant
    with db.get_conn(database) as conn:
        conn.execute(SQL_DELETE, params)
        return conn.execute(SQL_INSERT, params).rowcount


def refresh_bucket(tenant: str, bucket_start, database: str = DB) -> int:
    s = pd.Timestamp(bucket_start)
    return build_range(tenant, s, s + pd.Timedelta(minutes=BUCKET_MIN), database)


def build_all(tenant: str, since=None, database: str = DB) -> int:
    """Rebuild every bucket that has events (day by day), optionally only from `since` on."""
    r = db.query_one(tenant, "SELECT MIN(ingested_at) AS lo, MAX(ingested_at) AS hi FROM lc_events WHERE tenant_id=:tenant_id",
                     database=database)
    if not r or not r["lo"]:
        return 0
    lo, hi = pd.Timestamp(r["lo"]).normalize(), pd.Timestamp(r["hi"]).normalize() + pd.Timedelta(days=1)
    if since is not None:
        lo = max(lo, pd.Timestamp(since).normalize())
    rows, d = 0, lo
    while d < hi:
        rows += build_range(tenant, d, d + pd.Timedelta(days=1), database)
        d += pd.Timedelta(days=1)
    return rows
