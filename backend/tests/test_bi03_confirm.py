"""
BI-03 borderline-confirmation tests.   python test_bi03_confirm.py   (exit code 1 on any failure)

The fresh pull must stay point-in-time (it may reveal rows that exist today but are not yet in the daily feed, never anything dated after today).
  A tools        what the mock bureau / AA tools return, and that they never reach into the future
  B features     features built with a pull are unchanged by wrecking or deleting everything dated after today
  C decision     only borderline accounts are touched, the four outcomes follow the threshold, pulls stay a small share of accounts
  D runner       confirm is optional, logged step by step, and writes an audit trail to the database
"""
import shutil
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import confirm as C  # noqa: E402
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


def same(a, b) -> bool:
    try:
        assert_frame_equal(a, b, rtol=1e-9, atol=1e-9)
        return True
    except AssertionError:
        return False


eng = F.FeatureEngine.from_db(T)
ev, sn, ac = eng.events, eng.snaps, eng.accounts
bundle = M.load_bundle("bi03-v1")
start = M.stream_start(T)
store = M.FeatureStore(eng, T, start)
day = lambda n: M.day_to_date(start, n)

# ================================================================ A  the mock tools
for n in (60, 100, 130):
    D = day(n)
    b, a = C.bureau_refresh(ev, sn, D), C.aa_statement(ev, D)
    e_b, s_b, e_a = b["events"], b["snaps"], a["events"]
    ok(f"A day {n}: bureau pull returns only bureau rows dated up to today that the feed has not ingested yet",
       (e_b["source"] == "bureau").all() and (e_b["event_date"] <= D).all() and (s_b["snapshot_date"] <= D).all()
       and len(e_b) == int(((ev["source"] == "bureau") & (ev["event_date"] <= D) & (ev["available_from"] > D)).sum()), f"{len(e_b)} events + {len(s_b)} snapshots")
    ok(f"A day {n}: AA statement pull returns only statement rows dated up to today that are not posted yet", (e_a["source"] == "statement").all() and (e_a["event_date"] <= D).all(), f"{len(e_a)} rows")
    ok(f"A day {n}: nothing dated after today is ever returned, and returned rows are stamped as available today",
       max(e_b["event_date"].max() if len(e_b) else D, s_b["snapshot_date"].max() if len(s_b) else D, e_a["event_date"].max() if len(e_a) else D) <= D
       and (e_b["available_from"] == D).all() and (s_b["available_from"] == D).all() and (e_a["available_from"] == D).all())
ok("A the pulls really return something new on a typical day (otherwise the step would be pointless)", len(C.bureau_refresh(ev, sn, day(100))["events"]) + len(C.bureau_refresh(ev, sn, day(100))["snaps"]) > 0)
ok("A tool choice follows the dominant signal (salary / late EMI / balance -> AA; bounce / card / enquiries / score -> bureau; unknown -> both)",
   C.choose_tools("salary_delay") == ("aa_statement",) and C.choose_tools("other_loan_bounce") == ("bureau_refresh",)
   and C.choose_tools("card_utilisation") == ("bureau_refresh",) and C.choose_tools("late_emi_payments") == ("aa_statement",) and set(C.choose_tools("none")) == set(C.BOTH))

# ================================================================ B  fresh features stay point-in-time
rng = np.random.default_rng(3)
ids = list(rng.choice(ac["account_id"].values, 400, replace=False))
for n in (60, 100, 130):
    D = day(n)
    e, s, a_ = ev[ev["account_id"].isin(ids)], sn[sn["account_id"].isin(ids)], ac[ac["account_id"].isin(ids)]
    full, pulled = C.fresh_features(e, s, a_, D, C.BOTH)
    cut = C.fresh_features(e[e["event_date"] <= D], s[s["snapshot_date"] <= D], a_, D, C.BOTH)[0]            # drop everything dated after today
    pe, ps = e.copy(), s.copy()
    fut_e, fut_s = pe["event_date"] > D, ps["snapshot_date"] > D                                            # wreck everything dated after today
    pe.loc[fut_e, "amount"], pe.loc[fut_e, "value"], pe.loc[fut_e, "balance"] = 1e9, 99.0, 1.0
    ps.loc[fut_s, "card_utilisation"], ps.loc[fut_s, "bureau_score"] = 0.99, 300
    ok(f"B day {n}: fresh features are identical when everything dated after today is deleted", same(full, cut))
    ok(f"B day {n}: ... and when everything dated after today is wrecked", same(full, C.fresh_features(pe, ps, a_, D, C.BOTH)[0]))
    normal = F.compute_features(e, s, a_, D)
    changed = (~np.isclose(normal.fillna(-9e9).to_numpy(), full.fillna(-9e9).to_numpy())).any(axis=1)
    ok(f"B day {n}: a pull changes the features of some accounts and leaves the rest alone", 0 < changed.sum() < len(changed), f"{int(changed.sum())} of {len(changed)} accounts changed")

# ================================================================ C  the decision
for n in (70, 100):
    D = day(n)
    X = store.get(n)
    cur = X[~X.index.isin(R.not_current_accounts(ev[ev["available_from"] <= D], D))]
    s0 = M.score(bundle["model"], cur)
    res = C.confirm_borderline(eng, bundle, D, cur, s0)
    dec, tau = res["decisions"], bundle["tau"]
    lo, hi = C.BAND_LOW * tau, C.BAND_HIGH * tau
    in_band = s0[(s0 >= lo) & (s0 <= hi)].index
    ok(f"C day {n}: exactly the borderline accounts are re-scored", set(dec.index) == set(in_band) and res["band"] == len(in_band), f"{len(in_band)} of {len(cur)}")
    outside = s0.index.difference(in_band)
    ok(f"C day {n}: every other account keeps the same score and features", np.allclose(res["scores"].loc[outside], s0.loc[outside]) and same(res["X"].loc[outside], cur.loc[outside]))
    was, now = dec["score_before"] >= tau, dec["score_after"] >= tau
    expect = np.select([was & now, was & ~now, ~was & now], ["confirmed", "withdrawn", "promoted"], default="still_below")
    ok(f"C day {n}: the four outcomes follow the threshold exactly (confirmed / withdrawn / promoted / still below)", (dec["status"].to_numpy() == expect).all(), str(dec["status"].value_counts().to_dict()))
    ok(f"C day {n}: only a small share of accounts is pulled (cost discipline, < 3%)", res["band"] / len(cur) < 0.03, f"{res['band'] / len(cur):.2%}")
    ok(f"C day {n}: the tool calls are logged", {c["tool"] for c in res["calls"]} <= set(C.BOTH) and sum(c["accounts"] for c in res["calls"]) >= len(dec))
empty = C.confirm_borderline(eng, bundle, day(70), cur.iloc[:0], s0.iloc[:0])
ok("C an empty day (no accounts) is handled", empty["band"] == 0 and len(empty["decisions"]) == 0)

# ================================================================ D  runner integration
D = day(100)
r0 = R.compute_day(eng, bundle, D, pd.Series(dtype="datetime64[ns]"), features=store.get(100))
r1 = R.compute_day(eng, bundle, D, pd.Series(dtype="datetime64[ns]"), features=store.get(100), confirm=True)
tools = [s["tool"] for s in r1["steps"]]
ok("D confirm is optional: without it the runner behaves exactly as before (no confirm step)", "confirm_decide" not in [s["tool"] for s in r0["steps"]] and "confirmed_by_refresh" not in r0["flags"])
ok("D with confirm the step log shows select, the tool calls and the decision", "confirm_select" in tools and "confirm_decide" in tools and ("bureau_refresh" in tools or "aa_statement" in tools), str(tools))
dec = r1["confirm"]
untouched = r0["scores"].index.difference(dec.index)
ok("D confirm changes only the borderline accounts' scores", np.allclose(r0["scores"].loc[untouched, "score"], r1["scores"].loc[untouched, "score"]))
f1 = r1["flags"]
ok("D every promoted or confirmed flag is marked confirmed_by_refresh, withdrawn accounts are not flagged",
   set(f1.index[f1["confirmed_by_refresh"] == 1]) == set(f1.index) & set(dec.index[dec["status"].isin(["confirmed", "promoted"])])
   and not (set(f1.index) & set(dec.index[dec["status"] == "withdrawn"])))
ok("D the audit trail (score before the refresh, tools, outcome) is kept on the score row", r1["scores"].loc[dec.index, "signals_json"].str.contains('"refresh"').all() if len(dec) else True)

# database run on a small separate tenant, with the outcome tables blocked
TEN = "t_conf"
SCR = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\mini_conf")
shutil.rmtree(SCR, ignore_errors=True)
py = lambda *a: subprocess.run([sys.executable, *map(str, a)], cwd=BACKEND / "data", capture_output=True, text=True)
ok("D small test tenant generated and loaded", py("generate_bi03.py", "--n", 1500, "--seed", 21, "--id-start", 7_500_000, "--out", SCR).returncode == 0 and py("load_bi03.py", "--csv", SCR, "--tenant", TEN, "--reset").returncode == 0)
real_before = db.query_one(T, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id", database="bi03")["n"]
orig = db.get_conn
def guarded(database="main"):
    c = orig(database)
    c.set_authorizer(lambda action, a1, a2, dbn, src: sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and a1 in ("outcome_labels", "outcome_labels_hidden")) else sqlite3.SQLITE_OK)
    return c
db.get_conn = guarded
try:
    R.run_range(TEN, 61, 90, reset=True, use_cache=False, quiet=True, confirm=True); ran = True
except sqlite3.DatabaseError:
    ran = False
finally:
    db.get_conn = orig
ok("D a 30-day replay with confirm runs with the outcome tables blocked at database level", ran)
q = lambda sql, **p: db.query_df(TEN, sql, p, database="bi03")
names = set(q("SELECT DISTINCT tool FROM agent_steps WHERE tenant_id=:tenant_id").tool)
ok("D the tool calls are recorded in agent_steps (bureau_refresh / aa_statement / confirm_decide)", {"confirm_select", "confirm_decide"} <= names and bool({"bureau_refresh", "aa_statement"} & names), str(sorted(names)))
ok("D flags that the fresh data settled carry confirmed_by_refresh = 1", q("SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id AND confirmed_by_refresh = 1").n[0] > 0)
ok("D the audit trail reaches the database", q("SELECT COUNT(*) n FROM risk_scores WHERE tenant_id=:tenant_id AND signals_json LIKE '%\"refresh\"%'").n[0] > 0)
ok("D the real tenant's flags were not touched", db.query_one(T, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id", database="bi03")["n"] == real_before)

for tb in ("nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs", "events", "bureau_snapshots", "outcome_labels",
           "outcome_labels_hidden", "service_templates", "bp_meta", "bp_accounts"):
    db.execute(TEN, f"DELETE FROM {tb} WHERE tenant_id=:tenant_id", database="bi03")
shutil.rmtree(SCR, ignore_errors=True)

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
