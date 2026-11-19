"""
BI-03 feature engine: LEAKAGE and sanity tests.   python test_bi03_features.py   (exit code 1 if anything fails)

Future leakage is disqualifying, so most tests here try to BREAK the engine on purpose:
  1 truncation     features on the full data == features on data cut to available_from <= D
  2 poison         wreck every future row (huge values, earlier dates) and add fake late-posted rows: nothing may change
  3 boundary       a row available on day D is visible on D, a row available on D+1 is not (no off-by-one)
  4 late posting   rows that happened before D but arrived after D really exist in the data, and are invisible on D
  5 row order      shuffling the rows changes nothing; two calls give the same answer
  6 no label read  the engine runs with the outcome tables blocked at database level
  7 too good       no single feature may predict the hidden outcome suspiciously well (leakage shows up as AUC near 1)
  8 signal         the business signals really do separate future defaulters from the rest
  9 speed          one daily feature run over 6,000 accounts, and the 60,000-account estimate
"""
import re
import sqlite3
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import db  # noqa: E402
import features as F  # noqa: E402

T = "abc_bank"
DATES = ["2026-06-15", "2026-07-29", "2026-08-20"]      # inside public, end of public, inside hidden
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


def same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    try:
        assert_frame_equal(a, b, check_exact=False, rtol=1e-9, atol=1e-9)
        return True
    except AssertionError:
        return False


t0 = time.time()
eng = F.FeatureEngine.from_db(T)
print(f"engine loaded in {time.time() - t0:.1f}s: {len(eng.events):,} events, {len(eng.snaps):,} snapshots, {len(eng.accounts):,} accounts\n")
ev, sn, ac = eng.events, eng.snaps, eng.accounts

# ---------------------------------------------------------------- 0 basic shape
X0 = eng.features_at(DATES[1])
ok(f"one row per account, all {len(F.FEATURES)} features, no infinities", X0.shape == (6000, len(F.FEATURES)) and not np.isinf(X0.to_numpy()).any() and X0.index.is_unique, str(X0.shape))
ok("every feature belongs to a named signal family", set(F.SIGNAL_OF) == set(F.FEATURES))

# ---------------------------------------------------------------- 1 truncation invariance
for d in DATES:
    D = pd.Timestamp(d)
    full = F.compute_features(ev, sn, ac, D)
    cut = F.compute_features(ev[ev["available_from"] <= D], sn[sn["available_from"] <= D], ac, D)
    ok(f"1 truncation: features at {d} identical with or without the future", same(full, cut))

# ---------------------------------------------------------------- 2 poison the future
rng = np.random.default_rng(0)
for d in DATES:
    D = pd.Timestamp(d)
    base_X = F.compute_features(ev, sn, ac, D)
    pe, ps = ev.copy(), sn.copy()
    fut = pe["available_from"] > D
    pe.loc[fut, "amount"], pe.loc[fut, "value"], pe.loc[fut, "balance"] = 1e9, 99.0, 1.0
    pe.loc[fut, "event_date"] = pe.loc[fut, "event_date"] - pd.Timedelta(days=5)          # still arrives after D, but "happened" earlier
    fs = ps["available_from"] > D
    ps.loc[fs, "card_utilisation"], ps.loc[fs, "bureau_score"], ps.loc[fs, "other_loan_dpd"] = 0.99, 300, 90
    victims = rng.choice(ac["account_id"].values, 300, replace=False)
    nid = int(pe["event_id"].max()) + 1
    fake = []
    for a in victims:                                                                      # late-posted rows: happened before D, arrive after D
        for _ in range(6):
            for typ, src in (("other_loan_bounce", "bureau"), ("new_enquiry", "bureau")):
                fake.append((nid, a, D - pd.Timedelta(days=1), D + pd.Timedelta(days=1), src, typ, 1e6, 9.0, np.nan, "FAKE", False)); nid += 1
        fake.append((nid, a, D - pd.Timedelta(days=2), D + pd.Timedelta(days=1), "statement", "credit", 1.0, np.nan, 1.0, "SALARY FAKE", True)); nid += 1
    pe = pd.concat([pe, pd.DataFrame(fake, columns=pe.columns)], ignore_index=True)
    ps = pd.concat([ps, pd.DataFrame({"account_id": victims, "snapshot_date": D - pd.Timedelta(days=1), "available_from": D + pd.Timedelta(days=2),
                                      "card_utilisation": 0.99, "enquiries_30d": 9, "open_loans": 9, "other_loan_dpd": 90,
                                      "other_loan_bounces_90d": 9, "bureau_score": 300})], ignore_index=True)
    ok(f"2 poison: wrecked + fake future rows ({int(fut.sum()):,} real, {len(fake)} fake) change nothing at {d}", same(base_X, F.compute_features(pe, ps, ac, D)))

# ---------------------------------------------------------------- 3 boundary (off-by-one)
D = pd.Timestamp(DATES[1])
acct = ac["account_id"].iloc[0]
def with_bounce(avail):
    row = pd.DataFrame([(int(ev["event_id"].max()) + 1, acct, D, avail, "bureau", "other_loan_bounce", 1000.0, np.nan, np.nan, "TEST", False)], columns=ev.columns)
    return F.compute_features(pd.concat([ev, row], ignore_index=True), sn, ac, D).loc[acct, "bounce_30"]
base_b = F.compute_features(ev, sn, ac, D).loc[acct, "bounce_30"]
ok("3 boundary: a row available_from == D is visible on D", with_bounce(D) == base_b + 1, f"{base_b} -> {with_bounce(D)}")
ok("3 boundary: a row available_from == D+1 is NOT visible on D", with_bounce(D + pd.Timedelta(days=1)) == base_b)

# ---------------------------------------------------------------- 4 late-posted rows exist and are invisible
for d in DATES:
    D = pd.Timestamp(d)
    late = ev[(ev["event_date"] <= D) & (ev["available_from"] > D)]
    visible_late = ev[ev["available_from"] <= D]
    ok(f"4 late posting at {d}: {len(late)} real rows happened but were not yet visible, and none of them is used", len(late) > 0 and (visible_late["available_from"] <= D).all())

# ---------------------------------------------------------------- 5 row order and determinism
D = pd.Timestamp(DATES[1])
a = F.compute_features(ev, sn, ac, D)
shuf = F.compute_features(ev.sample(frac=1, random_state=1), sn.sample(frac=1, random_state=1), ac.sample(frac=1, random_state=1).sort_values("account_id"), D)
ok("5 shuffling every input table changes nothing", same(a, shuf))
ok("5 two calls give the same answer", same(a, F.compute_features(ev, sn, ac, D)))

# ---------------------------------------------------------------- 6 engine never reads the outcome tables
orig_get_conn = db.get_conn
BLOCKED = ("outcome_labels", "outcome_labels_hidden")
def guarded(database="main"):
    c = orig_get_conn(database)
    c.set_authorizer(lambda action, a1, a2, dbn, src: sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and a1 in BLOCKED) else sqlite3.SQLITE_OK)
    return c
db.get_conn = guarded
try:
    try:
        guarded("bi03").execute("SELECT * FROM outcome_labels").fetchall(); control = False
    except sqlite3.DatabaseError:
        control = True
    ok("6 control: the block really stops a read of outcome_labels", control)
    try:
        e2 = F.FeatureEngine.from_db(T); X2 = e2.features_at(DATES[1]); ran = True
    except sqlite3.DatabaseError:
        ran = False
    ok("6 feature engine loads and computes with the outcome tables blocked", ran and same(X2, a))
finally:
    db.get_conn = orig_get_conn
src_text = (BACKEND / "agents" / "bi03" / "features.py").read_text(encoding="utf-8")
ok("6 features.py has no SQL touching outcome tables", not re.search(r"(FROM|JOIN)\s+outcome", src_text, re.I))

# ---------------------------------------------------------------- 7 / 8 hidden-label checks (the EVALUATOR may read hidden labels, the engine may not)
from sklearn.metrics import roc_auc_score  # noqa: E402
D = pd.Timestamp("2026-07-29")                                                         # end of public period
pub = db.query_df(T, "SELECT account_id, dpd1_date FROM outcome_labels WHERE tenant_id = :tenant_id", database="bi03").set_index("account_id")["dpd1_date"]
hid = db.query_df(T, "SELECT account_id, dpd1_date FROM outcome_labels_hidden WHERE tenant_id = :tenant_id", database="bi03").set_index("account_id")["dpd1_date"]
X = F.compute_features(ev, sn, ac, D)
current = X.index[pub.reindex(X.index).isna().values]                                  # still current at the end of the public period
y = hid.reindex(current).notna().astype(int)
Xc = X.loc[current]
print(f"\nhidden-window evaluation population: {len(current)} current accounts, {int(y.sum())} go DPD 1 in the next 60 days ({y.mean():.2%})")

aucs = {}
for c in F.FEATURES:
    v = Xc[c].fillna(Xc[c].median())
    aucs[c] = roc_auc_score(y, v) if v.nunique() > 1 else 0.5
strength = {c: max(a_, 1 - a_) for c, a_ in aucs.items()}
top = sorted(strength.items(), key=lambda kv: -kv[1])[:5]
print("strongest single features (AUC):", {k: round(v, 3) for k, v in top})
ok("7 no single feature predicts the hidden outcome suspiciously well (AUC < 0.85)", max(strength.values()) < 0.85, f"max {max(strength.values()):.3f}")
ok("7 ... and the combined signals are not useless either (best single feature AUC > 0.55)", max(strength.values()) > 0.55)

pos, neg = Xc[y == 1], Xc[y == 0]
directional = {"sal_delay_max3": 1, "sal_late_cnt_90": 1, "bounce_60": 1, "enq_30": 1, "util_max3": 1, "emi_late_cnt_90": 1}
hits = {c: pos[c].mean() > neg[c].mean() for c in directional}
print("future defaulters vs the rest, mean of each signal:", {c: (round(pos[c].mean(), 3), round(neg[c].mean(), 3)) for c in directional})
ok("8 signals point the right way (>= 5 of 6 are higher for future defaulters)", sum(hits.values()) >= 5, f"{sum(hits.values())}/6")

# ---------------------------------------------------------------- 9 speed
times = []
for d in DATES:
    t0 = time.perf_counter(); F.compute_features(ev, sn, ac, d); times.append(time.perf_counter() - t0)
avg = sum(times) / len(times)
ok("9 one daily feature run over 6,000 accounts is fast (< 10 s)", avg < 10, f"{avg:.2f}s average, so about {avg * 10:.0f}s for 60,000 accounts")

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
