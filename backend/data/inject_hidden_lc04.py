"""
LC-04: seed the 6 HIDDEN anomalies into days 31-40. Run after generate_lc04.py:  python backend/data/inject_hidden_lc04.py

Reads  csv_lc04/events_hidden_clean.csv  (clean days 31-40)
Writes csv_lc04/events_hidden.csv and csv_lc04/anomaly_key_hidden.csv  (the answer key, evaluator only).
Own seed (777). The detector, the tuning and the public tests never import or read this file's output
until the single hidden run (step 12). Segments here differ from the practice anomalies in generate_lc04.py.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lc04_world as W  # noqa: E402
from lc04_anomalies import apply_all  # noqa: E402

OUT = Path(__file__).resolve().parent / "csv_lc04"
SEED = 777

HIDDEN = [
    dict(id="H1", kind="dialer_outage", tenant=None, start="2026-10-28 10:30:00", end="2026-10-28 13:00:00",
         segment={"city": "Pune", "dialer": "D2"}, params={"p_drop": .9}, note="city dialer outage"),
    dict(id="H2", kind="agency_misdisposition", tenant=None, start="2026-10-29 09:30:00", end="2026-10-29 17:30:00",
         segment={"agency": "A3"}, params={"p_fake": .45, "p_flip": .5}, note="agency mis-dispositioning"),
    dict(id="H3", kind="gateway_failure", tenant=None, start="2026-10-30 14:00:00", end="2026-10-30 17:00:00",
         segment={"gateway": "G2"}, params={"p_fail": .5}, note="payment gateway failure"),
    dict(id="H4", kind="feed_stall", tenant="L4", start="2026-11-02 11:00:00", end="2026-11-02 13:15:00",
         segment={"lender": "L4"}, params={}, note="lender feed stalls, then catches up"),
    dict(id="H5", kind="ptp_collapse", tenant="L1", start="2026-11-03 10:00:00", end="2026-11-03 19:00:00",
         segment={"product": "Two-Wheeler"}, params={"p_flip": .9}, note="PTP kept collapse in one lender + product"),
    dict(id="H6", kind="duplicate_events", tenant=None, start="2026-11-04 12:00:00", end="2026-11-04 15:00:00",
         segment={"agency": "A6"}, params={}, note="duplicate contact events from one agency"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    out = Path(a.out)
    clean_path = out / "events_hidden_clean.csv"
    if not clean_path.exists():
        sys.exit("run generate_lc04.py first")
    acct = pd.read_csv(out / "accounts.csv")
    ev = W.read_events(clean_path)
    ev, key = apply_all(ev, HIDDEN, acct, np.random.default_rng(SEED))
    W.write_events(ev, out / "events_hidden.csv")
    pd.DataFrame(key).to_csv(out / "anomaly_key_hidden.csv", index=False)
    print(f"hidden events {len(ev):,} (clean had {len(W.read_events(clean_path)):,}); {len(key)} anomalies seeded")


if __name__ == "__main__":
    main()
