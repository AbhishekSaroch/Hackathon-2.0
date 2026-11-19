"""
BI-03 daily runner tests.   python test_bi03_runner.py   (exit code 1 on any failure)

  A status        who is "current" is decided from visible repayment events only (point-in-time, matches the public labels)
  B behaviour     deterministic, cooldown works, and the runner flags the same accounts as the backtest (public days)
  C database      a replay on a small separate tenant: no duplicates, idempotent, flags close when an account misses,
                  and the whole run works with the outcome tables blocked at database level
"""
import shutil
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import db  # noqa: E402
import features as F  # noqa: E402
import model as M  # noqa: E402
import runner as R  # noqa: E402

T = "abc_bank"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


eng = F.FeatureEngine.from_db(T)
ev = eng.events
bundle = M.load_bundle("bi03-v1")
start = M.stream_start(T)
store = M.FeatureStore(eng, T, start)
pub = M.miss_day_series(T, start)                                       # PUBLIC labels only (days <= 90)
day = lambda n: M.day_to_date(start, n)

# ================================================================ A  status
for n in (45, 75, 90):
    D = day(n)
    full = set(R.not_current_accounts(ev[ev["available_from"] <= D], D))
    trunc = set(R.not_current_accounts(ev[ev["available_from"] <= D].copy(), D))
    poisoned = ev.copy()
    fut = poisoned["available_from"] > D
    poisoned.loc[fut, "event_type"] = poisoned.loc[fut, "event_type"].map({"emi_paid": "emi_due", "emi_due": "emi_paid"}).fillna(poisoned.loc[fut, "event_type"])
    pois = set(R.not_current_accounts(poisoned[poisoned["available_from"] <= D], D))
    ok(f"A status at day {n}: same answer on truncated data and with the future wrecked", full == trunc == pois, f"{len(full)} not current")

    should = set(pub.index[(pub <= n - (R.GRACE_DAYS + 1))])             # missed long enough ago to be past the grace period
    ever_missed = set(pub.index[pub <= n])
    ok(f"A status at day {n}: every account that missed more than {R.GRACE_DAYS + 1} days ago is detected", should <= full, f"{len(should)} of {len(should)}" if should <= full else f"missing {len(should - full)}")
    ok(f"A status at day {n}: nobody who never missed is wrongly dropped (100% precise)", full <= ever_missed, f"{len(full - ever_missed)} wrong")

# ================================================================ B  behaviour
t = day(120)
r1, r2 = R.compute_day(eng, bundle, t, pd.Series(dtype="datetime64[ns]")), R.compute_day(eng, bundle, t, pd.Series(dtype="datetime64[ns]"))
ok("B the same day gives the same scores and flags", np.allclose(r1["scores"]["score"], r2["scores"]["score"]) and list(r1["flags"].index) == list(r2["flags"].index))
ok("B every new flag is at or above the alert threshold tau", (r1["flags"]["score"] >= bundle["tau"]).all() and len(r1["flags"]) > 0, f"{len(r1['flags'])} flags")
ok("B every new flag names an actionable signal (not bureau_score / static)", r1["flags"]["dominant_signal"].notna().all() and not ({"bureau_score", "static"} & set(r1["flags"]["dominant_signal"])),
   str(r1["flags"]["dominant_signal"].value_counts().to_dict()))

flagged = r1["flags"].index
last = pd.Series(t, index=flagged)
soon = R.compute_day(eng, bundle, day(125), last)                       # 5 days later: inside the 30-day cooldown
ok("B cooldown: accounts flagged 5 days ago are not flagged again", not (set(flagged) & set(soon["flags"].index)))
ok("B cooldown: and the step log says how many were held back", soon["steps"][4]["output"]["suppressed_by_cooldown"] > 0, str(soon["steps"][4]["output"]))
later = R.compute_day(eng, bundle, day(150), pd.Series(day(110), index=flagged))                       # flagged 40 days earlier
ok("B cooldown: after 30+ days an account can be flagged again", later["steps"][4]["output"]["suppressed_by_cooldown"] == 0)

# runner vs the backtest's own flagging rule, on PUBLIC days only (no hidden labels involved)
tau, first_runner, first_bt, last_flag = bundle["tau"], {}, {}, pd.Series(dtype="datetime64[ns]")
for n in range(1, 91):
    res = R.compute_day(eng, bundle, day(n), last_flag, features=store.get(n))
    for a in res["flags"].index:
        first_runner.setdefault(a, n)
        last_flag.loc[a] = day(n)
    s = store.get(n)
    md = pub.reindex(s.index)
    cur = s[(md.isna() | (md > n)).to_numpy()]
    sc = M.score(bundle["model"], cur)
    for a in sc.index[sc.to_numpy() >= tau]:
        first_bt.setdefault(a, n)
both = set(first_runner) & set(first_bt)
same_day = np.mean([first_runner[a] == first_bt[a] for a in both])
ok("B runner and backtest flag the same accounts on public days 1-90 (>= 97% overlap)", len(both) / max(len(set(first_runner) | set(first_bt)), 1) >= 0.97,
   f"{len(first_runner)} vs {len(first_bt)} accounts, {len(both)} in common, {same_day:.1%} on the same day")

# ================================================================ C  database (small separate tenant, real outputs untouched)
TEN = "t_run"
SCR = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\mini_run")
shutil.rmtree(SCR, ignore_errors=True)
py = lambda *a: subprocess.run([sys.executable, *map(str, a)], cwd=BACKEND / "data", capture_output=True, text=True)
g = py("generate_bi03.py", "--n", 300, "--seed", 11, "--id-start", 8_000_000, "--out", SCR)
l = py("load_bi03.py", "--csv", SCR, "--tenant", TEN, "--reset")
ok("C small test tenant generated and loaded", g.returncode == 0 and l.returncode == 0)
before = {x: db.query_one(T, f"SELECT COUNT(*) n FROM {x} WHERE tenant_id=:tenant_id", database="bi03")["n"] for x in ("risk_scores", "flags", "agent_runs")}

BLOCKED = ("outcome_labels", "outcome_labels_hidden")
orig = db.get_conn
def guarded(database="main"):
    c = orig(database)
    c.set_authorizer(lambda action, a1, a2, dbn, src: sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and a1 in BLOCKED) else sqlite3.SQLITE_OK)
    return c
db.get_conn = guarded
try:
    res = R.run_range(TEN, 61, 90, reset=True, use_cache=False, quiet=True)
    ran = len(res) == 30
except sqlite3.DatabaseError as e:
    ran, res = False, []
finally:
    db.get_conn = orig
ok("C a 30-day replay runs with the outcome tables blocked at database level (runner reads no labels)", ran)

q = lambda sql, **p: db.query_df(TEN, sql, p, database="bi03")
runs = q("SELECT COUNT(*) n FROM agent_runs WHERE tenant_id=:tenant_id AND agent_name='bi03_daily' AND status='done'").n[0]
ok("C one finished agent run per day", runs == 30, str(runs))
steps = q("SELECT COUNT(DISTINCT tool) n FROM agent_steps WHERE tenant_id=:tenant_id").n[0]
ok("C every day logs the 7 steps (ingest, features, status, score, flag, explain, write)", steps == 7)
dups = q("SELECT COUNT(*) n FROM (SELECT account_id, as_of_date FROM risk_scores WHERE tenant_id=:tenant_id GROUP BY 1,2 HAVING COUNT(*) > 1)").n[0]
ok("C no account has two scores for the same day", dups == 0)
cool = q("SELECT COUNT(*) n FROM flags a JOIN flags b ON a.account_id=b.account_id AND a.tenant_id=b.tenant_id AND b.flag_id > a.flag_id "
         "AND julianday(b.flagged_on) - julianday(a.flagged_on) < 30 WHERE a.tenant_id=:tenant_id").n[0]
ok("C no account is flagged twice within the 30-day cooldown", cool == 0)
bad = q("SELECT COUNT(*) n FROM flags f LEFT JOIN risk_scores r ON r.account_id=f.account_id AND r.as_of_date=f.flagged_on AND r.tenant_id=f.tenant_id "
        "WHERE f.tenant_id=:tenant_id AND (r.score IS NULL OR ABS(r.score - f.score) > 1e-9 OR f.score < :tau OR f.dominant_signal IS NULL)", tau=bundle["tau"]).n[0]
ok("C every flag matches that day's stored score, is above tau and has a dominant signal", bad == 0)

pointer = q("SELECT value v FROM bp_meta WHERE tenant_id=:tenant_id AND key='replay_date'").v[0]
ok("C the replay pointer in bp_meta moved to the last replayed day", pointer == str(day(90).date()), pointer)

e2 = F.FeatureEngine.from_db(TEN)
gone = {str(a) for a in R.not_current_accounts(e2.events[e2.events["available_from"] <= day(90)], day(90))}
open_gone = set(q("SELECT account_id FROM flags WHERE tenant_id=:tenant_id AND status IN ('open','nudged')").account_id) & gone
ok("C flags of accounts that stopped being current are closed", not open_gone, f"{len(gone)} accounts not current at the end")

n1 = q("SELECT COUNT(*) n FROM risk_scores WHERE tenant_id=:tenant_id").n[0]
R.run_range(TEN, 61, 90, reset=False, use_cache=False, quiet=True)
ok("C running the same days again adds nothing (idempotent)", q("SELECT COUNT(*) n FROM risk_scores WHERE tenant_id=:tenant_id").n[0] == n1)

after = {x: db.query_one(T, f"SELECT COUNT(*) n FROM {x} WHERE tenant_id=:tenant_id", database="bi03")["n"] for x in ("risk_scores", "flags", "agent_runs")}
ok("C the real tenant's outputs were not touched by the test", before == after)

for tb in ("nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs", "events", "bureau_snapshots", "outcome_labels",
           "outcome_labels_hidden", "service_templates", "bp_meta", "bp_accounts"):
    db.execute(TEN, f"DELETE FROM {tb} WHERE tenant_id=:tenant_id", database="bi03")
shutil.rmtree(SCR, ignore_errors=True)

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
