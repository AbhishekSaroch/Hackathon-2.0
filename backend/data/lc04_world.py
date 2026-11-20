"""
LC-04 synthetic world: accounts, calendar and the CLEAN intraday event stream (40 days, no anomalies).
Used by generate_lc04.py (public world + 3 practice anomalies) and inject_hidden_lc04.py (6 hidden anomalies).
All knobs are at the top. Everything is seeded; nothing here knows about the detector.

Event chain: one contact attempt starts a chain (contact -> disposition -> ptp created -> ptp kept/broken -> payment).
`chain` and `src` are extra CSV columns (not loaded into the DB) so anomalies can be injected consistently.
"""
import numpy as np
import pandas as pd
from datetime import date, timedelta

# ------------------------------------------------------------------ time
START = date(2026, 9, 28)            # Monday. Day 1 = Sep 28, day 30 = Oct 27 (last public day), day 40 = Nov 6
N_DAYS = 40
PUBLIC_DAYS = 30
START_TS = pd.Timestamp(START)
END_TS = START_TS + pd.Timedelta(days=N_DAYS)
PUBLIC_END_TS = START_TS + pd.Timedelta(days=PUBLIC_DAYS)

# ------------------------------------------------------------------ dimensions (D13 is defined by us)
LENDERS = {"L1": .35, "L2": .25, "L3": .20, "L4": .12, "L5": .08}
# city: (weight, dialer a, dialer b, share of dialer a)
CITIES = {
    "Mumbai": (15, "D1", "D3", .60), "Delhi": (14, "D1", "D2", .50), "Bengaluru": (12, "D2", "D3", .60),
    "Pune": (10, "D1", "D2", .60), "Hyderabad": (9, "D1", "D3", .55), "Chennai": (8, "D2", "D3", .50),
    "Kolkata": (8, "D1", "D3", .70), "Ahmedabad": (6, "D2", "D3", .60), "Jaipur": (5, "D1", "D2", .50),
    "Lucknow": (5, "D1", "D3", .60), "Chandigarh": (4, "D2", "D3", .60), "Indore": (4, "D1", "D3", .50),
}
AGENCY_HOME = {"A1": ["Mumbai", "Pune"], "A2": ["Delhi", "Chandigarh"], "A3": ["Pune", "Ahmedabad"],
               "A4": ["Bengaluru", "Chennai"], "A5": ["Hyderabad"], "A6": ["Kolkata", "Lucknow"],
               "A7": ["Jaipur", "Indore"], "A8": []}                     # A8 works everywhere, a little
AGENCIES = list(AGENCY_HOME)
AGENCY_CONN = {"A1": 1.0, "A2": .95, "A3": 1.05, "A4": 1.0, "A5": .97, "A6": 1.03, "A7": .92, "A8": 1.0}
AGENCY_PTP = {"A1": 1.0, "A2": .9, "A3": 1.1, "A4": 1.0, "A5": 1.15, "A6": .85, "A7": 1.05, "A8": .95}
AGENCY_KEEP = {"A1": 1.0, "A2": .95, "A3": 1.05, "A4": 1.0, "A5": .9, "A6": 1.1, "A7": .92, "A8": 1.0}
DIALER_CONN = {"D1": 1.0, "D2": .97, "D3": 1.03}
GATEWAYS = {"G1": (.35, .965), "G2": (.30, .955), "G3": (.20, .93), "G4": (.15, .94)}   # share, success rate
PRODUCTS = {"Two-Wheeler": .40, "Personal Loan": .20, "Home Loan": .12, "Business Loan": .13, "Consumer Durable": .15}
BUCKETS = {"0": .55, "1-30": .20, "31-60": .10, "61-90": .08, "90+": .07}
DPD_RANGE = {"0": (0, 0), "1-30": (1, 30), "31-60": (31, 60), "61-90": (61, 90), "90+": (91, 240)}

# ------------------------------------------------------------------ behaviour knobs
CONTACT_RATE = {"0": .04, "1-30": 3.2, "31-60": 4.0, "61-90": 3.6, "90+": 2.4}   # attempts per account per day
CONN_BASE = .40                                                                  # connect probability
BUCKET_CONN = {"0": 1.0, "1-30": 1.1, "31-60": 1.0, "61-90": .9, "90+": .8}
FAIL_P = .02                                                                     # dialer glitch share
DISP_NAMES = ["rpc", "ptp", "refused", "dispute", "wrong_number", "callback"]
DISP_BASE = np.array([.30, .26, .15, .06, .05, .18])
BUCKET_PTP = {"0": 1.0, "1-30": 1.1, "31-60": 1.0, "61-90": .9, "90+": .7}
BUCKET_KEEP = {"0": .8, "1-30": .62, "31-60": .50, "61-90": .40, "90+": .28}
EMI_BASE = 1 / 30 / 1.22                 # bucket-0 accounts pay about once a month, more in the first days
SELF_PAY_RATE = .08                      # delinquent accounts paying on their own, per day
RETRY_P = .45                            # a failed payment is retried by the customer this often

# ------------------------------------------------------------------ calendar (seasonality the detector may know about)
FESTIVALS = [  # (day, city or '*', contact multiplier, payment multiplier, note)
    (date(2026, 10, 17), "Kolkata", .60, 1.0, "Durga Puja"),
    (date(2026, 10, 18), "Kolkata", .60, 1.0, "Durga Puja"),
    (date(2026, 10, 19), "Kolkata", .30, 1.0, "Durga Puja"),
    (date(2026, 10, 20), "Kolkata", .30, 1.0, "Durga Puja"),
    (date(2026, 10, 20), "*", .45, 1.25, "Dussehra"),
    (date(2026, 11, 6), "*", .75, 1.5, "Dhanteras"),
]


def month_end(d):                        # last 3 days of the month
    return (d + timedelta(days=3)).month != d.month


def contact_mult(city, d):
    m = .35 if d.weekday() == 6 else 1.0
    if month_end(d):
        m *= 1.3
    f = [cm for fd, fc, cm, _, _ in FESTIVALS if fd == d and fc in ("*", city)]
    return m * (min(f) if f else 1.0)


def pay_mult(d):
    m = .85 if d.weekday() == 6 else 1.0
    if month_end(d):
        m *= 1.15
    f = [pm for fd, fc, _, pm, _ in FESTIVALS if fd == d and fc == "*"]
    return m * (max(f) if f else 1.0)


def emi_factor(dom):                     # EMI payments cluster right after salary day
    return 3.0 if dom <= 5 else 1.3 if dom <= 10 else .75


def self_factor(dom):
    return 1.5 if dom <= 5 else 1.0


def calendar_rows():
    rows = []
    for i in range(N_DAYS):
        d = START + timedelta(days=i)
        if d.weekday() == 6:
            rows.append(dict(day=d.isoformat(), kind="weekend", city="*", note="Sunday"))
        if d.day <= 5:
            rows.append(dict(day=d.isoformat(), kind="salary_day", city="*", note="salary credit days (1st-5th)"))
        if month_end(d):
            rows.append(dict(day=d.isoformat(), kind="month_end", city="*", note="month-end push"))
        for fd, fc, _, _, note in FESTIVALS:
            if fd == d:
                rows.append(dict(day=d.isoformat(), kind="festival", city=fc, note=note))
    return rows


def _profile(hourly):
    p = np.repeat(np.array(hourly, float), 4)
    return p / p.sum()


CONTACT_PROFILE = _profile([0] * 9 + [.6, 1.3, 1.3, 1.0, .8, 1.1, 1.2, 1.2, 1.0, .9, .4] + [0] * 4)
PAY_PROFILE = _profile([.05, .03, .02, .02, .03, .05, .2, .4, .7, 1, 1.2, 1.2, 1.1, 1, 1, 1.1, 1.1, 1.2, 1.3, 1.4, 1.5, 1.3, .8, .3])

COLS = ["tenant_id", "account_id", "ts", "ingested_at", "event_type", "outcome", "channel", "agency_id", "dialer_id",
        "gateway_id", "amount", "city", "product", "bucket", "chain", "src"]


# ------------------------------------------------------------------ accounts
def make_accounts(n, rng):
    lender = rng.choice(list(LENDERS), n, p=list(LENDERS.values()))
    cw = np.array([v[0] for v in CITIES.values()], float)
    city = rng.choice(list(CITIES), n, p=cw / cw.sum())
    product = rng.choice(list(PRODUCTS), n, p=list(PRODUCTS.values()))
    bucket = rng.choice(list(BUCKETS), n, p=list(BUCKETS.values()))
    dpd = np.array([rng.integers(DPD_RANGE[b][0], DPD_RANGE[b][1] + 1) for b in bucket])
    loan = np.round(np.exp(rng.normal(np.log(400000), .6, n)), -3)
    emi = np.round(loan / rng.integers(18, 48, n), -1)
    outstanding = np.round(loan * rng.uniform(.3, .95, n), -2)
    agency = np.empty(n, dtype=object)
    for c in CITIES:                     # accounts are worked mostly by the agencies based in their city
        m = city == c
        cand = [a for a, h in AGENCY_HOME.items() if c in h] + ["A8"]
        w = np.array([1.0 if a != "A8" else .6 for a in cand])
        agency[m] = rng.choice(cand, m.sum(), p=w / w.sum())
    return pd.DataFrame({"account_id": [f"AC{i:07d}" for i in range(n)], "tenant_id": lender, "product": product,
                         "loan_amount": loan, "emi_amount": emi, "dpd": dpd, "bucket": bucket,
                         "outstanding": outstanding, "city": city, "agency_id": agency})


def d13_rows():
    rows = []
    for c in CITIES:
        rows.append(dict(tenant_id="GLOBAL", dimension="city", value_id=c, label=c, parent_id=None))
    for a in AGENCIES:
        rows.append(dict(tenant_id="GLOBAL", dimension="agency", value_id=a, label=f"Agency {a}", parent_id=None))
    for d in DIALER_CONN:
        rows.append(dict(tenant_id="GLOBAL", dimension="dialer", value_id=d, label=f"Dialer {d}", parent_id=None))
    for g in GATEWAYS:
        rows.append(dict(tenant_id="GLOBAL", dimension="gateway", value_id=g, label=f"Gateway {g}", parent_id=None))
    for p in PRODUCTS:
        rows.append(dict(tenant_id="GLOBAL", dimension="product", value_id=p, label=p, parent_id=None))
    for b in BUCKETS:
        rows.append(dict(tenant_id="GLOBAL", dimension="bucket", value_id=b, label=f"DPD {b}", parent_id=None))
    return rows


# ------------------------------------------------------------------ building blocks (also used by lc04_anomalies.py)
def secs(a):
    return pd.to_timedelta(np.asarray(a), unit="s")


def ingest_delay(n, rng):
    return secs(np.minimum(180, rng.exponential(25, n) + 2))


def fill_ingest(ev, rng):
    """Rows without ingested_at get ts + a few seconds (normal arrival delay)."""
    m = ev["ingested_at"].isna()
    if m.any():
        ev.loc[m, "ingested_at"] = ev.loc[m, "ts"] + ingest_delay(int(m.sum()), rng)
    return ev


def frame(base, ts, event_type, outcome, channel=None, dialer=None, gateway=None, amount=None, chain=None,
          src="", agency=True):
    base = base.reset_index(drop=True)
    return pd.DataFrame({
        "tenant_id": base["tenant_id"].values, "account_id": base["account_id"].values,
        "ts": pd.DatetimeIndex(ts), "ingested_at": pd.NaT, "event_type": event_type, "outcome": outcome,
        "channel": channel, "agency_id": base["agency_id"].values if agency else None, "dialer_id": dialer,
        "gateway_id": gateway, "amount": amount, "city": base["city"].values, "product": base["product"].values,
        "bucket": base["bucket"].values, "chain": chain, "src": src})[COLS]


def make_payments(base, ts, amount, chain, src, rng):
    """Payment attempts through a gateway; a failed attempt is retried by the customer some of the time."""
    n = len(base)
    if n == 0:
        return pd.DataFrame(columns=COLS)
    ts = pd.DatetimeIndex(ts)
    ids = list(GATEWAYS)
    gw = rng.choice(ids, n, p=[GATEWAYS[g][0] for g in ids])
    succ = np.array([GATEWAYS[g][1] for g in gw])
    ok = rng.random(n) < succ
    out = [frame(base, ts, "payment", np.where(ok, "success", "failed"), gateway=gw, amount=amount, chain=chain,
                 src=src, agency=False)]
    retry = (~ok) & (rng.random(n) < RETRY_P)
    if retry.any():
        k = int(retry.sum())
        b2 = base.reset_index(drop=True)[retry]
        gw2 = rng.choice(ids, k, p=[GATEWAYS[g][0] for g in ids])
        ok2 = rng.random(k) < np.array([GATEWAYS[g][1] for g in gw2])
        out.append(frame(b2, ts[retry] + secs(rng.integers(300, 3600, k)), "payment", np.where(ok2, "success", "failed"),
                         gateway=gw2, amount=np.asarray(amount)[retry], chain=np.asarray(chain)[retry], src="retry",
                         agency=False))
    return pd.concat(out, ignore_index=True)


def resolve_ptps(created, rng, keep_p=None):
    """A PTP is kept or broken 1-4 days later. Kept PTPs come with a payment. keep_p overrides the normal keep rate."""
    n = len(created)
    if n == 0:
        return pd.DataFrame(columns=COLS)
    created = created.reset_index(drop=True)
    off = rng.integers(1, 5, n)
    pday = pd.DatetimeIndex(created["ts"]).normalize() + pd.to_timedelta(off, unit="D")
    if keep_p is None:
        p = (created["bucket"].map(BUCKET_KEEP).values * created["agency_id"].map(AGENCY_KEEP).values
             * np.where(pday.day <= 5, 1.2, 1.0) * np.where(pday.weekday == 6, .85, 1.0))
        p = np.clip(p, 0, .92)
    else:
        p = np.full(n, float(keep_p))
    kept = rng.random(n) < p
    sec = np.where(kept, rng.integers(9 * 3600, 20 * 3600, n), rng.integers(19 * 3600 + 1800, 20 * 3600 + 4500, n))
    res_ts = pday + secs(sec)
    ok = np.asarray(res_ts < END_TS)
    c = created[ok].reset_index(drop=True)
    res = frame(c, res_ts[ok], "ptp", np.where(kept, "kept", "broken")[ok], amount=c["amount"].values,
                chain=c["chain"].values, src="ptp_res")
    kp = ok & kept
    pays = make_payments(created[kp], res_ts[kp] + secs(rng.integers(30, 300, int(kp.sum()))),
                         created.loc[kp, "amount"].values, created.loc[kp, "chain"].values, "ptp_pay", rng)
    return pd.concat([res, pays], ignore_index=True)


# ------------------------------------------------------------------ the clean stream
def simulate(acct, rng, days=N_DAYS):
    """days < N_DAYS is only for the 100k-account speed test (a short stream is enough to time a refresh)."""
    n = len(acct)
    cities = list(CITIES)
    c_idx = acct["city"].map({c: i for i, c in enumerate(cities)}).values
    b_names = list(BUCKETS)
    b_idx = acct["bucket"].map({b: i for i, b in enumerate(b_names)}).values
    crate = np.array([CONTACT_RATE[b] for b in b_names])
    DA = np.array([CITIES[c][1] for c in cities]); DB = np.array([CITIES[c][2] for c in cities])
    PA = np.array([CITIES[c][3] for c in cities])
    chain_no = 0
    parts, ptp_created = [], []
    for d in range(days):
        day = START + timedelta(days=d)
        day_ts = START_TS + pd.Timedelta(days=d)
        # ---- contacts, dispositions, PTPs created
        cm = np.array([contact_mult(c, day) for c in cities])
        k_i = rng.poisson(crate[b_idx] * cm[c_idx])
        idx = np.repeat(np.arange(n), k_i)
        k = len(idx)
        if k:
            base = acct.iloc[idx].reset_index(drop=True)
            slot = rng.choice(96, k, p=CONTACT_PROFILE)
            ts = day_ts + secs(slot * 900 + rng.integers(0, 900, k))
            ci = c_idx[idx]
            dialer = np.where(rng.random(k) < PA[ci], DA[ci], DB[ci])
            hour = slot // 4
            slotf = np.where(hour <= 10, .85, np.where(hour >= 17, 1.15, 1.0))
            p = (CONN_BASE * base["agency_id"].map(AGENCY_CONN).values * pd.Series(dialer).map(DIALER_CONN).values
                 * slotf * base["bucket"].map(BUCKET_CONN).values)
            failed = rng.random(k) < FAIL_P
            conn = (~failed) & (rng.random(k) < np.clip(p, 0, .85))
            outcome = np.where(failed, "failed", np.where(conn, "connected", "no_answer"))
            chain = np.arange(chain_no, chain_no + k); chain_no += k
            parts.append(frame(base, ts, "contact", outcome, channel="call", dialer=dialer, chain=chain, src="contact"))
            if conn.any():
                cb = base[conn].reset_index(drop=True)
                kc = int(conn.sum())
                dts = pd.DatetimeIndex(ts)[conn] + secs(rng.integers(30, 300, kc))
                P = np.tile(DISP_BASE, (kc, 1))
                P[:, 1] *= cb["agency_id"].map(AGENCY_PTP).values * cb["bucket"].map(BUCKET_PTP).values
                P /= P.sum(1, keepdims=True)
                kind = np.minimum((rng.random(kc)[:, None] > np.cumsum(P, 1)).sum(1), 5)
                disp = np.array(DISP_NAMES)[kind]
                dl = dialer[conn]
                parts.append(frame(cb, dts, "disposition", disp, dialer=dl, chain=chain[conn], src="dispo"))
                pm = kind == 1
                if pm.any():
                    pb = cb[pm].reset_index(drop=True)
                    amt = np.round(acct.iloc[idx[conn][pm]]["emi_amount"].values * rng.uniform(.5, 1.2, int(pm.sum())), -1)
                    created = frame(pb, dts[pm] + secs(rng.integers(5, 40, int(pm.sum()))), "ptp", "created",
                                    amount=amt, chain=chain[conn][pm], src="ptp_new")
                    parts.append(created)
                    ptp_created.append(created)
        # ---- payments (EMI payers and delinquent accounts paying on their own)
        pmult = pay_mult(day)
        zero = (b_idx == 0)
        lam = np.where(zero, EMI_BASE * emi_factor(day.day), SELF_PAY_RATE * self_factor(day.day)) * pmult
        k_p = rng.poisson(lam)
        pidx = np.repeat(np.arange(n), k_p)
        kp = len(pidx)
        if kp:
            base = acct.iloc[pidx].reset_index(drop=True)
            ts = day_ts + secs(rng.choice(96, kp, p=PAY_PROFILE) * 900 + rng.integers(0, 900, kp))
            emi = base["emi_amount"].values
            amt = np.where(base["bucket"].values == "0", emi, np.round(emi * rng.uniform(.3, 1.0, kp), -1))
            chain = np.arange(chain_no, chain_no + kp); chain_no += kp
            parts.append(make_payments(base, ts, amt, chain, "self_pay", rng))
    # ---- PTP outcomes (kept / broken) and their payments
    if ptp_created:
        parts.append(resolve_ptps(pd.concat(ptp_created, ignore_index=True), rng))
    ev = pd.concat(parts, ignore_index=True)
    ev = ev[ev["ts"] < END_TS]
    ev["amount"] = ev["amount"].astype(float)
    ev = fill_ingest(ev, rng)
    return ev.sort_values("ts", kind="stable").reset_index(drop=True)


def write_events(ev, path):
    out = ev.copy()
    for c in ("ts", "ingested_at"):
        out[c] = pd.to_datetime(out[c]).dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(path, index=False)


def read_events(path):
    return pd.read_csv(path, parse_dates=["ts", "ingested_at"], keep_default_na=False,
                       na_values=[""], dtype={"agency_id": "object", "dialer_id": "object", "gateway_id": "object",
                                              "channel": "object", "outcome": "object", "src": "object"})
