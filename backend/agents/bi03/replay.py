"""
BI-03 stream replay controls (used by the API / UI "advance one day" button).

  reset_to_day(tenant, day)   go back: everything the system produced AFTER that day is removed, as if the stream had only reached it
  advance(tenant, days)       go forward: run the daily runner for the next day(s) and turn the new flags into nudges
  status(tenant)              where the replay pointer is

The runner and the nudge engine never read outcome labels, so replaying through the hidden period does not look at hidden labels.
Outcomes (what happened to each flag) are NOT produced here: recording them past the last public day is a separate, guarded action.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import db  # noqa: E402

DB = "bi03"
LOCK = threading.Lock()                      # one replay action at a time
_ENGINES: dict = {}                          # tenant -> FeatureEngine (loaded once, about 1 GB for 6,000 accounts)


class ReplayError(Exception):
    pass


def _meta(tenant: str) -> dict:
    return {r["key"]: r["value"] for r in db.query(tenant, "SELECT key, value FROM bp_meta WHERE tenant_id = :tenant_id", database=DB)}


def status(tenant: str) -> dict:
    m = _meta(tenant)
    start = pd.Timestamp(m["stream_start_date"])
    day = lambda s: int((pd.Timestamp(s) - start).days + 1)
    first = db.query_one(tenant, "SELECT MIN(as_of_date) d FROM risk_scores WHERE tenant_id = :tenant_id", database=DB)["d"]
    last = db.query_one(tenant, "SELECT MAX(as_of_date) d FROM risk_scores WHERE tenant_id = :tenant_id", database=DB)["d"]
    pointer = m["replay_date"]
    return {"replay_date": pointer, "replay_day": day(pointer), "first_scored_day": day(first) if first else None, "last_scored_day": day(last) if last else None,
            "stream_start": m["stream_start_date"], "public_end_day": int(m["public_days"]), "stream_end_day": int(m["public_days"]) + int(m["hidden_days"]),
            "can_advance": day(pointer) < int(m["public_days"]) + int(m["hidden_days"]),
            "past_public_period": day(pointer) > int(m["public_days"])}


def reset_to_day(tenant: str, day: int) -> dict:
    """Remove everything produced after `day`, reopen flags that were only closed because of later misses, move the pointer."""
    import outcomes as O
    st = status(tenant)
    # upper bound = last day that was actually processed: going "back" to a day nothing was computed for would leave a gap in the scores
    if st["first_scored_day"] is None or not (st["first_scored_day"] <= day <= st["last_scored_day"]):
        raise ReplayError(f"day must be between {st['first_scored_day']} and {st['last_scored_day']} (the days already processed)")
    d = (pd.Timestamp(st["stream_start"]) + pd.Timedelta(days=day - 1)).strftime("%Y-%m-%d")
    with LOCK:
        q = lambda sql, **p: db.execute(tenant, sql, p, database=DB)
        gone = "SELECT flag_id FROM flags WHERE tenant_id = :tenant_id AND flagged_on > :d"
        n_flags = len(db.query(tenant, gone, {"d": d}, database=DB))          # count first: rowcount of the delete below is unreliable here
        n_nudges = q(f"DELETE FROM nudges WHERE tenant_id = :tenant_id AND flag_id IN ({gone})", d=d)
        q("DELETE FROM flag_outcomes WHERE tenant_id = :tenant_id AND (evaluated_on > :d OR flag_id IN (%s))" % gone, d=d)   # outcomes known only later would reveal the future
        q("DELETE FROM flags WHERE tenant_id = :tenant_id AND flagged_on > :d", d=d)
        n_scores = q("DELETE FROM risk_scores WHERE tenant_id = :tenant_id AND as_of_date > :d", d=d)
        runs = "SELECT run_id FROM agent_runs WHERE tenant_id = :tenant_id AND agent_name = 'bi03_daily' AND entity_id > :d"
        q(f"DELETE FROM agent_steps WHERE tenant_id = :tenant_id AND run_id IN ({runs})", d=d)
        n_runs = q("DELETE FROM agent_runs WHERE tenant_id = :tenant_id AND agent_name = 'bi03_daily' AND entity_id > :d", d=d)

        # a flag was closed because its account missed; if that miss happens after `d`, the flag is open again as of `d`
        missed = set(O.first_miss_dates(O.load_repayments(tenant), pd.Timestamp(d)).index)
        closed = db.query_df(tenant, "SELECT f.flag_id, f.account_id, (SELECT COUNT(*) FROM nudges n WHERE n.flag_id = f.flag_id AND n.gate_passed = 1) AS has_nudge "
                                     "FROM flags f WHERE f.tenant_id = :tenant_id AND f.status = 'closed'", database=DB)
        reopen = closed[~closed["account_id"].isin(missed)]
        for status_, ids in (("nudged", reopen.loc[reopen["has_nudge"] > 0, "flag_id"]), ("open", reopen.loc[reopen["has_nudge"] == 0, "flag_id"])):
            for i in range(0, len(ids), 500):
                chunk = {f"f{j}": int(v) for j, v in enumerate(ids.iloc[i:i + 500])}
                if chunk:
                    db.execute(tenant, f"UPDATE flags SET status = '{status_}' WHERE tenant_id = :tenant_id AND flag_id IN ({', '.join(':' + k for k in chunk)})", chunk, database=DB)
        q("UPDATE bp_meta SET value = :d WHERE tenant_id = :tenant_id AND key = 'replay_date'", d=d)
    return {"replay_day": day, "replay_date": d, "removed": {"risk_scores": n_scores, "flags": n_flags, "nudges": n_nudges, "daily_runs": n_runs}, "reopened_flags": int(len(reopen))}


def advance(tenant: str, days: int = 1, confirm: bool = True) -> dict:
    """Run the daily runner for the next `days` day(s), then create nudges for the new flags."""
    import features as F
    import nudge as N
    import runner as R
    if not 1 <= days <= 10:
        raise ReplayError("days must be between 1 and 10")
    st = status(tenant)
    if not st["can_advance"]:
        raise ReplayError("the stream has reached its last day")
    first, last = st["replay_day"] + 1, min(st["replay_day"] + days, st["stream_end_day"])
    with LOCK:
        eng = _ENGINES.get(tenant) or _ENGINES.setdefault(tenant, F.FeatureEngine.from_db(tenant))
        ran = R.run_range(tenant, first, last, use_cache=True, eng=eng, quiet=True, confirm=confirm)
        nudged = N.process_flags(tenant, eng=eng)
    return {"ran_days": [r["day"] for r in ran], "new_flags": int(sum(r["flags"] for r in ran)), "flags_closed": int(sum(r["closed"] for r in ran)),
            "new_nudges": nudged.get("flags", 0), "replay": status(tenant)}
