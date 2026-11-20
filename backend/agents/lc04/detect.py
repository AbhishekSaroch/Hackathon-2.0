"""
LC-04 anomaly detector: finds segments whose key metric is off AFTER accounting for seasonality, within the hour.

One idea, applied to every monitored series (a "series" = one metric in one segment, e.g. connect rate in city Pune):

  RATE metrics (connect rate, payment success rate, PTP capture rate): n and d are the metric's own numerator and
    denominator in the segment. Expected rate = the same clock window on up to 14 previous days (days with a known event
    or an earlier alert left out). Test: binomial (exact tail when counts are small, normal otherwise).
  COUNT metrics (contact attempts, PTPs kept, events): the day's level moves every segment together (Sunday, salary days,
    month-end, a national festival). So expected = history of THIS segment in the same window x today's level, where the
    level is the weighted MEDIAN of today/history over all segments of the same kind (cities, lenders ...). A median, not a
    total, so one stalled lender or one dead dialer does not make every other segment look "up".
  The variance includes sampling noise, a dispersion factor phi (fitted on public training days) and the uncertainty of the
  baseline itself. Windows: the last 2, 4, 8 or 16 buckets (30 min ... 4 h): a sharp drop is seen fast, a mild one slowly.
  An alert needs: score >= the window's threshold, the last two buckets deviating the same way, and enough volume to test.
  Thresholds are for ~100 monitored series per metric and are relaxed / tightened by log10(series count / 100)
  (a Bonferroni-style budget: more series, more chances of a chance alert).
  Known calendar events (a regional festival, the PTP outcomes that lag it by up to 4 days) are not alerted and are left
  out of history. Alerts are de-duplicated per series; a catch-up burst after a stall is merged into the stall.
Point-in-time: the score at tick t uses buckets <= t of today and fully past days only (tested by poisoning the future).
Tuned on the PUBLIC days only; thresholds and phi live in data/lc04_detect_params.json.
"""
import json
import math
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "metrics"))
sys.path.insert(0, str(ROOT / "db"))
import compute as C  # noqa: E402
import db  # noqa: E402
import registry  # noqa: E402

SLOTS = 96
WINDOWS = (2, 4, 8, 16)
HIST_DAYS = 14
MIN_HIST_DAYS = 5
FIRST_DAY = 7                       # day index 7 = Oct 5: needs a week of history first
STREAM_START = pd.Timestamp("2026-09-28")
PARAMS_PATH = ROOT / "data" / "lc04_detect_params.json"
CACHE_DIR = ROOT / "data" / "lc04_cache"
LENDERS = ("L1", "L2", "L3", "L4", "L5")

SCAN = [  # metric, the segments (dimension sets) it is monitored by: what deserves attention, per metric
    ("contacts_attempted", [("lender",), ("city",), ("agency",), ("dialer",), ("city", "dialer")]),
    ("connect_rate", [("lender",), ("city",), ("agency",), ("dialer",), ("city", "dialer")]),
    ("ptp_capture_rate", [("lender",), ("city",), ("agency",)]),
    ("ptp_kept", [("lender",), ("product",), ("lender", "product")]),
    ("pay_success_rate", [("gateway",), ("lender",)]),
    ("events_total", [("lender",)]),
]
VOLUME_METRICS = {"contacts_attempted", "events_total", "ptp_kept"}      # hit directly by a regional festival
LAGGED_METRICS = {"ptp_kept"}                                            # PTP outcomes arrive 1-4 days after the calls
FESTIVAL_LAG_DAYS = 4
N_REF = 100
DEFAULT_PARAMS = {"phi": {}, "phi_default": 1.0, "alpha": 1.0,
                  "T": {"2": 7.0, "4": 6.0, "8": 5.5, "16": 5.5},
                  "min_n": {"2": 6.0, "4": 8.0, "8": 12.0, "16": 16.0},      # count metrics: expected count needed to test
                  "min_d": {"2": 5.0, "4": 8.0, "8": 14.0, "16": 24.0}}      # rate metrics: denominator needed to test


def load_params():
    p = json.loads(json.dumps(DEFAULT_PARAMS))
    if PARAMS_PATH.exists():
        for k, v in json.loads(PARAMS_PATH.read_text()).items():
            p[k] = {**p.get(k, {}), **v} if isinstance(v, dict) else v
    return p


@dataclass
class Series:
    metric: str
    dims: tuple
    vals: tuple
    n: np.ndarray
    d: np.ndarray
    is_ratio: bool

    @property
    def segment(self):
        return dict(zip(self.dims, self.vals))

    def __post_init__(self):
        self.cn = np.concatenate([[0.0], np.cumsum(self.n)])
        self.cd = np.concatenate([[0.0], np.cumsum(self.d)])


# ---------------------------------------------------------------------------------------------- data
def _tick_map(n_days):
    return {(STREAM_START + pd.Timedelta(minutes=15 * g)).strftime("%Y-%m-%d %H:%M:%S"): g for g in range(n_days * SLOTS)}


def build_series(n_days=30, use_cache=True, database="lc04"):
    """Monitored series from the governed metrics (compute(), the same numbers every view shows), all lenders."""
    conn = db.get_conn(database)
    end = (STREAM_START + pd.Timedelta(days=n_days)).strftime("%Y-%m-%d %H:%M:%S")      # only the stream days (a demo day after them changes nothing)
    sig = (n_days, tuple(conn.execute("SELECT COUNT(*), MAX(bucket_ts), SUM(ev_total), SUM(pay_failed) FROM cube_15m WHERE bucket_ts < ?", (end,)).fetchone()), str(SCAN))
    conn.close()
    cache = CACHE_DIR / "series.pkl"
    if use_cache and cache.exists():
        saved = pickle.loads(cache.read_bytes())
        if saved["sig"] == sig:
            return saved["series"]
    scope, G = C.Scope(LENDERS), n_days * SLOTS
    tm = _tick_map(n_days)
    start, end = STREAM_START, STREAM_START + pd.Timedelta(days=n_days)
    out = []
    for metric, dimsets in SCAN:
        is_ratio = bool(registry.get(metric).denominator)
        for dims in dimsets:
            acc = {}
            for r in C.compute(metric, scope, start, end, group_by=list(dims), grain="bucket"):
                a = acc.setdefault(tuple(r[x] for x in dims), (np.zeros(G), np.zeros(G)))
                g = tm[r["time"]]
                a[0][g] = r["numerator"]
                if is_ratio:
                    a[1][g] = r["denominator"]
            for key, (n, d) in sorted(acc.items()):
                out.append(Series(metric, tuple(dims), key, n, d, is_ratio))
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps({"sig": sig, "series": out}))
    return out


@dataclass
class Calendar:
    festival_days: dict       # city -> set of day indexes (regional festivals)
    agency_city: dict         # agency -> {city: share of its accounts}


def load_calendar(database="lc04") -> Calendar:
    fest = {}
    for r in db.query("GLOBAL", "SELECT day, city FROM lc_calendar WHERE tenant_id=:tenant_id AND kind='festival'", database=database):
        if r["city"] != "*":
            fest.setdefault(r["city"], set()).add((pd.Timestamp(r["day"]) - STREAM_START).days)
    cnt = {}
    for t in LENDERS:
        for r in db.query(t, "SELECT agency_id, city, COUNT(*) n FROM lc_accounts WHERE tenant_id=:tenant_id GROUP BY agency_id, city", database=database):
            cnt.setdefault(r["agency_id"], {})[r["city"]] = cnt.get(r["agency_id"], {}).get(r["city"], 0) + r["n"]
    share = {a: {c: n / sum(v.values()) for c, n in v.items()} for a, v in cnt.items()}
    return Calendar(fest, share)


def known_event(s: Series, day: int, cal: Calendar) -> bool:
    """True when a calendar event explains a swing in this series on this day (not alerted, not learned from)."""
    cities = []
    if "city" in s.dims:
        cities = [s.segment["city"]]
    elif "agency" in s.dims:                                  # an agency based (>= 30%) in the festival city
        cities = [c for c, sh in cal.agency_city.get(s.segment["agency"], {}).items() if sh >= .3]
    for c in cities:
        days = cal.festival_days.get(c, ())
        if s.metric in VOLUME_METRICS and day in days and ("city" in s.dims or "agency" in s.dims):
            return True
        if s.metric in LAGGED_METRICS and any(0 <= day - fd <= FESTIVAL_LAG_DAYS for fd in days):
            return True
    return False


# ---------------------------------------------------------------------------------------------- statistics
def _window(cum, ticks, W):
    return cum[ticks + 1] - cum[np.maximum(ticks + 1 - W, 0)]


def _hist(s: Series, day, W, valid):
    T = day * SLOTS + np.arange(SLOTS)
    hn, hd, k_ok = np.zeros(SLOTS), np.zeros(SLOTS), 0
    for k in range(1, HIST_DAYS + 1):
        if day - k < 0:
            break
        if not valid(day - k):
            continue
        Tk = T - SLOTS * k
        hn += _window(s.cn, Tk, W)
        hd += _window(s.cd, Tk, W)
        k_ok += 1
    return hn, hd, k_ok


def _pvalue(z, n, exp, lo_scale, hi_scale, kind, d=None, p=None):
    """Two-sided p. Normal tail when the counts are large, otherwise the exact tail with the baseline moved one standard
    error AGAINST the alert (conservative about baseline uncertainty)."""
    pv = 2 * stats.norm.sf(np.abs(z))
    if kind == "ratio":
        sparse = (exp < 20) | (d - exp < 20)
    else:
        sparse = exp < 20
    sparse &= np.isfinite(exp) & (exp > 0)
    if sparse.any():
        n_i = np.round(n[sparse]).astype(int)
        if kind == "ratio":
            d_i = np.round(d[sparse]).astype(int)
            n_i = np.minimum(n_i, d_i)
            pl, ph = np.clip(p[sparse] * lo_scale[sparse], 1e-9, 1 - 1e-9), np.clip(p[sparse] * hi_scale[sparse], 1e-9, 1 - 1e-9)
            lower, upper = stats.binom.cdf(n_i, d_i, pl), stats.binom.sf(n_i - 1, d_i, ph)
        else:
            lower = stats.poisson.cdf(n_i, exp[sparse] * lo_scale[sparse])
            upper = stats.poisson.sf(n_i - 1, exp[sparse] * hi_scale[sparse])
        pv[sparse] = np.minimum(1.0, 2 * np.minimum(lower, upper))
    return pv


KAPPA = 40.0      # pseudo-observations: a baseline rate built from few observations is pulled toward the group's pooled rate


def stats_ratio_group(group, day, W, phi, valids, min_d):
    """Rate metrics: each segment against its own history, whose rate is shrunk toward the pooled rate of the group
    (so a baseline from a handful of observations, e.g. 'no failures yet', cannot make normal noise look like an anomaly)."""
    T = day * SLOTS + np.arange(SLOTS)
    H = [_hist(s, day, W, valids[i]) for i, s in enumerate(group)]
    good = [h for h in H if h[2] >= MIN_HIST_DAYS]
    if not good:
        return [None] * len(group)
    with np.errstate(divide="ignore", invalid="ignore"):
        p0 = sum(h[0] for h in good) / np.maximum(sum(h[1] for h in good), 1e-9)
    out = []
    for i, s in enumerate(group):
        hn, hd, k_ok = H[i]
        if k_ok < MIN_HIST_DAYS:
            out.append(None)
            continue
        nW, dW = _window(s.cn, T, W), _window(s.cd, T, W)
        with np.errstate(divide="ignore", invalid="ignore"):
            p = (hn + KAPPA * p0) / (hd + KAPPA)
            exp = dW * p
            testable = (dW >= min_d) & (p > 1e-4) & (p < 1 - 1e-4)    # exact tails handle small counts
            var = p * (1 - p) * (phi * dW + dW ** 2 / (hd + KAPPA))
            z = np.where(testable, (nW - exp) / np.sqrt(np.maximum(var, 1e-9)), 0.0)
            se = np.sqrt(p * (1 - p) / (hd + KAPPA))                  # standard error of the baseline rate
            lo, hi = (p + se) / np.maximum(p, 1e-9), np.maximum(p - se, 1e-9) / np.maximum(p, 1e-9)
        pv = _pvalue(z, nW, exp, lo, hi, "ratio", d=dW, p=p)
        score = np.where(testable, -np.log10(np.maximum(pv, 1e-300)), 0.0)
        out.append(dict(T=T, n=nW, exp=exp, d=dW, p=p, z=z, score=score, sign=np.sign(nW - exp), testable=testable,
                        kind="ratio", W=W))
    return out


def weighted_median(r, w):
    """Per column weighted median of r (m x ticks); entries with w == 0 are ignored."""
    order = np.argsort(r, axis=0)
    rs, ws = np.take_along_axis(r, order, 0), np.take_along_axis(w, order, 0)
    cw = np.cumsum(ws, axis=0)
    k = (cw >= cw[-1] / 2).argmax(axis=0)
    return rs[k, np.arange(r.shape[1])]


def stats_count_group(group, day, W, phi, valids, knowns, min_n):
    """All segments of one count metric / dimension set together (the day's level is estimated across them)."""
    T = day * SLOTS + np.arange(SLOTS)
    m = len(group)
    nW, hbar, hn, ok = np.zeros((m, SLOTS)), np.zeros((m, SLOTS)), np.zeros((m, SLOTS)), np.zeros(m, bool)
    for i, s in enumerate(group):
        nW[i] = _window(s.cn, T, W)
        h, _, k_ok = _hist(s, day, W, valids[i])
        if k_ok >= MIN_HIST_DAYS:
            hn[i], hbar[i], ok[i] = h, h / k_ok, True
    use = ok & ~np.asarray(knowns)                                   # segments that define today's level
    use_mat = use[:, None] & (hbar >= min_n)
    if use_mat.sum(axis=0).max() < 3:
        return [None] * m
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(use_mat, nW / np.where(hbar > 0, hbar, 1), 0.0)
        L = weighted_median(r, np.where(use_mat, hbar, 0.0))
        L = np.where(use_mat.sum(axis=0) >= 3, L, np.nan)
        n_level = np.where(use_mat, nW, 0).sum(axis=0)               # counts behind the level estimate
    out = []
    for i, s in enumerate(group):
        if not ok[i]:
            out.append(None)
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            exp = L * hbar[i]
            testable = (hbar[i] >= min_n) & np.isfinite(exp) & (exp > 0)
            rel = 1.0 / np.maximum(hn[i], 1) + 1.0 / np.maximum(n_level, 1)             # baseline + level uncertainty (relative variance)
            var = phi * exp + exp ** 2 * rel
            z = np.where(testable, (nW[i] - exp) / np.sqrt(np.maximum(var, 1e-9)), 0.0)
            se = np.sqrt(rel)
        pv = _pvalue(z, nW[i], exp, 1 + se, np.maximum(1 - se, 0.05), "count")
        score = np.where(testable, -np.log10(np.maximum(pv, 1e-300)), 0.0)
        out.append(dict(T=T, n=nW[i], exp=exp, d=np.full(SLOTS, np.nan), p=np.full(SLOTS, np.nan), z=z, score=score,
                        sign=np.sign(nW[i] - exp), testable=testable, kind="count", W=W))
    return out


def group_stats(group, day, W, phi, valids, knowns, params):
    if group[0].is_ratio:
        return stats_ratio_group(group, day, W, phi, valids, params["min_d"][str(W)])
    return stats_count_group(group, day, W, phi, valids, knowns, params["min_n"][str(W)])


def _dev_vec(s: Series, st, b):
    """Observed minus expected in the bucket b buckets before each tick (for the persistence check)."""
    Tb = np.maximum(st["T"] - b, 0)
    if st["kind"] == "ratio":
        return s.n[Tb] - s.d[Tb] * np.nan_to_num(st["p"])
    return s.n[Tb] - np.nan_to_num(st["exp"]) / st["W"]


def _dev_at(s: Series, g, st, i):
    """Same, for one earlier bucket g (used to trim the onset)."""
    if st["kind"] == "ratio":
        return s.n[g] - s.d[g] * st["p"][i]
    return s.n[g] - st["exp"][i] / st["W"]


def groups_of(series):
    out = {}
    for s in series:
        out.setdefault((s.metric, s.dims), []).append(s)
    return out


def thresholds(series, params):
    """Per metric and window: the base threshold (for ~100 series) adjusted by log10(series count / 100)."""
    n_m = {}
    for s in series:
        n_m[s.metric] = n_m.get(s.metric, 0) + 1
    return {m: {W: params["T"][str(W)] + params["alpha"] * math.log10(n / N_REF) for W in WINDOWS} for m, n in n_m.items()}


def fit_phi(series, cal, params, days):
    """Dispersion per metric from the robust spread of the 1-hour z-scores on training days (phi = 1 is pure sampling noise)."""
    zs = {}
    for (metric, dims), grp in groups_of(series).items():
        for d in days:
            valids = [(lambda dd, s=s: not known_event(s, dd, cal)) for s in grp]
            knowns = [known_event(s, d, cal) for s in grp]
            res = group_stats(grp, d, 4, 1.0, valids, knowns, params)
            for st in res:
                if st is not None:
                    zs.setdefault(metric, []).append(st["z"][st["testable"]])
    phi = {}
    for m, v in zs.items():
        z = np.concatenate(v)
        q = np.quantile(np.abs(z), 0.99) / 2.576                      # tail-based, so heavy tails (clustered events) are not understated
        phi[m] = round(float(max(1.0, q ** 2)), 2)
    return phi


def bucket_end(g):
    return STREAM_START + pd.Timedelta(minutes=15 * (g + 1))


def run(series, cal, params=None, first_day=FIRST_DAY, last_day=29):
    """Replay days first_day..last_day in order. Returns alerts (list of dicts) sorted by detection time."""
    params = params or load_params()
    thr = thresholds(series, params)
    groups = groups_of(series)
    ok_day = {id(s): np.ones(last_day + 2, bool) for s in series}       # a series' day with its own alert is not learned from
    alerts = []
    for d in range(first_day, last_day + 1):
        today = []
        for (metric, dims), grp in groups.items():
            phi = params["phi"].get(metric, params["phi_default"])
            knowns = [known_event(s, d, cal) for s in grp]
            valids = [(lambda dd, s=s: ok_day[id(s)][dd] and not known_event(s, dd, cal)) for s in grp]
            fire = np.zeros((len(grp), SLOTS), bool)
            info = [dict() for _ in grp]
            for W in WINDOWS:
                res = group_stats(grp, d, W, phi, valids, knowns, params)
                for i, (s, st) in enumerate(zip(grp, res)):
                    if st is None or knowns[i]:
                        continue
                    agree = np.ones(SLOTS, bool)
                    for b in (0, 1):
                        agree &= _dev_vec(s, st, b) * st["sign"] >= 0
                    f = st["testable"] & (st["score"] >= thr[metric][W]) & agree
                    for t in np.flatnonzero(f):
                        if t not in info[i] or st["score"][t] > info[i][t]["score"]:
                            info[i][t] = dict(W=W, n=st["n"][t], exp=st["exp"][t], d=st["d"][t], sign=st["sign"][t],
                                              score=st["score"][t], st=st, t=t)
                    fire[i] |= f
            for i, s in enumerate(grp):
                ticks = np.flatnonzero(fire[i])
                if not len(ticks):
                    continue
                gps, cur = [], [ticks[0]]
                for t in ticks[1:]:
                    if t - cur[-1] <= 3:                               # a gap of up to 3 buckets is the same alert
                        cur.append(t)
                    else:
                        gps.append(cur)
                        cur = [t]
                gps.append(cur)
                prev = None
                for grp_t in gps:
                    inf = info[i][grp_t[0]]
                    g = d * SLOTS + grp_t[0]
                    onset = g - (inf["W"] - 1)
                    for b in range(1, inf["W"]):                       # trim the onset to the first bucket that deviates the same way
                        if _dev_at(s, g - b, inf["st"], grp_t[0]) * inf["sign"] > 0:
                            onset = g - b
                        else:
                            break
                    a = dict(metric=metric, dims=dims, segment=s.segment, day=d, first_tick=g, last_tick=d * SLOTS + grp_t[-1],
                             onset_ts=str(STREAM_START + pd.Timedelta(minutes=15 * onset)), detected_at=str(bucket_end(g)),
                             direction="down" if inf["sign"] < 0 else "up", score=round(float(inf["score"]), 2),
                             window=int(inf["W"]), observed=float(inf["n"]), expected=float(inf["exp"]),
                             denominator=None if np.isnan(inf["d"]) else float(inf["d"]), catchup=False,
                             tenant=s.segment.get("lender", "GLOBAL"))
                    if prev is not None and prev["direction"] != a["direction"] and a["first_tick"] - prev["last_tick"] <= 8:
                        a["catchup"] = True                            # the burst after a stall / outage: part of the same incident
                    a["_sid"] = id(s)
                    today.append(a)
                    prev = a
        alerts += today
        for a in today:
            if not a["catchup"]:
                ok_day[a["_sid"]][d] = False
    return sorted(alerts, key=lambda a: (a["detected_at"], a["metric"]))


def incidents(alerts):
    """Light grouping: alerts that fire within 60 min of each other on compatible segments (no conflicting value on a shared
    dimension) form one incident. Step 7 (drill-down) refines this by the driving segment."""
    live = [a for a in alerts if not a["catchup"]]
    groups = []
    for a in live:
        for grp in groups:
            if any(abs(pd.Timestamp(a["detected_at"]) - pd.Timestamp(b["detected_at"])) <= pd.Timedelta(minutes=60)
                   and all(a["segment"][k] == b["segment"][k] for k in set(a["segment"]) & set(b["segment"])) for b in grp):
                grp.append(a)
                break
        else:
            groups.append([a])
    return groups
