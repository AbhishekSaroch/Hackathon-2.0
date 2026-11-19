"""
BI-03 model discipline tests.   python test_bi03_model.py   (exit code 1 on any failure)

These lock in the rules that make the backtest honest, so a later edit cannot silently break them.
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import features as F  # noqa: E402
import model as M  # noqa: E402

T = "abc_bank"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


src_model = (BACKEND / "agents" / "bi03" / "model.py").read_text(encoding="utf-8")
src_bt = (BACKEND / "agents" / "bi03" / "backtest.py").read_text(encoding="utf-8")
src_feat = (BACKEND / "agents" / "bi03" / "features.py").read_text(encoding="utf-8")

# ---- the hidden labels are only ever read by the evaluator ----
ok("model.py never touches the hidden-label table", "outcome_labels_hidden" not in src_model)
ok("features.py never touches the hidden-label table", "outcome_labels_hidden" not in src_feat)
ok("backtest.py reads the hidden labels only inside cmd_hidden()", "outcome_labels_hidden" not in src_bt.split("def cmd_hidden")[0])
try:
    M.miss_day_series(T, pd.Timestamp("2026-05-01"), table="outcome_labels_hidden"); blocked = False
except AssertionError:
    blocked = True
ok("the label loader in model.py refuses the hidden table", blocked)

# ---- the saved model ----
b = M.load_bundle("bi03-v1")
ok("trained only on days whose whole 60-day outcome window is public (last train day + 60 <= 90)", max(b["train_days"]) + b["horizon"] <= 90,
   f"days {min(b['train_days'])}-{max(b['train_days'])}, horizon {b['horizon']}")
ok("model uses exactly the feature engine's features", b["features"] == F.FEATURES)

start = M.stream_start(T)
eng = F.FeatureEngine.from_db(T)
store = M.FeatureStore(eng, T, start)
X = store.get(90)
s = M.score(b["model"], X)
ok("every account gets a probability between 0 and 1", len(s) == 6000 and s.between(0, 1).all())
ok("scoring twice gives the same numbers", np.allclose(s, M.score(b["model"], X)))

# ---- the alert threshold uses scores only and gives a ~5% flag budget on public days ----
pub = M.miss_day_series(T, start)
accounts = X.index
uni = accounts[(pub.reindex(accounts).isna() | (pub.reindex(accounts) > 30)).to_numpy()]
peak = pd.concat([M.score(b["model"], store.get(d)).reindex(uni) for d in range(1, 91)], axis=1).max(axis=1)
share = float((peak >= b["tau"]).mean())
ok("alert threshold tau flags about 5% of accounts on the public period", 0.04 <= share <= 0.06, f"{share:.1%}")

# ---- dominant signal ----
top = s.nlargest(150).index
ex = M.explain(b, X.loc[top])
ok("every flagged account gets a dominant signal", ex["dominant_signal"].notna().all())
ok("context / summary signals (static, bureau_score) are never named as the driver", not ({"static", "bureau_score"} & set(ex["dominant_signal"])),
   str(ex["dominant_signal"].value_counts().to_dict()))
ok("the dominant signals are varied, so the nudge can actually differ by account (at least 3 different ones)", ex["dominant_signal"].nunique() >= 3)
ok("the named signals are real families", set(ex["dominant_signal"]) <= set(F.SIGNAL_OF.values()) | {"none"})

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
