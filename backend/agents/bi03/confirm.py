"""
BI-03 borderline confirmation: the part of the loop where the agent DECIDES to fetch more data.

Most accounts are clearly safe or clearly risky. An account whose risk sits close to the alert threshold is a coin toss, and the
daily feed is a little stale (bureau data arrives 1-8 days late, statement rows up to a day late). So for those accounts only:

  1 pick tools   the dominant signal says which source can settle it: salary / balance / late EMI -> AA statement,
                 bounces / card use / enquiries / bureau score -> bureau refresh
  2 pull         call the (mock) tool: it returns what exists at the source as of TODAY, including rows the daily feed has not
                 ingested yet. It never returns anything dated after today.
  3 re-score     recompute the account's features with the fresh rows and score again
  4 decide       confirmed (still above tau) | withdrawn (was above, fresh data says no: a false alarm saved) |
                 promoted (was below, fresh data says yes: caught early) | still below

Pulls cost money and need customer consent in real life, so only the borderline band is pulled (never every account).
The AA consent check is not modelled because the brief gives no consent data; a real pull would be skipped without consent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db")]
import features as F  # noqa: E402
import model as M  # noqa: E402

BAND_LOW, BAND_HIGH = 0.6, 1.6            # borderline = score between 0.6x and 1.6x the alert threshold
TOOLS_FOR = {"salary_delay": ("aa_statement",), "low_balance": ("aa_statement",), "late_emi_payments": ("aa_statement",),
             "other_loan_bounce": ("bureau_refresh",), "card_utilisation": ("bureau_refresh",), "new_enquiries": ("bureau_refresh",),
             "bureau_score": ("bureau_refresh",)}
BOTH = ("aa_statement", "bureau_refresh")


def choose_tools(dominant_signal: str | None) -> tuple:
    return TOOLS_FOR.get(dominant_signal, BOTH)


# ------------------------------------------------------------------ the (mock) tools
def bureau_refresh(events: pd.DataFrame, snaps: pd.DataFrame, as_of) -> dict:
    """Mock bureau pull. Returns bureau events and monthly snapshots that exist as of `as_of` but are not in the daily feed yet
    (bureau lag). Nothing dated after `as_of` is ever returned."""
    t = pd.Timestamp(as_of)
    ev = events[(events["source"] == "bureau") & (events["event_date"] <= t) & (events["available_from"] > t)]
    sn = snaps[(snaps["snapshot_date"] <= t) & (snaps["available_from"] > t)]
    return {"events": ev.assign(available_from=t), "snaps": sn.assign(available_from=t)}


def aa_statement(events: pd.DataFrame, as_of) -> dict:
    """Mock Account Aggregator statement pull: bank transactions dated up to `as_of` that the daily feed has not posted yet."""
    t = pd.Timestamp(as_of)
    ev = events[(events["source"] == "statement") & (events["event_date"] <= t) & (events["available_from"] > t)]
    return {"events": ev.assign(available_from=t)}


def fresh_features(events: pd.DataFrame, snaps: pd.DataFrame, accounts: pd.DataFrame, as_of, tools: tuple) -> tuple[pd.DataFrame, dict]:
    """Features for these accounts as of `as_of` using the normal feed PLUS whatever the chosen tools return."""
    t = pd.Timestamp(as_of)
    parts_e, parts_s, pulled = [events[events["available_from"] <= t]], [snaps[snaps["available_from"] <= t]], {}
    if "bureau_refresh" in tools:
        r = bureau_refresh(events, snaps, t)
        parts_e.append(r["events"]); parts_s.append(r["snaps"])
        pulled["bureau_refresh"] = len(r["events"]) + len(r["snaps"])
    if "aa_statement" in tools:
        r = aa_statement(events, t)
        parts_e.append(r["events"])
        pulled["aa_statement"] = len(r["events"])
    return F.compute_features(pd.concat(parts_e, ignore_index=True), pd.concat(parts_s, ignore_index=True), accounts, t), pulled


# ------------------------------------------------------------------ the decision
def confirm_borderline(eng: F.FeatureEngine, bundle: dict, as_of, X: pd.DataFrame, scores: pd.Series) -> dict:
    """Re-score the borderline accounts with fresh data. Returns the updated scores / features and a row per account decided."""
    t, tau, model = pd.Timestamp(as_of), bundle["tau"], bundle["model"]
    band = scores.index[((scores >= BAND_LOW * tau) & (scores <= BAND_HIGH * tau)).to_numpy()]
    out = {"scores": scores, "X": X, "decisions": pd.DataFrame(), "calls": [], "band": int(len(band))}
    if len(band) == 0:
        return out

    dom = M.explain(bundle, X.loc[band])["dominant_signal"]
    tools_of = dom.map(lambda s: "+".join(choose_tools(s)))            # e.g. "aa_statement" or "aa_statement+bureau_refresh"
    new_X = X.copy()
    rows_pulled: dict[str, int] = {}
    accounts_called: dict[str, int] = {}
    for key, idx in tools_of.groupby(tools_of).groups.items():
        tools, ids = tuple(key.split("+")), list(idx)
        ev = eng.events[eng.events["account_id"].isin(ids)]
        sn = eng.snaps[eng.snaps["account_id"].isin(ids)]
        ac = eng.accounts[eng.accounts["account_id"].isin(ids)]
        Xf, pulled = fresh_features(ev, sn, ac, t, tools)
        new_X.loc[ids] = Xf.loc[ids].to_numpy()
        for tool, n in pulled.items():
            rows_pulled[tool] = rows_pulled.get(tool, 0) + n
        for tool in tools:
            accounts_called[tool] = accounts_called.get(tool, 0) + len(ids)

    after = M.score(model, new_X.loc[band])
    before = scores.loc[band]
    was, now = before >= tau, after >= tau
    status = np.select([was & now, was & ~now, ~was & now], ["confirmed", "withdrawn", "promoted"], default="still_below")
    dec = pd.DataFrame({"score_before": before, "score_after": after, "dominant_signal": dom, "tools": tools_of, "status": status})
    new_scores = scores.copy()
    new_scores.loc[band] = after
    out.update({"scores": new_scores, "X": new_X, "decisions": dec})
    out["calls"] = [{"tool": tool, "accounts": accounts_called[tool], "new_rows_returned": rows_pulled.get(tool, 0)} for tool in sorted(accounts_called)]
    return out
