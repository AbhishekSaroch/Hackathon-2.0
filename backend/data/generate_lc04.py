"""
LC-04 synthetic data, PUBLIC part. Run:  python backend/data/generate_lc04.py [--accounts 10000] [--seed 42]

Writes backend/data/csv_lc04/ :
  accounts.csv, d13.csv, calendar.csv
  events_public.csv        days 1-30, WITH 3 practice anomalies we tune on
  events_hidden_clean.csv  days 31-40, CLEAN (no anomalies). inject_hidden_lc04.py turns it into events_hidden.csv
  anomaly_key.csv          answer key for the 3 practice anomalies
  meta.json
Everything is synthetic and seeded. The 6 hidden anomalies are NOT defined here (separate script, separate seed).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lc04_world as W  # noqa: E402
from lc04_anomalies import apply_all  # noqa: E402

OUT = Path(__file__).resolve().parent / "csv_lc04"

# The 7 PRACTICE anomalies (public days): every one of the 6 kinds appears at least once so the detector is tuned on
# all of them, with different segments (and some milder strengths) than the hidden ones on purpose. All end by day 26,
# so none of their effects (PTP outcomes arrive up to 4 days later) reach the hidden days.
PRACTICE = [
    dict(id="P1", kind="dialer_outage", tenant=None, start="2026-10-15 11:00:00", end="2026-10-15 14:00:00",
         segment={"city": "Hyderabad", "dialer": "D3"}, params={"p_drop": .9}, note="practice: dialer outage"),
    dict(id="P2", kind="gateway_failure", tenant=None, start="2026-10-19 15:00:00", end="2026-10-19 18:00:00",
         segment={"gateway": "G4"}, params={"p_fail": .6}, note="practice: gateway failure"),
    dict(id="P3", kind="duplicate_events", tenant=None, start="2026-10-23 10:30:00", end="2026-10-23 13:30:00",
         segment={"agency": "A5"}, params={}, note="practice: duplicate events"),
    dict(id="P4", kind="agency_misdisposition", tenant=None, start="2026-10-14 10:00:00", end="2026-10-14 16:00:00",
         segment={"agency": "A2"}, params={"p_fake": .45, "p_flip": .5}, note="practice: agency mis-dispositioning"),
    dict(id="P5", kind="feed_stall", tenant="L3", start="2026-10-21 14:00:00", end="2026-10-21 16:00:00",
         segment={"lender": "L3"}, params={}, note="practice: lender feed stall"),
    dict(id="P6", kind="ptp_collapse", tenant="L2", start="2026-10-22 10:00:00", end="2026-10-22 19:00:00",
         segment={"product": "Two-Wheeler"}, params={"p_flip": .9}, note="practice: PTP kept collapse"),
    dict(id="P7", kind="dialer_outage", tenant=None, start="2026-10-09 12:00:00", end="2026-10-09 16:00:00",
         segment={"city": "Mumbai", "dialer": "D1"}, params={"p_drop": .6}, note="practice: partial dialer outage"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(a.seed)

    acct = W.make_accounts(a.accounts, rng)
    ev = W.simulate(acct, rng)
    print(f"clean stream: {len(ev):,} events, {a.accounts:,} accounts, {time.time() - t0:.0f}s")

    ev, key = apply_all(ev, PRACTICE, acct, np.random.default_rng(a.seed + 1))
    cut = W.PUBLIC_END_TS
    pub, hid = ev[ev["ts"] < cut], ev[ev["ts"] >= cut]

    acct.to_csv(out / "accounts.csv", index=False)
    pd.DataFrame(W.d13_rows()).to_csv(out / "d13.csv", index=False)
    pd.DataFrame(W.calendar_rows()).assign(tenant_id="GLOBAL").to_csv(out / "calendar.csv", index=False)
    W.write_events(pub, out / "events_public.csv")
    W.write_events(hid, out / "events_hidden_clean.csv")
    pd.DataFrame(key).to_csv(out / "anomaly_key.csv", index=False)
    (out / "meta.json").write_text(json.dumps({
        "seed": a.seed, "accounts": a.accounts, "stream_start": W.START.isoformat(), "days": W.N_DAYS,
        "public_days": W.PUBLIC_DAYS, "hidden_days": W.N_DAYS - W.PUBLIC_DAYS}, indent=2))

    print(f"public events {len(pub):,}  hidden (clean) {len(hid):,}")
    print(pub.groupby("event_type").size().to_string())
    print(f"done in {time.time() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
