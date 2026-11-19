"""
BI-03 risk model: training set, models, scoring, and "which signal is driving this account" (dominant signal).

Rules that keep the backtest honest (see backtest.py):
  - training uses PUBLIC labels only, on snapshot days whose whole 60-day outcome window lies inside the public period
  - this file never loads the hidden labels
  - model selection is done by cross-validation that splits by ACCOUNT, so the same account never sits in train and test
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import db  # noqa: E402
import features as F  # noqa: E402

DB = "bi03"
MODEL_DIR = HERE / "models"
CACHE_DIR = HERE.parents[1] / "data" / "bi03_cache"
HORIZON = 60                                   # "will miss an EMI within the next 60 days"
TRAIN_DAYS = list(range(3, 31, 3))             # day 30 + 60 = day 90 = last public day, so every label here is fully observed
FAMILIES: dict[str, list[str]] = {}
for _f, _fam in F.SIGNAL_OF.items():
    FAMILIES.setdefault(_fam, []).append(_f)
NON_DRIVER = {"static", "bureau_score"}        # never reported as "the" dominant signal (context / summary, nothing to act on)


# ------------------------------------------------------------------ dates and labels
def stream_start(tenant: str) -> pd.Timestamp:
    r = db.query_one(tenant, "SELECT value FROM bp_meta WHERE tenant_id = :tenant_id AND key = 'stream_start_date'", database=DB)
    return pd.Timestamp(r["value"])


def day_to_date(start: pd.Timestamp, day: int) -> pd.Timestamp:
    return start + pd.Timedelta(days=day - 1)


def miss_day_series(tenant: str, start: pd.Timestamp, table: str = "outcome_labels") -> pd.Series:
    """account_id -> stream day of its DPD 1 (NaN = never). Only 'outcome_labels' (public) is allowed here."""
    assert table == "outcome_labels", "the hidden labels are only read by the evaluator in backtest.py"
    df = db.query_df(tenant, f"SELECT account_id, dpd1_date FROM {table} WHERE tenant_id = :tenant_id", database=DB)
    d = pd.to_datetime(df["dpd1_date"])
    return pd.Series(((d - start).dt.days + 1).to_numpy(float), index=df["account_id"].values)


# ------------------------------------------------------------------ cached daily features
class FeatureStore:
    """features_at(day) computed once and cached on disk (the key includes a fingerprint of the data)."""

    def __init__(self, engine: F.FeatureEngine, tenant: str, start: pd.Timestamp):
        self.engine, self.tenant, self.start = engine, tenant, start
        self.fp = hashlib.md5(f"{len(engine.events)}-{engine.events['event_id'].max()}-{len(engine.snaps)}-{F.FEATURES}".encode()).hexdigest()[:10]
        self.mem: dict[int, pd.DataFrame] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, day: int) -> pd.DataFrame:
        if day not in self.mem:
            path = CACHE_DIR / f"{self.tenant}_{self.fp}_d{day}.pkl"
            if path.exists():
                self.mem[day] = pd.read_pickle(path)
            else:
                self.mem[day] = self.engine.features_at(day_to_date(self.start, day))
                self.mem[day].to_pickle(path)
        return self.mem[day]


# ------------------------------------------------------------------ training set
def build_training_set(store: FeatureStore, miss_day: pd.Series, days=TRAIN_DAYS, horizon=HORIZON) -> pd.DataFrame:
    """One row per (account, snapshot day): features as of that day + label 'missed within the next 60 days'."""
    parts = []
    for d in days:
        X = store.get(d)
        md = miss_day.reindex(X.index)
        current = md.isna() | (md > d)
        y = (md > d) & (md <= d + horizon)
        part = X[current.values].copy()
        part["y"], part["day"] = y[current.values].astype(int).values, d
        parts.append(part)
    return pd.concat(parts)


# ------------------------------------------------------------------ models
def make_pipeline(kind: str, params: dict):
    if kind == "logreg":
        return Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler()),
                         ("clf", LogisticRegression(C=params.get("C", 0.3), max_iter=3000))])
    if kind == "hgb":
        return HistGradientBoostingClassifier(max_depth=params.get("max_depth", 3), max_iter=params.get("max_iter", 200), learning_rate=0.05,
                                              min_samples_leaf=40, l2_regularization=1.0, random_state=0)
    raise ValueError(kind)


def fit(train: pd.DataFrame, kind: str, params: dict):
    model = make_pipeline(kind, params)
    model.fit(train[F.FEATURES], train["y"])
    return model


def score(model, X: pd.DataFrame) -> pd.Series:
    return pd.Series(model.predict_proba(X[F.FEATURES])[:, 1], index=X.index)


def explain(bundle: dict, X: pd.DataFrame) -> pd.DataFrame:
    """Risk plus the dominant signal. Works for any model: neutralise one signal family at a time (set it to the
    typical value) and see how much the risk drops. The family whose removal drops the risk most is the driver."""
    model, med = bundle["model"], bundle["medians"]
    base = score(model, X)
    drops = {}
    for fam, cols in FAMILIES.items():
        if fam == "static":                      # context features are never reported as the driver
            continue
        Xn = X.copy()
        Xn[cols] = med[cols].to_numpy()
        drops[fam] = base - score(model, Xn)
    D = pd.DataFrame(drops)
    # The bureau score is a SUMMARY of all distress, so it is a strong predictor but tells us nothing to act on. The dominant
    # signal (which decides the nudge) is therefore chosen among the signals we can respond to.
    actionable = [f for f in D.columns if f not in NON_DRIVER]
    out = pd.DataFrame({"risk": base})
    out["dominant_signal"] = np.where(D[actionable].max(axis=1) > 0, D[actionable].idxmax(axis=1), "none")
    return out.join(D.add_prefix("drop_"))


# ------------------------------------------------------------------ save / load
def save_bundle(bundle: dict, name: str) -> Path:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / f"{name}.joblib"
    joblib.dump(bundle, path)
    return path


def load_bundle(name: str) -> dict:
    return joblib.load(MODEL_DIR / f"{name}.joblib")


def bundle_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# ------------------------------------------------------------------ metrics shared by CV and the hidden evaluation
def top_fraction(scores: pd.Series, frac: float = 0.05, seed: int = 0) -> pd.Index:
    """Accounts in the top `frac` by score (ties broken at random, never by account order)."""
    k = max(1, int(round(frac * len(scores))))
    noise = np.random.default_rng(seed).random(len(scores))
    order = np.lexsort((noise, -scores.to_numpy()))
    return scores.index[order[:k]]


def snapshot_lift(scores: pd.Series, miss_day: pd.Series, day: int, last_day: int, horizon: int = HORIZON, frac: float = 0.05) -> dict:
    """Rank today's current accounts, take the top 5%, and compare with the base rate (miss within the window)."""
    md = miss_day.reindex(scores.index)
    y = ((md > day) & (md <= min(day + horizon, last_day))).astype(int)
    top = top_fraction(scores, frac)
    base, prec = y.mean(), y.loc[top].mean()
    return {"day": day, "n": int(len(scores)), "positives": int(y.sum()), "base_rate": float(base), "precision_top": float(prec),
            "lift": float(prec / base) if base > 0 else float("nan"), "caught": int(y.loc[top].sum())}


def calibrate_tau(score_by_day: dict[int, pd.Series], days: list[int], universe: pd.Index, share: float = 0.05) -> float:
    """Alert threshold with a BUDGET: over these days, about `share` of the accounts will ever cross it.
    Uses scores only (no labels), so it can be set on public days and then applied unchanged to the hidden period."""
    peak = pd.concat([score_by_day[d] for d in days]).groupby(level=0).max().reindex(universe)
    return float(peak.quantile(1 - share))


def replay(score_by_day: dict[int, pd.Series], miss_day: pd.Series, days: list[int], tau: float, universe: pd.Index, eval_from: int) -> dict:
    """Daily alerting with a fixed threshold, like the running system: an account is flagged the first day its score reaches tau.
    `days` are the days flags may be raised (they start BEFORE eval_from, a warm-up, because the real system does not reset
    when the evaluation period starts). Only misses on or after eval_from are counted. `universe` = accounts still current
    when the evaluation starts.
      precision = share of flagged accounts that DID miss an EMI after being flagged;  lift = precision / base rate
      lead time = days from the first flag to the DPD 1 (accounts flagged before they missed)."""
    first_flag: dict[str, int] = {}
    for d in days:
        s = score_by_day[d]
        for a in s.index[s.to_numpy() >= tau]:
            first_flag.setdefault(a, d)
    md = miss_day.reindex(universe)
    rec = pd.DataFrame(index=universe)                       # one row per account: when flagged, when it missed
    rec["flag_day"] = pd.Series(first_flag).reindex(universe)
    rec["miss_day"] = md.where((md >= eval_from) & (md <= days[-1]))
    rec["hit"] = rec["flag_day"].notna() & rec["miss_day"].notna() & (rec["flag_day"] < rec["miss_day"])
    rec["lead"] = (rec["miss_day"] - rec["flag_day"]).where(rec["hit"])
    return {**replay_stats(rec), "tau": tau, "records": rec}


def replay_stats(rec: pd.DataFrame) -> dict:
    flagged, missers, hits = int(rec["flag_day"].notna().sum()), int(rec["miss_day"].notna().sum()), int(rec["hit"].sum())
    base, precision = missers / len(rec), hits / max(flagged, 1)
    leads = rec["lead"].dropna()
    return {"flagged": flagged, "flagged_share": flagged / len(rec), "missers": missers, "base_rate": base, "caught": hits,
            "recall": hits / max(missers, 1), "precision": precision, "lift": precision / base if base > 0 else float("nan"),
            "median_lead_days": float(leads.median()) if len(leads) else float("nan"),
            "p25_lead_days": float(leads.quantile(0.25)) if len(leads) else float("nan")}
