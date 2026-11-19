"""
Do the leakage tests actually catch leaks?   python test_bi03_mutations.py

We deliberately break features.py in 5 ways a real bug could (each one leaks future data) and check that the
truncation / poison / boundary tests FAIL for every one. If a leak slips through, this script exits with code 1.
"""
import sys
import types
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import features as F  # noqa: E402

SRC = (BACKEND / "agents" / "bi03" / "features.py").read_text(encoding="utf-8")
GATE_EV = 'ev = events[events["available_from"] <= t]'
GATE_SN = 'sn = snaps[snaps["available_from"] <= t]'
MUTATIONS = {
    "gate is off by one day (<= t + 1 day)": (GATE_EV, 'ev = events[events["available_from"] <= t + pd.Timedelta(days=1)]'),
    "gate uses event_date instead of available_from": (GATE_EV, 'ev = events[events["event_date"] <= t]'),
    "bureau snapshots are not gated at all": (GATE_SN, "sn = snaps"),
    "bounce features read the ungated events": ('bounce = ev[ev["event_type"] == "other_loan_bounce"]', 'bounce = events[events["event_type"] == "other_loan_bounce"]'),
    "salary features read the ungated events": ("sal = ev[ev[\"is_salary\"]]", "sal = events[events[\"is_salary\"]]"),
}
DATES = ["2026-06-15", "2026-07-29", "2026-08-20"]


def build(old: str, new: str):
    assert old in SRC, f"mutation target not found: {old}"
    mod = types.ModuleType("mutant")
    mod.__dict__["__file__"] = str(BACKEND / "agents" / "bi03" / "features.py")   # features.py resolves paths from __file__
    exec(compile(SRC.replace(old, new), "mutant", "exec"), mod.__dict__)
    return mod.compute_features


def leaks(compute, ev, sn, ac) -> list[str]:
    """Return the names of the tests that notice a leak in this compute function."""
    caught = []
    for d in DATES:
        D = pd.Timestamp(d)
        full = compute(ev, sn, ac, D)
        cut = compute(ev[ev["available_from"] <= D], sn[sn["available_from"] <= D], ac, D)
        try:
            assert_frame_equal(full, cut, rtol=1e-9, atol=1e-9)
        except AssertionError:
            caught.append(f"truncation@{d}")
    D = pd.Timestamp(DATES[1])                                              # poison: wreck all future rows
    pe, ps = ev.copy(), sn.copy()
    fut, fs = pe["available_from"] > D, ps["available_from"] > D
    pe.loc[fut, "amount"], pe.loc[fut, "value"], pe.loc[fut, "balance"] = 1e9, 99.0, 1.0
    pe.loc[fut, "event_date"] = pe.loc[fut, "event_date"] - pd.Timedelta(days=5)
    ps.loc[fs, "card_utilisation"], ps.loc[fs, "bureau_score"] = 0.99, 300
    try:
        assert_frame_equal(compute(ev, sn, ac, D), compute(pe, ps, ac, D), rtol=1e-9, atol=1e-9)
    except AssertionError:
        caught.append("poison")
    acct = ac["account_id"].iloc[0]                                         # boundary: available on D+1 must stay invisible on D
    row = pd.DataFrame([(int(ev["event_id"].max()) + 1, acct, D, D + pd.Timedelta(days=1), "bureau", "other_loan_bounce", 1000.0, np.nan, np.nan, "T", False)], columns=ev.columns)
    if compute(pd.concat([ev, row], ignore_index=True), sn, ac, D).loc[acct, "bounce_30"] != compute(ev, sn, ac, D).loc[acct, "bounce_30"]:
        caught.append("boundary")
    return caught


eng = F.FeatureEngine.from_db("abc_bank")
ev, sn, ac = eng.events, eng.snaps, eng.accounts
missed = []

clean = leaks(F.compute_features, ev, sn, ac)
print(f"{'PASS' if not clean else 'FAIL'}  the real engine is clean: tests that fire = {clean or 'none'}")
if clean:
    missed.append("real engine reported as leaky")

for name, (old, new) in MUTATIONS.items():
    caught = leaks(build(old, new), ev, sn, ac)
    print(f"{'PASS' if caught else 'FAIL'}  leak injected: {name:52s} -> caught by {caught or 'NOTHING (leak slipped through!)'}")
    if not caught:
        missed.append(name)

print("\n" + ("All injected leaks were caught." if not missed else f"{len(missed)} problem(s): {missed}"))
sys.exit(1 if missed else 0)
