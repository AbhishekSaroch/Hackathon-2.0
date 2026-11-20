"""
LC-04 alert text and audience. Two lines and a link, templated from computed numbers (never written by an LLM: in BI-02 a
language model flipped a fact twice, and a wrong fact in an alert is the worst outcome). Alerts are MOCK: they are rows in the
alerts table shown in the UI; nothing is sent outside.

    line1  what happened      "Contact attempts fell 91% in Hyderabad / dialer D3 since 11:00 (21 against 195 expected)."
    line2  where it comes from "Driver: Hyderabad / dialer D3 (carries 100% of the deviation; the rest of the book is normal). Lenders affected: L1, L2."
    link   the annotated view  "/command/incidents/INC-..."
A lender user / RM gets the same two lines in THEIR lender's numbers only.
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
import drill as DR  # noqa: E402
import registry  # noqa: E402
import roles as RL  # noqa: E402
from frame import STREAM_START  # noqa: E402

MIN_LENDER_SHARE = 0.03          # a lender is "affected" when it holds at least 3% of the segment's events


def clock_text(tick):
    return (STREAM_START + pd.Timedelta(minutes=15 * tick)).strftime("%H:%M")


def two_lines(f, numbers=None, lenders=None, who="platform"):
    """f = Finding; numbers = dict from Investigator.measure for a lender-scoped alert (else the finding's own numbers)."""
    m = registry.get(f.metric)
    since = clock_text(f.window[0])
    where = DR.segment_text(f.segment)
    n = numbers or dict(n=f.observed["n"], exp=f.expected["n"], value=f.observed["value"], exp_value=f.expected["value"])
    if m.denominator:
        l1 = (f"{m.name} is {DR.fmt_value(n['value'], m.unit)} in {where} against {DR.fmt_value(n['exp_value'], m.unit)} "
              f"expected, since {since}.")
    else:
        pct = abs(n["n"] - n["exp"]) / max(n["exp"], 1e-9) * 100
        verb = "fell" if f.direction == "down" else "rose"
        l1 = f"{m.name} {verb} {pct:.0f}% in {where} since {since} ({n['n']:,.0f} against {n['exp']:,.0f} expected)."
    carry = min(f.explained, 1.0) * 100
    if who == "platform":
        tail = f" Lenders affected: {', '.join(lenders)}." if lenders else ""
        if f.segment != f.parent:
            l2 = f"Driver: {where} (carries {carry:.0f}% of the deviation in {DR.segment_text(f.parent)}; the rest of it is back to normal).{tail}"
        else:
            l2 = f"Driver: {where}; the rest of the book is normal.{tail}" if f.counterfactual_ok else f"Driver: {where}.{tail}"
    else:
        l2 = f"Driver: {where}. This is the part of your book that is affected; the figures above are yours only."
    return l1, l2


def link_for(incident_id):
    return f"/command/incidents/{incident_id}"


def audience(metric_id, lenders):
    """(lender, role) pairs that should hear about it: leadership and the head whose metrics include it, for every affected
    lender, and the RM and lender user of each affected lender."""
    out = []
    heads = [r["role_id"] for r in RL.ROLES if r["role_id"] in ("ops_head", "delivery_head")
             and (r["allowed_metrics"] == "*" or metric_id in r["allowed_metrics"])]
    for L in lenders:
        for role in ["cn_leadership"] + heads + ["lender_rm", "lender_user"]:
            out.append((L, role))
    return out


def affected_lenders(inv, segment, day):
    """Lenders that hold a real part of the segment (so a lender-specific segment names only that lender)."""
    if "lender" in segment:
        return [segment["lender"]]
    d = max(day - 1, 0)
    rows = inv.f.sums("events_total", d * 96, d * 96 + 95, segment, ("lender",))
    tot = rows["n"].sum()
    if tot <= 0:
        return []
    return [r.lender for r in rows.itertuples() if r.n / tot >= MIN_LENDER_SHARE]
