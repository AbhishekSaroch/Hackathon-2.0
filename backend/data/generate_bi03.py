"""
Synthetic BI-03 data (pre-delinquency early warning) -> CSV files. No tenant_id column: the loader adds it.

  python generate_bi03.py [--n 6000] [--seed 42] [--out <dir>]

Writes to csv_bi03/:  bp_accounts, events, bureau_snapshots, outcome_labels, outcome_labels_hidden,
                      service_templates, bp_meta

Timeline (all dates synthetic):
  12 months of history (the brief's D8)  ->  stream day 1..90 = PUBLIC  ->  day 91..150 = HIDDEN
  Public outcome labels cover the public period only. Hidden labels are for OUR OWN evaluation and
  live in a separate table that training code must never read.

How the data is made realistic (this is what makes the backtest honest):
  - Base rate is low: about 5.5% of accounts miss an EMI during the 150 days.
  - Defaulters show weak, noisy distress 25-65 days before the miss: late salary credits, bounces on
    other loans, rising card utilisation, bursts of new enquiries, slightly late EMI payments.
  - 15% of defaulters give NO warning at all (sudden shock), so recall can never be perfect.
  - Healthy accounts also show these signals (hard negatives): one-off late salary, a card spike,
    an enquiry burst, a single bounce, and 3.5% "recoverers" who look fully distressed and then recover.
  - Every event has available_from >= event_date (posting / bureau lag). Features must use available_from.
  - An account's events stop after its first missed EMI (it is no longer a "current" account).
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "csv_bi03"

STREAM_START = date(2026, 5, 1)
PUBLIC_DAYS, HIDDEN_DAYS = 90, 60
STREAM_END = STREAM_START + timedelta(days=PUBLIC_DAYS + HIDDEN_DAYS - 1)
PUBLIC_END = STREAM_START + timedelta(days=PUBLIC_DAYS - 1)
HISTORY_MONTHS = 12

# ---- knobs (tune here, then re-run the diagnostic) ----
DEFAULT_RATE = 0.055        # share of accounts that miss an EMI during the stream
SUDDEN_SHARE = 0.12         # defaulters with no warning signs at all
RECOVER_RATE = 0.025        # healthy accounts that look distressed, then recover
HN_SALARY, HN_UTIL, HN_ENQ, HN_BOUNCE = 0.08, 0.06, 0.06, 0.02   # one-off noise on healthy accounts
LEAD_RANGE = (30, 75)       # days between first distress and the missed EMI
RAMP_SHARE = 0.6            # distress reaches full strength this far into the lead time, then stays
PROFILES = [("salary_delay", .35), ("bounce", .25), ("card_stress", .20), ("enquiries", .10), ("mixed", .10)]
PROFILE_SIGNALS = {
    "salary_delay": {"salary": 1.0},
    "bounce": {"bounce": 1.0},
    "card_stress": {"util": 1.0, "enq": 0.3},
    "enquiries": {"enq": 1.0, "util": 0.3},
    "mixed": {"salary": .5, "bounce": .5, "util": .5, "enq": .5},
}

FIRST = ["Rahul", "Amit", "Neha", "Priya", "Vikram", "Karan", "Sana", "Rohit", "Anjali", "Mohd", "Rajesh", "Asha",
         "Mayur", "Shreya", "Deepak", "Pooja", "Suresh", "Kavita", "Imran", "Lakshmi", "Arjun", "Meena"]
LAST = ["Sharma", "Singh", "Gupta", "Verma", "Joshi", "Mehta", "Khan", "Das", "Rao", "Patel", "Saxena", "Iyer",
        "Nair", "Reddy", "Kapoor", "Shaikh", "Thakur", "Pillai", "Bose", "Malhotra"]
PRODUCTS = ["Home Loan", "Personal Loan", "Business Loan", "Vehicle Loan"]
BRANCHES = ["Mumbai", "Pune", "Chennai", "Delhi", "Bengaluru", "Hyderabad", "Kolkata", "Ahmedabad"]
EMPLOYERS = ["ACME TECH PVT LTD", "SUNRISE LOGISTICS", "BLUEWAVE SERVICES", "METRO RETAIL LTD", "GREENFIELD INFRA", "NOVA PHARMA"]
MERCHANTS = ["SWIGGY", "ZOMATO", "BIGBASKET", "DMART", "JIO RECHARGE", "IRCTC", "UBER", "AIRTEL"]

# D11: approved service templates (no dues words, no amounts, no urgency). Placeholders are filled by code.
TEMPLATES = [
    ("T-EDC-H1", "emi_date_change", "hinglish", "Hi {name}, humne dekha ki aapki salary date badal sakti hai. Kya aap apni EMI date {new_date} tareekh pe shift karna chahenge? Reply YES, hum aapke liye set kar denge.", "name,new_date"),
    ("T-EDC-H2", "emi_date_change", "hinglish", "Namaste {name}, agar aapki salary der se aa rahi hai toh hum EMI date badal sakte hain. {new_date} tareekh theek rahegi? Reply YES.", "name,new_date"),
    ("T-EDC-E1", "emi_date_change", "english", "Hi {name}, we noticed your salary date may have changed. Would you like to move your EMI date to the {new_date}? Reply YES and we will set it up for you.", "name,new_date"),
    ("T-EDC-E2", "emi_date_change", "english", "Hello {name}, if your salary is arriving later, we can change your EMI date. Would the {new_date} suit you? Reply YES.", "name,new_date"),
    ("T-APY-H1", "autopay_setup", "hinglish", "Hi {name}, EMI date yaad rakhne ki chinta chhodiye! 2 minute me auto-pay set kar lein. Madad chahiye toh HELP likhein.", "name"),
    ("T-APY-H2", "autopay_setup", "hinglish", "Namaste {name}, auto-pay se aapki EMI apne aap ho jayegi, koi tension nahi. App me ya HELP likh ke set kar sakte hain.", "name"),
    ("T-APY-E1", "autopay_setup", "english", "Hi {name}, never worry about your EMI date again. Set up auto-pay in 2 minutes. Reply HELP if you would like a hand.", "name"),
    ("T-APY-E2", "autopay_setup", "english", "Hello {name}, with auto-pay your EMI is taken care of automatically. You can set it up in the app or reply HELP.", "name"),
    ("T-REM-H1", "reminder", "hinglish", "Hi {name}, ek friendly yaad: aapki agli EMI {emi_day} tareekh ko hai. App se kabhi bhi bhar sakte hain. Madad ke liye HELP likhein.", "name,emi_day"),
    ("T-REM-H2", "reminder", "hinglish", "Namaste {name}, agli EMI {emi_day} tareekh ko aayegi. Aap app me aasaani se bhar sakte hain. Koi sawaal ho toh HELP likhein.", "name,emi_day"),
    ("T-REM-E1", "reminder", "english", "Hi {name}, a friendly heads-up: your next EMI date is the {emi_day}. You can pay anytime in the app. Reply HELP if you need anything.", "name,emi_day"),
    ("T-REM-E2", "reminder", "english", "Hello {name}, your next EMI falls on the {emi_day}. You can pay easily in the app. Reply HELP for any question.", "name,emi_day"),
]


def add_months(d: date, k: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + k, 12)
    return date(y, m + 1, min(d.day, 28))


def month_starts(first: date, last: date):
    d = first.replace(day=1)
    while d <= last:
        yield d
        d = add_months(d, 1)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def generate_account(r: random.Random, acct: str, w_events, w_bureau) -> dict:
    """Write one account's events and bureau snapshots. Returns facts used for outcome labels and stats."""
    salary = r.randrange(25000, 90001, 500)
    emi = max(1000, round(salary * r.uniform(0.12, 0.30), -2))
    sal_day, due_day = r.randint(1, 7), r.randint(8, 28)
    emp = r.choice(EMPLOYERS)
    base_util, open_loans, base_score = r.uniform(0.12, 0.55), r.randint(1, 4), r.randint(640, 800)
    hist_start = add_months(STREAM_START.replace(day=1), -HISTORY_MONTHS)

    # ---- who is this account? ----
    kind, profile, sec_profile, onset, peak, miss_due = "healthy", None, None, None, None, None
    dues_in_stream = [date(m.year, m.month, due_day) for m in month_starts(STREAM_START, STREAM_END)]
    dues_in_stream = [d for d in dues_in_stream if STREAM_START + timedelta(days=20) <= d < STREAM_END]
    roll = r.random()
    if roll < DEFAULT_RATE:
        kind = "defaulter"
        miss_due = peak = r.choice(dues_in_stream)
        if r.random() >= SUDDEN_SHARE:
            profile = r.choices([p for p, _ in PROFILES], [w for _, w in PROFILES])[0]
            onset = peak - timedelta(days=r.randint(*LEAD_RANGE))
            if r.random() < 0.35:
                sec_profile = r.choice([p for p, _ in PROFILES if p != profile])
        else:
            kind = "defaulter_sudden"
    elif roll < DEFAULT_RATE + RECOVER_RATE:
        kind = "recoverer"
        peak = r.choice(dues_in_stream)
        profile = r.choices([p for p, _ in PROFILES], [w for _, w in PROFILES])[0]
        onset = peak - timedelta(days=r.randint(*LEAD_RANGE))
    stop_date = miss_due                                   # defaulters leave the "current" universe at the miss

    weights = {}
    for prof, w in ((profile, 1.0), (sec_profile, 0.4)):
        for sig, v in PROFILE_SIGNALS.get(prof, {}).items():
            weights[sig] = weights.get(sig, 0.0) + w * v

    def intensity(d: date) -> float:
        if onset is None or d < onset:
            return 0.0
        if d >= peak:
            return 1.0 if kind == "defaulter" else 0.0     # recoverers drop back to normal after the peak
        return min(1.0, (d - onset).days / max(RAMP_SHARE * (peak - onset).days, 1))

    def strength(sig: str, d: date) -> float:
        return clamp(weights.get(sig, 0.0) * intensity(d), 0.0, 1.0)

    # ---- one-off noise on healthy-looking accounts (hard negatives) ----
    healthy_like = kind in ("healthy", "recoverer", "defaulter_sudden")
    hn_salary_months = set()
    if healthy_like and r.random() < HN_SALARY:
        for _ in range(r.randint(1, 2)):
            hn_salary_months.add(add_months(STREAM_START.replace(day=1), r.randint(0, 4)))
    hn_util_month = add_months(STREAM_START.replace(day=1), r.randint(0, 4)) if healthy_like and r.random() < HN_UTIL else None

    stm, other = [], []                                     # statement txns, other events

    # ---- bounce and enquiry events ----
    bounce_dates = []
    wb = weights.get("bounce", 0.0)
    if onset is not None and wb > 0:
        n = r.randint(1, 3) if wb >= 1 else (1 if r.random() < wb else 0)
        span = max((peak - onset).days - 8, 1)
        bounce_dates = [onset + timedelta(days=5 + r.randint(0, span)) for _ in range(n)]
    if healthy_like and r.random() < HN_BOUNCE:
        bounce_dates.append(STREAM_START + timedelta(days=r.randint(5, 120)))
    for d in bounce_dates:
        other.append((d, r.randint(1, 3), "bureau", "other_loan_bounce", round(r.uniform(0.2, 1.0) * emi, -2), None, None, "OTHER LOAN EMI BOUNCE"))

    enq_dates = []
    for m0 in month_starts(hist_start, STREAM_END):
        n = (1 if r.random() < 0.22 else 0) + (1 if r.random() < 0.03 else 0)
        mid = m0 + timedelta(days=14)
        n += round(strength("enq", mid) * r.uniform(2, 4))
        enq_dates += [m0 + timedelta(days=r.randint(0, 27)) for _ in range(n)]
    if healthy_like and r.random() < HN_ENQ:
        burst_start = STREAM_START + timedelta(days=r.randint(0, 110))
        enq_dates += [burst_start + timedelta(days=r.randint(0, 20)) for _ in range(r.randint(3, 5))]
    for d in enq_dates:
        other.append((d, r.randint(1, 3), "bureau", "new_enquiry", None, 1, None, "NEW CREDIT ENQUIRY"))

    # ---- monthly cycle: salary, EMI, spends, bureau snapshot ----
    for m0 in month_starts(hist_start, STREAM_END):
        mid = m0 + timedelta(days=14)
        # salary credit (the late-salary signal)
        delay = r.choice([-1, 0, 0, 0, 1])
        delay += round(strength("salary", m0 + timedelta(days=sal_day)) * r.uniform(5, 12))
        if m0 in hn_salary_months:
            delay += r.randint(4, 8)
        sal_date = date(m0.year, m0.month, sal_day) + timedelta(days=delay)
        amount = salary * (r.uniform(0.6, 0.9) if strength("salary", sal_date) > 0.6 and r.random() < 0.3 else 1.0)
        stm.append((sal_date, r.choice([0, 0, 1]), "credit", round(amount), f"SALARY CREDIT {emp}"))

        # EMI due / paid
        due = date(m0.year, m0.month, due_day)
        x = intensity(due)
        other.append((due, 0, "repayment", "emi_due", emi, None, None, None))
        if not (miss_due is not None and due == miss_due):      # the missed EMI is simply not paid
            late = r.randint(1, 4) if (x > 0.3 and r.random() < 0.6 * x) else 0
            paid = due + timedelta(days=late)
            other.append((paid, 0, "repayment", "emi_paid", emi, float(late), None, None))
            stm.append((paid, r.choice([0, 0, 1]), "debit", emi, f"ACH DR LOAN EMI {acct}"))

        # everyday spends (two chunks a month; spend more when stressed)
        for lo, hi in ((6, 12), (15, 26)):
            stm.append((m0 + timedelta(days=r.randint(lo, hi)), r.choice([0, 1]), "debit",
                        round(salary * r.uniform(0.12, 0.22) * (1 + 0.4 * x), -1),
                        f"UPI/DR/{r.randint(10**8, 10**9 - 1)}/{r.choice(MERCHANTS)}"))

        # monthly bureau snapshot (D9), available a few days later
        snap = date(m0.year, m0.month, 5)
        util = base_util + r.uniform(-0.04, 0.04) + strength("util", snap) * r.uniform(0.35, 0.5)
        if hn_util_month == m0:
            util = r.uniform(0.8, 0.95)
        enq30 = sum(1 for d in enq_dates if snap - timedelta(days=30) < d <= snap)
        bounces90 = sum(1 for d in bounce_dates if snap - timedelta(days=90) < d <= snap)
        recent_bounce = any(snap - timedelta(days=60) < d <= snap for d in bounce_dates)
        other_dpd = r.randint(8, 40) if recent_bounce else 0
        stress = strength("util", snap) + strength("salary", snap) + strength("bounce", snap)
        score = int(clamp(base_score - 45 * stress + r.randint(-8, 8), 300, 900))
        if stop_date is None or snap < stop_date:
            w_bureau.writerow([acct, snap.isoformat(), (snap + timedelta(days=r.randint(3, 8))).isoformat(),
                               round(clamp(util, 0.0, 0.99), 3), enq30, open_loans, other_dpd, bounces90, score])

    # ---- write events in time order with a running balance, cut at the stream end / the miss ----
    last_day = min(STREAM_END, stop_date) if stop_date else STREAM_END
    bal = emi * r.uniform(2, 4)
    rows = []
    for d, lag, typ, amt, narr in sorted(stm, key=lambda t: (t[0], 0 if t[2] == "credit" else 1)):
        if d > last_day:
            continue
        bal = bal + amt if typ == "credit" else max(0.0, bal - amt)
        rows.append((d, d + timedelta(days=lag), "statement", typ, round(amt, 2), None, round(bal, 2), narr))
    for d, lag, src, typ, amt, val, bal_, narr in other:
        if d <= last_day:
            rows.append((d, d + timedelta(days=lag), src, typ, amt, val, bal_, narr))
    for d, avail, src, typ, amt, val, bal_, narr in sorted(rows, key=lambda t: (t[0], t[2])):
        w_events.writerow([acct, d.isoformat(), avail.isoformat(), src, typ, amt, val, bal_, narr])

    first_due = next((date(m.year, m.month, due_day) for m in month_starts(STREAM_START, STREAM_END)
                      if date(m.year, m.month, due_day) >= STREAM_START), None)
    return {"salary": salary, "emi": emi, "kind": kind, "profile": profile, "sec": sec_profile, "onset": onset,
            "miss_due": miss_due, "dpd1": (miss_due + timedelta(days=1)) if miss_due else None, "first_due": first_due,
            "n_events": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--id-start", type=int, default=6_000_000, help="first account number (use another range for a second dataset)")
    args = ap.parse_args()
    r = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    f = {n: open(out / f"{n}.csv", "w", newline="", encoding="utf-8") for n in
         ("bp_accounts", "events", "bureau_snapshots", "outcome_labels", "outcome_labels_hidden", "service_templates", "bp_meta")}
    w = {n: csv.writer(h) for n, h in f.items()}
    w["bp_accounts"].writerow(["account_id", "customer_name", "product", "branch", "loan_amount", "emi_amount",
                               "due_date", "dpd", "bucket", "outstanding", "status"])
    w["events"].writerow(["account_id", "event_date", "available_from", "source", "event_type", "amount", "value", "balance", "narration"])
    w["bureau_snapshots"].writerow(["account_id", "snapshot_date", "available_from", "card_utilisation", "enquiries_30d",
                                    "open_loans", "other_loan_dpd", "other_loan_bounces_90d", "bureau_score"])
    w["outcome_labels"].writerow(["account_id", "dpd1_date"])
    w["outcome_labels_hidden"].writerow(["account_id", "dpd1_date"])
    w["service_templates"].writerow(["template_id", "nudge_type", "language", "channel", "body", "placeholders"])
    w["bp_meta"].writerow(["key", "value"])

    stats = {"public": 0, "hidden": 0, "kinds": {}, "profiles": {}, "leads": [], "events": 0}
    seq = args.id_start
    for _ in range(args.n):
        seq += r.randint(1, 5)
        acct = f"006{seq:08d}"
        a = generate_account(r, acct, w["events"], w["bureau_snapshots"])
        emi = a["emi"]
        w["bp_accounts"].writerow([acct, f"{r.choice(FIRST)} {r.choice(LAST)}", r.choice(PRODUCTS), r.choice(BRANCHES),
                                   emi * r.randint(36, 180), emi, a["first_due"].isoformat(), 0, "Current",
                                   emi * r.randint(10, 100), "current"])
        d1 = a["dpd1"]
        w["outcome_labels"].writerow([acct, d1.isoformat() if d1 and d1 <= PUBLIC_END else ""])
        w["outcome_labels_hidden"].writerow([acct, d1.isoformat() if d1 and PUBLIC_END < d1 <= STREAM_END else ""])
        stats["kinds"][a["kind"]] = stats["kinds"].get(a["kind"], 0) + 1
        stats["events"] += a["n_events"]
        if d1:
            stats["public" if d1 <= PUBLIC_END else "hidden"] += 1
            if a["onset"]:
                stats["leads"].append((a["miss_due"] - a["onset"]).days)
                stats["profiles"][a["profile"]] = stats["profiles"].get(a["profile"], 0) + 1

    for tid, ntype, lang, body, placeholders in TEMPLATES:
        w["service_templates"].writerow([tid, ntype, lang, "whatsapp", body, placeholders])

    meta = {"stream_start_date": STREAM_START.isoformat(), "public_days": PUBLIC_DAYS, "hidden_days": HIDDEN_DAYS,
            "history_months": HISTORY_MONTHS, "replay_date": STREAM_START.isoformat(), "generator_seed": args.seed,
            "n_accounts": args.n, "public_end_date": PUBLIC_END.isoformat(), "stream_end_date": STREAM_END.isoformat()}
    for k, v in meta.items():
        w["bp_meta"].writerow([k, v])
    for h in f.values():
        h.close()

    leads = sorted(stats["leads"])
    print(f"accounts {args.n} | events {stats['events']:,} | missed EMI: public {stats['public']}, hidden {stats['hidden']} "
          f"({(stats['public'] + stats['hidden']) / args.n:.1%} of accounts)")
    print("kinds:", stats["kinds"])
    print("defaulter profiles:", stats["profiles"])
    if leads:
        print(f"first-distress -> miss lead (days): min {leads[0]}, median {leads[len(leads) // 2]}, max {leads[-1]}")
    print(f"written to {out}")


if __name__ == "__main__":
    main()
