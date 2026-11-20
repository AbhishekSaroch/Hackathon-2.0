"""
LC-04 anomaly engine: turns a CLEAN event stream into one with seeded anomalies.
Shared by generate_lc04.py (3 practice anomalies in the public days) and inject_hidden_lc04.py (6 hidden ones).
The DETECTOR never imports this file. It only sees the resulting events.

A spec is a dict: id, kind, tenant (lender or None = spans lenders), start, end, segment (dict), params (dict).
Six kinds: dialer_outage, gateway_failure, agency_misdisposition, feed_stall, ptp_collapse, duplicate_events.
"""
import json

import numpy as np
import pandas as pd

from lc04_world import CITIES, GATEWAYS, frame, make_payments, resolve_ptps, secs, fill_ingest  # noqa: F401

KIND_METRIC = {  # the governed metric (metrics/registry.py) that shows each kind most clearly
    "dialer_outage": "contacts_attempted", "gateway_failure": "pay_success_rate",
    "agency_misdisposition": "ptp_capture_rate", "feed_stall": "events_total",
    "ptp_collapse": "ptp_kept", "duplicate_events": "contacts_attempted"}


def _win(ev, s, e):
    return (ev["ts"] >= s) & (ev["ts"] < e)


def _chains(ctx, n):
    out = np.arange(ctx["next_chain"], ctx["next_chain"] + n)
    ctx["next_chain"] += n
    return out


def _clone(rows, **kw):
    df = rows.copy()
    for k, v in kw.items():
        df[k] = v
    df["ingested_at"] = pd.NaT
    return df


def dialer_outage(ev, sp, ctx, rng):
    """A dialer stops placing calls in one city: most attempts vanish, a few fail, some spill to the sibling dialer."""
    s, e, seg, pr = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"]), sp["segment"], sp["params"]
    m = _win(ev, s, e) & (ev["src"] == "contact") & (ev["city"] == seg["city"]) & (ev["dialer_id"] == seg["dialer"])
    hit = ev[m]
    r = rng.random(len(hit))
    drop, surv = hit.index[r < pr["p_drop"]], hit.index[r >= pr["p_drop"]]
    down = ev["chain"].isin(hit["chain"]) & (ev["src"] != "contact")        # their dispositions / PTPs go too
    a, b, _ = CITIES[seg["city"]][1:4]
    sibling = b if seg["dialer"] == a else a
    spill = hit.loc[drop][rng.random(len(drop)) < pr.get("p_spill", .2)]
    new = _clone(spill, ts=spill["ts"] + secs(rng.integers(900, 2700, len(spill))), dialer_id=sibling,
                 outcome="no_answer", src="retry_dial")
    new["chain"] = _chains(ctx, len(new))
    ev = ev[~(ev.index.isin(drop) | down)].copy()
    ev.loc[surv, "outcome"] = "failed"
    return pd.concat([ev, new], ignore_index=True)


def gateway_failure(ev, sp, ctx, rng):
    """One payment gateway starts failing: successful payments turn into failures, some customers retry elsewhere."""
    s, e, g, pr = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"]), sp["segment"]["gateway"], sp["params"]
    m = _win(ev, s, e) & (ev["event_type"] == "payment") & (ev["gateway_id"] == g) & (ev["outcome"] == "success")
    hit = ev[m]
    flip = hit.index[rng.random(len(hit)) < pr["p_fail"]]
    ev = ev.copy()
    ev.loc[flip, "outcome"] = "failed"
    rt = hit.loc[flip][rng.random(len(flip)) < .35]
    others = [x for x in GATEWAYS if x != g]
    new = _clone(rt, ts=rt["ts"] + secs(rng.integers(600, 2400, len(rt))), gateway_id=rng.choice(others, len(rt)),
                 outcome=np.where(rng.random(len(rt)) < .95, "success", "failed"), src="retry")
    return pd.concat([ev, new], ignore_index=True)


def agency_misdisposition(ev, sp, ctx, rng):
    """One agency records PTP / RPC dispositions that did not happen. PTP capture rises, kept rate falls later."""
    s, e, ag, pr = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"]), sp["segment"]["agency"], sp["params"]
    emi = ctx["emi"]
    w = _win(ev, s, e)
    # (a) calls nobody answered get a disposition anyway
    na = ev[w & (ev["src"] == "contact") & (ev["agency_id"] == ag) & (ev["outcome"] == "no_answer")]
    na = na[rng.random(len(na)) < pr["p_fake"]]
    dts = na["ts"] + secs(rng.integers(60, 180, len(na)))
    fake_ptp = rng.random(len(na)) < .7
    disp = _clone(na, event_type="disposition", ts=dts, outcome=np.where(fake_ptp, "ptp", "rpc"), channel=None,
                  src="dispo_fake")
    created = _clone(na[fake_ptp], event_type="ptp", outcome="created", channel=None, src="ptp_new_fake",
                     ts=dts[fake_ptp] + secs(rng.integers(5, 40, int(fake_ptp.sum()))),
                     amount=np.round(na[fake_ptp]["account_id"].map(emi).values * rng.uniform(.5, 1.2, int(fake_ptp.sum())), -1))
    # (b) real "refused / callback" calls are re-labelled as PTP
    rf = ev[w & (ev["src"] == "dispo") & (ev["agency_id"] == ag) & ev["outcome"].isin(["refused", "callback"])]
    flip = rf.index[rng.random(len(rf)) < pr["p_flip"]]
    ev = ev.copy()
    ev.loc[flip, "outcome"] = "ptp"
    fl = ev.loc[flip]
    created2 = _clone(fl, event_type="ptp", outcome="created", src="ptp_new_fake",
                      ts=fl["ts"] + secs(rng.integers(5, 40, len(fl))),
                      amount=np.round(fl["account_id"].map(emi).values * rng.uniform(.5, 1.2, len(fl)), -1))
    cr = pd.concat([created, created2], ignore_index=True)
    cr = fill_ingest(cr, rng)
    res = resolve_ptps(cr, rng, keep_p=pr.get("fake_keep", .05))         # fake promises are almost never kept
    return pd.concat([ev, disp, cr, res], ignore_index=True)


def feed_stall(ev, sp, ctx, rng):
    """One lender's feed stops delivering; everything arrives in a burst shortly after the stall ends."""
    s, e = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"])
    m = _win(ev, s, e) & (ev["tenant_id"] == sp["tenant"])
    ev = ev.copy()
    ev.loc[m, "ingested_at"] = e + secs(rng.integers(60, 1500, int(m.sum())))
    return ev


def ptp_collapse(ev, sp, ctx, rng):
    """In one lender + product, kept promises turn into broken promises and their payments disappear."""
    s, e, pr = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"]), sp["params"]
    m = (_win(ev, s, e) & (ev["src"] == "ptp_res") & (ev["outcome"] == "kept") & (ev["tenant_id"] == sp["tenant"])
         & (ev["product"] == sp["segment"]["product"]))
    hit = ev[m]
    flip = hit.index[rng.random(len(hit)) < pr["p_flip"]]
    chains = ev.loc[flip, "chain"]
    ev = ev.copy()
    ev.loc[flip, "outcome"] = "broken"
    ev.loc[flip, "ts"] = ev.loc[flip, "ts"].dt.normalize() + secs(rng.integers(19 * 3600 + 1800, 20 * 3600 + 4500, len(flip)))
    ev.loc[flip, "ingested_at"] = pd.NaT
    ev = ev[~(ev["chain"].isin(chains) & ev["src"].isin(["ptp_pay", "retry"]) & (ev["event_type"] == "payment"))]
    return ev


def duplicate_events(ev, sp, ctx, rng):
    """One agency's contact events are logged twice."""
    s, e, ag = pd.Timestamp(sp["start"]), pd.Timestamp(sp["end"]), sp["segment"]["agency"]
    m = _win(ev, s, e) & (ev["src"] == "contact") & (ev["agency_id"] == ag)
    src = ev[m]
    new = _clone(src, ts=src["ts"] + secs(rng.integers(0, 20, len(src))), src="dup")
    new["chain"] = _chains(ctx, len(new))
    return pd.concat([ev, new], ignore_index=True)


KINDS = {f.__name__: f for f in (dialer_outage, gateway_failure, agency_misdisposition, feed_stall, ptp_collapse,
                                 duplicate_events)}


def apply_all(ev, specs, acct, rng):
    """Apply the specs in order. Returns (events, key rows). New rows get a normal arrival delay."""
    ctx = {"emi": acct.set_index("account_id")["emi_amount"], "next_chain": int(ev["chain"].max()) + 1_000_000}
    key = []
    for sp in specs:
        ev = KINDS[sp["kind"]](ev, sp, ctx, rng)
        ev = fill_ingest(ev.reset_index(drop=True), rng)
        key.append(dict(anomaly_id=sp["id"], tenant_id=sp.get("tenant") or "GLOBAL", kind=sp["kind"],
                        start_ts=sp["start"], end_ts=sp["end"], metric_id=KIND_METRIC[sp["kind"]],
                        segment=json.dumps(sp["segment"]), note=sp.get("note", "")))
    return ev.sort_values("ts", kind="stable").reset_index(drop=True), key
