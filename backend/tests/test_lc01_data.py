"""LC-01 data + governed metrics: the warehouse is intact, the planted stories are really in it, and EVERY governed metric
reconciles with an independent pandas implementation. Runs on a COPY of lc01.db. Zero LLM tokens.   python backend/tests/test_lc01_data.py"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp()) / "lc01.db"
shutil.copy(ROOT / "lc01.db", TMP)
os.environ["PORTFOLIOIQ_LC01_DB"] = str(TMP)
sys.path[:0] = [str(ROOT / "db"), str(ROOT / "agents" / "lc01")]
import lc01_access as A  # noqa: E402
import lc01_compiler as C  # noqa: E402
import lc01_oracle as O  # noqa: E402
import lc01_registry as R  # noqa: E402

bad = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


# ---- registry hygiene
ok("38 governed metrics", len(R.METRICS) == 38)
ok("every metric id is unique and its table is a known fact table", all(m.fact in R.FACTS for m in R.METRICS.values()))
ok("aliases are unique across metrics", len([a for m in R.METRICS.values() for a in m.aliases]) == len({a for m in R.METRICS.values() for a in m.aliases}))
ok("6 overlap groups, each with at least 2 definitions", len(R.OVERLAP_GROUPS) == 6 and all(len(v) >= 2 for v in R.OVERLAP_GROUPS.values()))
ok("an overlap group never needs more than 4 clarification options", max(len(v) for v in R.OVERLAP_GROUPS.values()) <= 4)
ok("every metric has a definition, a known unit and a 'good' direction", all(m.definition and m.unit in R.UNITS and m.good in ("up", "down", "neutral") for m in R.METRICS.values()))
ok("dimension whitelist is column-safe (only registry names reach SQL)", set(R.DIM_COL) >= {d for m in R.METRICS.values() for d in m.dims})
ok("every metric's SQL runs (syntax + columns) for every lender", all(len(C.run(A.login("u_csm"), C.make_query(A.login("u_csm"), mid)).rows) >= 1 for mid in R.METRICS))

# ---- reconciliation: SQL path vs independent pandas, every metric, 4 users x 2 windows
mism, n = [], 0
for uid, lend in (("u_l1", ["L1"]), ("u_l2", ["L2"]), ("u_l3", ["L3"]), ("u_csm", ["L1", "L2", "L3"])):
    p = A.login(uid)
    for mid in R.METRICS:
        for a, b in (("2026-10", "2026-10"), ("2026-07", "2026-09")):
            got = C.run(p, C.make_query(p, mid, m_from=a, m_to=b)).rows[0]["value"]
            want = O.value(mid, lend, a, b)
            n += 1
            if (got is None) != (want is None) or (got is not None and abs(got - want) > 1e-9 * max(1, abs(want))):
                mism.append((uid, mid, a, b, got, want))
ok(f"every governed metric reconciles with the independent calculation ({n} comparisons)", not mism)
if mism:
    print(mism[:5])

# grouped + monthly figures reconcile too
p = A.login("u_l1")
g = C.run(p, C.make_query(p, "connect_rate", ["agency"], m_from="2026-10", m_to="2026-10")).rows
w = O.value("connect_rate", ["L1"], "2026-10", "2026-10", group_by=["agency"])
ok("grouped result matches the oracle for every segment", all(abs(r["value"] - w[(r["agency"],)]) < 1e-9 for r in g) and len(g) == len(w))
t = C.run(p, C.make_query(p, "par90", trend=True, m_from="2026-01", m_to="2026-10")).rows
s = O.series("par90", ["L1"], "2026-01", "2026-10")
ok("monthly series matches the oracle for all 10 months", len(t) == 10 and all(abs(r["value"] - s[r["month"]]) < 1e-9 for r in t))
pc = A.login("u_csm")
tot = C.run(pc, C.make_query(pc, "total_collection_efficiency")).rows[0]
parts = [C.run(A.login(u), C.make_query(A.login(u), "total_collection_efficiency")).rows[0] for u in ("u_l1", "u_l2", "u_l3")]
ok("a combined total is sum(numerators)/sum(denominators), NOT an average of the three lenders' ratios",
   abs(tot["value"] - sum(x["num"] for x in parts) / sum(x["den"] for x in parts)) < 1e-12 and abs(tot["value"] - sum(x["value"] for x in parts) / 3) > 1e-6)

# ---- the planted stories are really in the data (they are the root-cause answer keys)
def v(mid, lend, a, b=None, **f):
    return O.value(mid, lend, a, b or a, f or None)


ok("S1 Bluewave: connect rate collapses from August", v("connect_rate", ["L1"], "2026-08", agency="Bluewave Collections") < 0.30 < v("connect_rate", ["L1"], "2026-06", agency="Bluewave Collections"))
ok("S2 L2 East two-wheelers: NACH bounce spikes in September", v("nach_bounce_rate", ["L2"], "2026-09", product="two_wheeler", region="East") > 0.30)
ok("S2 ... and new delinquency there is at least 2x the rest of L2", v("new_delinquency_rate", ["L2"], "2026-09", product="two_wheeler", region="East") > 2 * v("new_delinquency_rate", ["L2"], "2026-09", region="North"))
ok("S3 Chennai delinquency rises at least 5 points Jan to Oct", v("delinquency_rate", ["L3"], "2026-10", city="Chennai") - v("delinquency_rate", ["L3"], "2026-01", city="Chennai") > 0.05)
ok("S4 Fortis keeps far fewer promises than every other L2 agency", v("ptp_kept_rate", ["L2"], "2026-01", "2026-10", agency="Fortis Collections") + 0.15 < min(
    v("ptp_kept_rate", ["L2"], "2026-01", "2026-10", agency=a) for a in ("Eastgate Recovery", "Granite Services", "Harbor Recovery", "Ionic Field Force")))
ok("S6 L3 gold-loan disputes in July are at least 5x June", v("disputes_raised", ["L3"], "2026-07", product="gold_loan") >= 5 * max(1, v("disputes_raised", ["L3"], "2026-06", product="gold_loan")))
ok("L2's 5-day grace is visible: its new-delinquency rate is not above the no-grace lenders'", v("new_delinquency_rate", ["L2"], "2026-01", "2026-08") <= v("new_delinquency_rate", ["L1"], "2026-01", "2026-08") + 0.01)

print(f"\n{'ALL PASS' if not bad else str(len(bad)) + ' FAILED'}")
sys.exit(1 if bad else 0)
