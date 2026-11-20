"""
LC-04 CubeFrame: the cube in memory, for the drill-down's wide scans (dozens of segments x 15 history windows).
Same governed definitions as compute.py (it parses the SAME registry formulas), same numbers: tests/test_lc04_drill.py
asserts frame.sums(...) == compute(...) on many views. The drill-down shows only numbers it re-derives through here and the
test pins them to compute(). It reads every lender (a system actor: the detector and the drill-down run for the platform;
what a USER may see is decided later by access.py).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
from registry import DIM_COL, MetricError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

SLOTS = 96
STREAM_START = pd.Timestamp("2026-09-28")
_SRC_TO_DIM = {v: k for k, v in DIM_COL.items()}


def tick_of(ts) -> int:
    return int((pd.Timestamp(ts) - STREAM_START) // pd.Timedelta(minutes=15))


class CubeFrame:
    def __init__(self, database="lc04", max_tick=None):
        conn = db.get_conn(database)
        df = pd.read_sql_query("SELECT * FROM cube_15m", conn)
        conn.close()
        df = df.rename(columns={v: k for k, v in DIM_COL.items()})
        df["tick"] = ((pd.to_datetime(df["bucket_ts"]) - STREAM_START) // pd.Timedelta(minutes=15)).astype(int)
        if max_tick is not None:
            df = df[df["tick"] <= max_tick]
        self.df = df.sort_values("tick").reset_index(drop=True)
        self.ticks = self.df["tick"].values

    def _slice(self, a, b):
        i0, i1 = np.searchsorted(self.ticks, a, "left"), np.searchsorted(self.ticks, b, "right")
        return self.df.iloc[i0:i1]

    @staticmethod
    def _expr(df, terms):
        out = 0.0
        for col, sign in terms:
            out = out + sign * df[col].values
        return out

    def sums(self, metric_id, a, b, filters=None, group_by=()):
        """Numerator and denominator of a governed metric over ticks a..b (inclusive), optionally filtered / grouped.
        Returns a DataFrame with the group columns and n, d (d = 0 for plain sums)."""
        m = registry.get(metric_id)
        allowed = set(m.allowed_dims)
        for dim in list(group_by) + list(filters or {}):
            if dim not in allowed:
                raise MetricError(f"{metric_id} cannot be sliced by {dim!r}")
        sub = self._slice(a, b)
        for dim, v in (filters or {}).items():
            sub = sub[sub[dim].isin([v] if isinstance(v, str) else list(v))]
        n = self._expr(sub, m.num_terms)
        d = self._expr(sub, m.den_terms) if m.den_terms else np.zeros(len(sub))
        tmp = pd.DataFrame({**{g: sub[g].values for g in group_by}, "n": n, "d": d})
        if group_by:
            return tmp.groupby(list(group_by), as_index=False)[["n", "d"]].sum()
        return pd.DataFrame([{"n": float(tmp["n"].sum()), "d": float(tmp["d"].sum())}])
