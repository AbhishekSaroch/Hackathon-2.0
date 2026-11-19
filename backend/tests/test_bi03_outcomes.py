"""
BI-03 outcomes + threshold tuning tests.   python test_bi03_outcomes.py   (exit code 1 on any failure)

  A derivation   outcomes from repayment events match the public labels exactly, and are point-in-time
  B rules        a flag's outcome: hit / no miss / not known yet / missed too late, including the exact boundaries
  C recording    one outcome per flag on a small separate tenant, idempotent, hidden period guarded, no label tables read
  D tuning       candidate thresholds behave sensibly, the active threshold is never changed by tuning, and the daily runner obeys the active one
"""
import json
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
import outcomes as O  # noqa: E402
import runner as R  # noqa: E402

T = "abc_bank"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


# ================================================================ A  derivation vs the PUBLIC labels
info = O.stream_info(T)
rep = O.load_repayments(T)
pub = db.query_df(T, "SELECT account_id, dpd1_date FROM outcome_labels WHERE tenant_id=:tenant_id", database="bi03")
pub = pd.Series(pd.to_datetime(pub.dpd1_date).values, index=pub.account_id.values)
for day in (60, 75, 90):
    E = O.to_date(info, day)
    miss = O.first_miss_dates(rep, E)
    known = pub[pub <= E - pd.Timedelta(days=O.GRACE_DAYS)]
    ok(f"A day {day}: the DPD 1 date derived from repayment events equals the public label for every account that missed (past the grace period)",
       (miss.reindex(known.index) == known).all() and len(known) > 20, f"{len(known)} accounts")
    ok(f"A day {day}: nobody who never missed is reported as missed", set(miss.index) <= set(pub.dropna().index))
    cut = rep[rep["available_from"] <= E]
    poisoned = rep.copy()
    fut = poisoned["available_from"] > E
    poisoned.loc[fut, "event_type"] = poisoned.loc[fut, "event_type"].map({"emi_paid": "emi_due", "emi_due": "emi_paid"})
    ok(f"A day {day}: point-in-time: same answer with the future deleted or wrecked", miss.equals(O.first_miss_dates(cut, E)) and miss.equals(O.first_miss_dates(poisoned, E)))

# ================================================================ B  the outcome rules
f0 = pd.Timestamp("2026-06-01")
flags = pd.DataFrame({"flag_id": range(1, 9), "account_id": list("ABCDEFGH"), "flagged_on": [f0.strftime("%Y-%m-%d")] * 8})
miss = pd.Series({"A": f0 + pd.Timedelta(days=20),     # missed 20 days after the flag
                  "B": f0 + pd.Timedelta(days=60),     # exactly 60: still a hit
                  "C": f0 + pd.Timedelta(days=61),     # 61: too late for this flag
                  "D": f0 - pd.Timedelta(days=2),      # was already past due when flagged
                  "F": f0 + pd.Timedelta(days=100)})   # missed long after
late = f0 + pd.Timedelta(days=200)
early = f0 + pd.Timedelta(days=30)
res_late, res_early = O.derive_outcomes(flags, miss, late), O.derive_outcomes(flags, miss, early)
g = lambda df, a: df.set_index("account_id").loc[a]
ok("B a miss within 60 days of the flag is a hit, with its lead time", g(res_late, "A").went_dpd1 == 1 and g(res_late, "A").lead_time_days == 20)
ok("B a miss exactly 60 days after the flag is still a hit; 61 days is not", g(res_late, "B").went_dpd1 == 1 and g(res_late, "C").went_dpd1 == 0)
ok("B an account that was already past due when flagged is not a hit", g(res_late, "D").went_dpd1 == 0 and pd.isna(g(res_late, "D").lead_time_days))
ok("B no miss after the 60 days (+ grace) are over -> 'no' (0)", g(res_late, "E").went_dpd1 == 0)
ok("B no miss yet while the 60 days are still running -> NOT KNOWN (NULL), never a 'no'", pd.isna(g(res_early, "E").went_dpd1) and pd.isna(g(res_early, "G").went_dpd1))
ok("B a miss that has already happened is final even before the window ends", g(res_early, "A").went_dpd1 == 1)
ok("B the end of the window is exactly 60 + grace + 1 days after the flag", pd.isna(g(O.derive_outcomes(flags, miss, f0 + pd.Timedelta(days=64)), "E").went_dpd1)
   and g(O.derive_outcomes(flags, miss, f0 + pd.Timedelta(days=65)), "E").went_dpd1 == 0)

# ================================================================ small tenant for C and D
TEN = "t_out"
SCR = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\mini_out")
shutil.rmtree(SCR, ignore_errors=True)
py = lambda *a: subprocess.run([sys.executable, *map(str, a)], cwd=BACKEND / "data", capture_output=True, text=True)
ok("C small test tenant generated and loaded", py("generate_bi03.py", "--n", 1500, "--seed", 31, "--id-start", 7_800_000, "--out", SCR).returncode == 0
   and py("load_bi03.py", "--csv", SCR, "--tenant", TEN, "--reset").returncode == 0)
real_before = {t: db.query_one(T, f"SELECT COUNT(*) n FROM {t} WHERE tenant_id=:tenant_id", database="bi03")["n"] for t in ("flags", "flag_outcomes", "thresholds")}
O.LEDGER = Path(r"C:\Users\abhis\AppData\Local\Temp\claude\7df24b89c951eb30\8a10a620-20e1-4437-9fa4-e80af5adf670\scratchpad\test_ledger.json")
O.LEDGER.unlink(missing_ok=True)
q = lambda sql, **p: db.query_df(TEN, sql, p, database="bi03")

R.run_range(TEN, 61, 90, reset=True, use_cache=False, quiet=True)
orig = db.get_conn
def guarded(database="main"):
    c = orig(database)
    c.set_authorizer(lambda action, a1, a2, dbn, src: sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and a1 in ("outcome_labels", "outcome_labels_hidden")) else sqlite3.SQLITE_OK)
    return c
db.get_conn = guarded
try:
    summary = O.record_outcomes(TEN)
    tuned = O.tune_threshold(TEN)
    blocked_ok = True
except sqlite3.DatabaseError:
    blocked_ok = False
finally:
    db.get_conn = orig
ok("C recording outcomes and tuning work with the outcome (label) tables blocked at database level", blocked_ok, str(summary))

nfl = q("SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id").n[0]
fo = q("SELECT * FROM flag_outcomes WHERE tenant_id=:tenant_id")
ok("C every flag has exactly one outcome row", len(fo) == nfl == summary["flags"] and fo.flag_id.is_unique, f"{nfl} flags")
ok("C outcomes are 1, 0 or NULL (not known yet) and the numbers add up", set(fo.went_dpd1.dropna().unique()) <= {0.0, 1.0} and summary["hit"] + summary["no_miss"] + summary["pending"] == nfl,
   f"hit {summary['hit']}, no miss {summary['no_miss']}, pending {summary['pending']}")
hits = fo[fo.went_dpd1 == 1]
ok("C every hit has a DPD 1 date after its flag and a lead time of 1-60 days", len(hits) > 0 and hits.lead_time_days.between(1, 60).all() and hits.dpd1_date.notna().all(), f"{len(hits)} hits")
lab = q("SELECT account_id, dpd1_date FROM outcome_labels WHERE tenant_id=:tenant_id")                     # cross-check with the PUBLIC labels
lab = pd.Series(pd.to_datetime(lab.dpd1_date).values, index=lab.account_id.values)
ok("C every hit's DPD 1 date equals the account's public label", (pd.to_datetime(hits.set_index("account_id").dpd1_date).values == lab.reindex(hits.account_id).values).all())
ok("C flags raised in the last days are 'not known yet' (pending), never counted as a miss that did not happen", summary["pending"] > 0, f"{summary['pending']} pending")

n_first = len(q("SELECT 1 FROM flag_outcomes WHERE tenant_id=:tenant_id"))
O.record_outcomes(TEN)
ok("C recording again replaces the rows (idempotent, no duplicates)", len(q("SELECT 1 FROM flag_outcomes WHERE tenant_id=:tenant_id")) == n_first)
try:
    O.record_outcomes(TEN, as_of_day=120); guard = False
except O.HiddenPeriodError:
    guard = True
ok("C recording past the last public day is refused unless --include-hidden is given", guard)
ok("C ... and with --include-hidden it goes ahead and is written to the ledger", O.record_outcomes(TEN, as_of_day=120, include_hidden=True)["as_of_day"] == 120 and len(json.loads(O.LEDGER.read_text())) == 1)
try:
    O.tune_threshold(TEN, cutoff_day=130); guard2 = False
except O.HiddenPeriodError:
    guard2 = True
ok("C tuning past the last public day is refused the same way", guard2)
O.record_outcomes(TEN)                                                                                       # back to the public cutoff

# ================================================================ D  tuning
tb = tuned["table"]
cand = tb[tb.candidate != "ACTIVE now"].sort_values("flagged_share")
ok("D the table has the 5 candidate flag budgets plus the active threshold", len(cand) == 5 and "ACTIVE now" in set(tb.candidate))
ok("D a looser threshold (bigger flag budget) flags more accounts and has a lower threshold value", cand.flagged.is_monotonic_increasing and cand.tau.is_monotonic_decreasing)
ok("D a looser threshold never catches fewer misses (recall rises with the budget)", cand.recall.is_monotonic_increasing)
ok("D the flagged share of each candidate is close to its budget", ((cand.flagged_share - np.array(O.BUDGETS)).abs() < 0.02).all(), str(cand.flagged_share.round(3).tolist()))
rec = tuned["recommended"]
both = cand[(cand.precision >= O.MIN_PRECISION) & (cand.median_lead_days >= O.MIN_LEAD)]
prec_only = cand[cand.precision >= O.MIN_PRECISION]
pool = both if len(both) else (prec_only if len(prec_only) else cand)
rule_ok = rec["candidate"] in set(pool.candidate)
if len(pool) and pool is not cand:
    rule_ok &= rec["recall"] == pool.recall.max()                         # highest recall among those that qualify
ok("D the recommendation follows the rule: highest recall with precision >= 30% and lead >= 21 days; else precision alone; else best lift", rule_ok, tuned["reason"])
ok("D a fallback is never presented as a clean pass (the reason text names it)", (len(both) > 0) == tuned["reason"].startswith("precision and lead"))
cands = q("SELECT threshold_version, is_active FROM thresholds WHERE tenant_id=:tenant_id")
ok("D the recommendation is saved as an INACTIVE candidate; the active threshold is untouched",
   cands[cands.threshold_version == tuned["version"]].is_active.iloc[0] == 0 and cands.is_active.sum() == 1 and cands[cands.is_active == 1].threshold_version.iloc[0] == R.THRESHOLD_VERSION)

new_tau = float(rec["tau"])
old = q("SELECT value v FROM thresholds WHERE tenant_id=:tenant_id AND is_active=1").v[0]
ok("D (setup) the candidate differs from the active threshold, so the next check means something", abs(new_tau - old) > 1e-6, f"active {old:.4f}, candidate {new_tau:.4f}")
act = O.activate_threshold(TEN, tuned["version"])
cands = q("SELECT threshold_version, is_active FROM thresholds WHERE tenant_id=:tenant_id")
ok("D activating a version makes exactly that one active", cands.is_active.sum() == 1 and cands[cands.is_active == 1].threshold_version.iloc[0] == tuned["version"] and act["tau"] == new_tau)
try:
    O.activate_threshold(TEN, "no-such-version"); unknown = False
except ValueError:
    unknown = True
ok("D an unknown version cannot be activated", unknown)

before_flags = set(q("SELECT flag_id FROM flags WHERE tenant_id=:tenant_id").flag_id)
R.run_range(TEN, 91, 95, reset=False, use_cache=False, quiet=True)                                          # 5 more days under the new threshold
newf = q("SELECT threshold_version tv, score FROM flags WHERE tenant_id=:tenant_id AND flag_id NOT IN (%s)" % ",".join(map(str, before_flags)))
ok("D the daily runner obeys the ACTIVE threshold: new flags carry its version and are at or above its value", len(newf) > 0 and (newf.tv == tuned["version"]).all() and (newf.score >= new_tau - 1e-9).all(), f"{len(newf)} new flags")

ok("the real tenant's flags, outcomes and thresholds were not touched", {t: db.query_one(T, f"SELECT COUNT(*) n FROM {t} WHERE tenant_id=:tenant_id", database="bi03")["n"] for t in ("flags", "flag_outcomes", "thresholds")} == real_before)
for tb_ in ("nudges", "flag_outcomes", "flags", "risk_scores", "thresholds", "agent_steps", "agent_runs", "events", "bureau_snapshots", "outcome_labels",
            "outcome_labels_hidden", "service_templates", "bp_meta", "bp_accounts"):
    db.execute(TEN, f"DELETE FROM {tb_} WHERE tenant_id=:tenant_id", database="bi03")
shutil.rmtree(SCR, ignore_errors=True)
O.LEDGER.unlink(missing_ok=True)

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
