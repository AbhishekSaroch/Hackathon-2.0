"""
LC-04 drill-down: for an alert, find the SEGMENT that drives it (not just restate the symptom), prove it, and write the finding.

Given an alert (metric, segment S0, onset, detection) it
  1. scans every narrower segment inside S0: one extra dimension, then two (city x dialer, agency x product ...), over the
     anomaly window, against the same clock window on up to 14 previous days (days with an alert / a known event left out);
  2. scores each by its EXPLANATORY POWER (the share of S0's deviation it carries) and by how unlikely its own deviation is;
  3. picks the segment with the strongest deviation among those that explain at least 60% of it (a parent segment dilutes the
     effect, a sub-segment loses volume, so the driver is where the evidence peaks);
  4. verifies by COUNTERFACTUAL: take that segment out of S0; the rest must be back to normal. If nothing narrower explains the
     deviation, S0 itself is the driver and the check is run on the rest of the book instead.
All numbers are code (CubeFrame = the governed definitions; tests pin them to compute()); the headline is templated from them.

    inv = Investigator(frame, calendar)
    finding = inv.investigate(alert)
    incidents = inv.group(findings)
"""
import math
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
import detect as D  # noqa: E402
import registry  # noqa: E402
from frame import CubeFrame, SLOTS, STREAM_START, tick_of  # noqa: E402

PHI = 1.3
KAPPA = 40.0
MIN_EP = 0.60                 # a driving segment must carry at least this share of the deviation
MIN_Z = 3.0                   # and its own deviation must be significant
MAX_REST = 0.35               # counterfactual: the rest keeps at most this share of the deviation (or looks normal)
MIN_GAIN = 1.2                # a narrower segment must be at least this much MORE intense than the alert's own segment (a segment
                              # that is 63% of its parent can gain at most 1.6x, so this stays low; z_diff below is the real guard)
MIN_DIFF = 4.5                # ... and decisively different from the rest of it (z of the difference; many candidates are tested)
HIST_DAYS = 14
MIN_HIST = 5


def segment_text(seg: dict) -> str:
    order = ["lender", "city", "agency", "dialer", "gateway", "product", "dpd_bucket"]
    label = {"lender": "lender", "agency": "agency", "dialer": "dialer", "gateway": "gateway", "dpd_bucket": "DPD bucket"}
    return " / ".join(f"{label[k]} {seg[k]}" if k in label else str(seg[k]) for k in order if k in seg)


def fmt_value(v, unit):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return {"pct": f"{v:.1f}%", "rs": f"Rs {v:,.0f}", "ratio": f"{v:.2f}", "count": f"{v:,.0f}"}[unit]


@dataclass
class Finding:
    alert: dict
    segment: dict                       # the driving segment
    parent: dict                        # the alert's own segment (S0)
    metric: str
    direction: str
    window: tuple                       # (first tick, last tick) of the anomaly window
    observed: dict                      # n, d, value
    expected: dict
    explained: float                    # explanatory power (share of S0's deviation)
    z: float
    counterfactual_ok: bool
    counterfactual: dict
    candidates: list = field(default_factory=list)
    headline: str = ""
    confirmed: bool = False
    intensity: float = 0.0              # relative size of the deviation inside the driving segment
    relaxed: bool = False               # True when too few clean history days were left and alert days were put back

    def to_json(self):
        return dict(segment=self.segment, parent=self.parent, metric=self.metric, direction=self.direction,
                    window=[str(STREAM_START + pd.Timedelta(minutes=15 * self.window[0])),
                            str(STREAM_START + pd.Timedelta(minutes=15 * (self.window[1] + 1)))],
                    observed=self.observed, expected=self.expected, explained=round(self.explained, 3), z=round(self.z, 2),
                    counterfactual_ok=self.counterfactual_ok, counterfactual=self.counterfactual,
                    candidates=self.candidates[:6], confirmed=self.confirmed)


class Investigator:
    def __init__(self, frame: CubeFrame, cal: D.Calendar | None = None):
        self.f, self.cal = frame, cal
        self.exclude_days: set = set()           # (kept for callers that want to exclude whole days)
        self.alert_log: list = []                # (day, segment) of earlier alerts: see note_alert()
        self.relaxed = False
        self._pops: dict = {}

    # ---------------------------------------------------------------- history windows
    def note_alert(self, alert: dict):
        """Remember an alert: that day is not learned from FOR SEGMENTS THAT OVERLAP IT (it is a normal day for the rest of the book)."""
        self.alert_log.append((alert["day"], dict(alert["segment"])))

    def _same_area(self, a: dict, b: dict, day: int) -> bool:
        nested = all(b.get(k) == v for k, v in a.items()) or all(a.get(k) == v for k, v in b.items())
        return nested or self.overlap(a, b, day) >= .3

    def _hist_windows(self, a, b, day, seg=None):
        """Same clock window on up to 14 previous days. A day with an earlier alert on an overlapping segment is left out; if that
        leaves fewer than MIN_HIST days, the closest left-out days are put back rather than giving up (flagged in self.relaxed):
        an incident must never be dropped silently for lack of history."""
        out, skipped = [], []
        for k in range(1, HIST_DAYS + 1):
            if day - k < 0:
                break
            hit = (day - k) in self.exclude_days or (seg is not None and any(
                d == day - k and self._same_area(aseg, seg, day - k) for d, aseg in self.alert_log))
            (skipped if hit else out).append((a - SLOTS * k, b - SLOTS * k))
        self.relaxed = False
        if len(out) < MIN_HIST and skipped:
            out = sorted(out + skipped[:MIN_HIST - len(out)], reverse=True)
            self.relaxed = True
        return out

    def _hist_sums(self, metric, wins, filters, group_by):
        """Mean over the history windows of n and d per segment, plus the pooled counts."""
        acc = {}
        for a, b in wins:
            df = self.f.sums(metric, a, b, filters, group_by)
            for row in df.itertuples(index=False):
                key = tuple(getattr(row, g) for g in group_by)
                n, d, c = acc.get(key, (0.0, 0.0, 0))
                acc[key] = (n + row.n, d + row.d, c)
        return acc

    # ---------------------------------------------------------------- scoring one segment
    def _score(self, is_ratio, n, d, hn, hd, k, level, p_prior):
        """expected numerator, expected denominator-based rate, delta and z for one segment."""
        if is_ratio:
            p = (hn + KAPPA * p_prior) / (hd + KAPPA) if (hd + KAPPA) > 0 else float("nan")
            exp = d * p
            var = max(p * (1 - p) * (PHI * d + d * d / (hd + KAPPA)), 1e-9)
        else:
            exp = level * (hn / k)
            var = max(PHI * exp + exp * exp / max(hn, 1.0), 1e-9)
            p = float("nan")
        return exp, p, (n - exp), (n - exp) / math.sqrt(var), var

    # ---------------------------------------------------------------- the drill-down
    def investigate(self, alert: dict, extend: int = 0) -> Finding:
        """Drill into an alert. extend = more buckets of data AFTER detection (a refinement run, e.g. 4 = one hour later, when more
        events have arrived and a segment can be told apart from its neighbours); the caller must only ask for data that exists."""
        metric = alert["metric"]
        m = registry.get(metric)
        is_ratio = bool(m.denominator)
        S0 = dict(alert["segment"])
        g = alert["first_tick"]
        a = max(tick_of(alert["onset_ts"]), g - 15)
        b = g + extend
        day = g // SLOTS
        wins = self._hist_windows(a, b, day, S0)
        relaxed = self.relaxed
        if len(wins) < MIN_HIST:
            raise ValueError("not enough history to investigate")
        sign = 1 if alert["direction"] == "up" else -1

        # level: how far the rest of the book (outside S0) is from its own history today (removes Sunday / salary / festival)
        level = 1.0
        if not is_ratio:
            tot_now = self.f.sums(metric, a, b).iloc[0]["n"]
            s0_now = self.f.sums(metric, a, b, S0).iloc[0]["n"]
            tot_h = sum(self.f.sums(metric, x, y).iloc[0]["n"] for x, y in wins) / len(wins)
            s0_h = sum(self.f.sums(metric, x, y, S0).iloc[0]["n"] for x, y in wins) / len(wins)
            level = (tot_now - s0_now) / max(tot_h - s0_h, 1e-9)
        # S0 itself
        now0 = self.f.sums(metric, a, b, S0).iloc[0]
        h0 = self._hist_sums(metric, wins, S0, ())[()]
        k = len(wins)
        p_prior = h0[0] / max(h0[1], 1e-9) if is_ratio else float("nan")
        exp0, p0, delta0, z0, var0 = self._score(is_ratio, now0["n"], now0["d"], h0[0], h0[1], k, level, p_prior)
        if is_ratio and not (p_prior > 0):
            raise ValueError("no baseline rate")

        cands = [dict(segment=S0, n=now0["n"], d=now0["d"], exp=exp0, p=p0, delta=delta0, z=z0, ep=1.0, var=var0)]
        free = [x for x in m.allowed_dims if x not in S0]
        for r in (1, 2):
            for dims in combinations(free, r):
                now = self.f.sums(metric, a, b, S0, dims)
                hist = self._hist_sums(metric, wins, S0, dims)
                for row in now.itertuples(index=False):
                    key = tuple(getattr(row, x) for x in dims)
                    hn, hd, _ = hist.get(key, (0.0, 0.0, 0))
                    if hn <= 0 and not is_ratio:
                        continue
                    exp, p, delta, z, var = self._score(is_ratio, row.n, row.d, hn, hd, k, level, p_prior)
                    seg = {**S0, **dict(zip(dims, key))}
                    cands.append(dict(segment=seg, n=row.n, d=row.d, exp=exp, p=p, delta=delta, z=z, var=var,
                                      ep=(delta / delta0) if delta0 != 0 else 0.0))
        eligible = [c for c in cands if np.sign(c["delta"]) == sign and c["ep"] >= MIN_EP and abs(c["z"]) >= MIN_Z
                    and (c["exp"] >= 3 or (is_ratio and c["d"] >= 6))]

        def counterfactual(c):
            """Take c out of S0: the rest must look normal."""
            n_rest, d_rest = now0["n"] - c["n"], now0["d"] - c["d"]
            exp_rest = exp0 - c["exp"]
            if is_ratio:
                p_rest = min(max(exp_rest / max(d_rest, 1e-9), 1e-4), 1 - 1e-4)
                var = max(p_rest * (1 - p_rest) * PHI * d_rest, 1e-9)
            else:
                var = max(PHI * max(exp_rest, 0.0), 1e-9)
            z_rest = (n_rest - exp_rest) / math.sqrt(var)
            share = (n_rest - exp_rest) / delta0 if delta0 else 0.0
            # is c decisively MORE affected than the rest of S0? (two-sample test on the relative deviations)
            r_c, r_r = c["delta"] / max(c["exp"], 1e-9), (n_rest - exp_rest) / max(exp_rest, 1e-9)
            v_c, v_r = c["var"] / max(c["exp"], 1e-9) ** 2, var / max(exp_rest, 1e-9) ** 2
            z_diff = (r_c - r_r) / math.sqrt(v_c + v_r) if exp_rest > 0.5 else 0.0
            return (bool(abs(z_rest) < MIN_Z or abs(share) <= MAX_REST),
                    dict(kind="rest of the segment", observed=round(float(n_rest), 2), expected=round(float(exp_rest), 2),
                         z=round(float(z_rest), 2), rest_share=round(float(share), 3), z_diff=round(float(z_diff), 2)))

        # a narrower segment is the driver only if its effect is clearly MORE intense than S0's (a dead dialer in one city is a
        # 90% drop inside a city that drops 36%); if the whole of S0 is affected evenly, S0 itself is the driver.
        inten0 = abs(delta0) / max(exp0, 1e-9)
        narrower = []
        for c in eligible:
            if c["segment"] == S0:
                continue
            c["intensity"] = abs(c["delta"]) / max(c["exp"], 1e-9)
            ok_cf, cf_c = counterfactual(c)
            if c["intensity"] >= MIN_GAIN * inten0 and ok_cf and abs(cf_c["z_diff"]) >= MIN_DIFF:
                narrower.append((c, cf_c))
        s0_ok = any(c["segment"] == S0 for c in eligible)
        if narrower:
            zmax = max(abs(c["z"]) for c, _ in narrower)
            best, cf = min(((c, f_) for c, f_ in narrower if abs(c["z"]) >= .85 * zmax), key=lambda t: (len(t[0]["segment"]), -abs(t[0]["z"])))
            cf_ok = True
        else:
            best = cands[0]                                              # S0 itself
            outside = self._rest_of_book(metric, a, b, wins, S0, is_ratio, level, k)
            cf = dict(kind="rest of the book", z=round(float(outside), 2), rest_share=0.0)
            cf_ok = bool(abs(outside) < MIN_Z)
            eligible = [best] if s0_ok else []

        seg = best["segment"]
        unit = m.unit
        if is_ratio:
            mult = registry.UNITS[unit]
            obs_v = best["n"] * mult / best["d"] if best["d"] else None
            exp_v = best["p"] * mult
        else:
            obs_v, exp_v = best["n"], best["exp"]
        f = Finding(alert=alert, segment=seg, parent=S0, metric=metric, direction=alert["direction"], window=(a, b),
                    observed=dict(n=float(best["n"]), d=float(best["d"]) if is_ratio else None, value=obs_v),
                    expected=dict(n=float(best["exp"]), value=exp_v), explained=float(best["ep"]), z=float(best["z"]),
                    counterfactual_ok=cf_ok, counterfactual=cf,
                    candidates=[dict(segment=c["segment"], explained=round(float(c["ep"]), 3), z=round(float(c["z"]), 2))
                                for c in sorted(cands, key=lambda c: -abs(c["z"]))[:8]])
        f.intensity = float(abs(best["delta"]) / max(best["exp"], 1e-9))
        f.relaxed = bool(relaxed)
        f.confirmed = bool(best in eligible and cf_ok and abs(best["z"]) >= MIN_Z + 1)
        f.headline = headline(f, m)
        return f

    def _rest_of_book(self, metric, a, b, wins, S0, is_ratio, level, k):
        """z of everything outside S0 over the window (0 = normal). Used when S0 itself is the driver."""
        tot = self.f.sums(metric, a, b)
        s0 = self.f.sums(metric, a, b, S0)
        n, d = tot.iloc[0]["n"] - s0.iloc[0]["n"], tot.iloc[0]["d"] - s0.iloc[0]["d"]
        hn = hd = 0.0
        for x, y in wins:
            t_, s_ = self.f.sums(metric, x, y).iloc[0], self.f.sums(metric, x, y, S0).iloc[0]
            hn += t_["n"] - s_["n"]
            hd += t_["d"] - s_["d"]
        if is_ratio:
            p = hn / max(hd, 1e-9)
            exp = d * p
            var = max(p * (1 - p) * PHI * d, 1e-9)
        else:
            exp = (hn / k) * level
            var = max(PHI * exp, 1e-9)
        return (n - exp) / math.sqrt(var)

    # ---------------------------------------------------------------- incidents
    def measure(self, metric: str, seg: dict, a: int, b: int) -> dict | None:
        """Observed vs expected for ONE segment over ticks a..b (same baseline logic as the drill-down). Used to write an alert in
        the numbers of a single lender (a lender user must not see other lenders' figures)."""
        m = registry.get(metric)
        is_ratio = bool(m.denominator)
        day = b // SLOTS
        wins = self._hist_windows(a, b, day, seg)
        if len(wins) < MIN_HIST:
            return None
        k = len(wins)
        level = 1.0
        if not is_ratio:
            tot_now = self.f.sums(metric, a, b).iloc[0]["n"]
            s_now = self.f.sums(metric, a, b, seg).iloc[0]["n"]
            tot_h = sum(self.f.sums(metric, x, y).iloc[0]["n"] for x, y in wins) / k
            s_h = sum(self.f.sums(metric, x, y, seg).iloc[0]["n"] for x, y in wins) / k
            level = (tot_now - s_now) / max(tot_h - s_h, 1e-9)
        now = self.f.sums(metric, a, b, seg).iloc[0]
        hn, hd, _ = self._hist_sums(metric, wins, seg, ())[()]
        p_prior = hn / max(hd, 1e-9) if is_ratio else float("nan")
        exp, p, delta, z, var = self._score(is_ratio, now["n"], now["d"], hn, hd, k, level, p_prior)
        mult = registry.UNITS[m.unit]
        return dict(n=float(now["n"]), d=float(now["d"]) if is_ratio else None, exp=float(exp), z=float(z),
                    value=(now["n"] * mult / now["d"] if is_ratio and now["d"] else None) if is_ratio else float(now["n"]),
                    exp_value=(p * mult) if is_ratio else float(exp))

    def _pop(self, seg, day):
        key = (tuple(sorted(seg.items())), day)
        if key not in self._pops:
            self._pops[key] = float(self.f.sums("events_total", day * SLOTS, day * SLOTS + SLOTS - 1, seg).iloc[0]["n"])
        return self._pops[key]

    def overlap(self, a: dict, b: dict, day: int) -> float:
        """Share of the smaller segment's population that lies inside the other (0 if they contradict each other)."""
        if any(k in b and b[k] != v for k, v in a.items()):
            return 0.0
        d = max(day - 1, 0)
        return self._pop({**a, **b}, d) / max(min(self._pop(a, d), self._pop(b, d)), 1.0)

    def group(self, findings):
        """One incident per root cause: findings within 2 hours whose segments are nested or cover mostly the same part of the book
        (one dead dialer shows up in calls, connect rate and events, in its city, its agencies and the dialer)."""
        out = []
        for f in sorted(findings, key=lambda f: -abs(f.z)):
            for inc in out:
                p = inc[0]
                near = abs(f.window[1] - p.window[1]) <= 8
                a, b = f.segment, p.segment
                nested = all(b.get(k) == v for k, v in a.items()) or all(a.get(k) == v for k, v in b.items())
                if near and (nested or self.overlap(a, b, f.window[1] // SLOTS) >= .5):
                    inc.append(f)
                    break
            else:
                out.append([f])
        return out

    def primary(self, incident):
        """The incident's driving segment. Every finding already tested its own sub-segments with the decisive-difference rule, so
        the finding of the alert with the BROADEST scope is the one that has seen the whole picture; among confirmed findings take
        that one (strongest evidence breaks ties). If nothing is confirmed, the strongest finding."""
        conf = [f for f in incident if f.confirmed]
        if not conf:
            return max(incident, key=lambda f: abs(f.z))
        # A finding that only refines the alert segment of another finding (and that finding looked at it and kept the broader
        # segment) is dominated: e.g. "G4 at lender L1" is one of the candidates the alert on gateway G4 already weighed.
        dominated = set()
        for b in conf:
            for n in conf:
                if n is not b and len(n.segment) > len(b.segment) and b.segment == b.parent \
                        and all(n.segment.get(k) == v for k, v in b.segment.items()):
                    dominated.add(id(n))
        pool = [f for f in conf if id(f) not in dominated] or conf
        return max(pool, key=lambda f: abs(f.z))

    def equivalent(self, a: dict, b: dict, day: int, thr: float = .85) -> bool:
        """Two segments the data cannot tell apart: each covers at least `thr` of the other's population
        (an agency that works in one city only is that city, as far as the events can tell)."""
        if any(k in b and b[k] != v for k, v in a.items()):
            return False
        d = max(day - 1, 0)
        both = self._pop({**a, **b}, d)
        return both / max(self._pop(a, d), 1.0) >= thr and both / max(self._pop(b, d), 1.0) >= thr


def headline(f: Finding, m) -> str:
    t0 = (STREAM_START + pd.Timedelta(minutes=15 * f.window[0])).strftime("%H:%M")
    t1 = (STREAM_START + pd.Timedelta(minutes=15 * (f.window[1] + 1))).strftime("%H:%M")
    where = segment_text(f.segment)
    if m.denominator:
        s = (f"{m.name} was {fmt_value(f.observed['value'], m.unit)} in {where} against {fmt_value(f.expected['value'], m.unit)} "
             f"expected ({t0}-{t1}).")
    else:
        pct = abs(f.observed["n"] - f.expected["n"]) / max(f.expected["n"], 1e-9) * 100
        verb = "fell" if f.direction == "down" else "rose"
        s = f"{m.name} {verb} {pct:.0f}% in {where}: {f.observed['n']:,.0f} against {f.expected['n']:,.0f} expected ({t0}-{t1})."
    if f.segment != f.parent:
        s += f" Inside {segment_text(f.parent)} this segment carries {min(f.explained, 1.0) * 100:.0f}% of the deviation."
    if f.counterfactual_ok:
        s += (" Without it the rest of " + segment_text(f.parent) + " is back to normal.") if f.segment != f.parent else \
            " The rest of the book is normal."
    return s
