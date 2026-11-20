"""
BI-03 demo accounts: a few hand-made accounts with a known story, loaded from CSV into bi03.db, then scored and nudged.

  python demo_accounts.py build              write the CSVs to csv_bi03_demo/ (no database)
  python demo_accounts.py load [--tenant T]  build + load the CSVs (append, ids start with DEMO-) + score the days already replayed + draft nudges
  python demo_accounts.py remove             delete every DEMO- account and everything produced for it
  python demo_accounts.py show               one line per demo account: first flag, signal, nudge, score on a few days

The scenarios (the first is the one we asked for; the others cover the other nudge types and a "must NOT flag" control):
  DEMO-001  salary arrives later and later, the EMI follows   -> salary_delay      -> EMI date change
  DEMO-002  salary on time, EMI paid later every month        -> late_emi_payments -> auto-pay
  DEMO-003  salary and EMI fine, card use up, other loan bounces, enquiries -> other signals -> reminder
  DEMO-004  control: one late salary in July, then normal      -> must NOT be flagged

The data follows the same format as generate_bi03.py (statement credits / debits, emi_due / emi_paid, bureau events, monthly bureau snapshot).
Nothing here reads outcome labels; demo accounts get empty label rows ("stayed current") only so the table joins stay valid.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "db"), str(HERE.parent / "agents" / "bi03"), str(HERE)]
import db  # noqa: E402

DB = "bi03"
PREFIX = "DEMO-"
OUT = HERE / "csv_bi03_demo"
STREAM_START = date(2026, 5, 1)
STREAM_END = STREAM_START + timedelta(days=149)          # day 150 (same as generate_bi03.py)
HISTORY_MONTHS = 12


def add_months(d: date, k: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + k, 12)
    return date(y, m + 1, min(d.day, 28))


def months():
    d = add_months(STREAM_START.replace(day=1), -HISTORY_MONTHS)
    while d <= STREAM_END:
        yield d
        d = add_months(d, 1)


def ym(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


# ----------------------------------------------------------------------------------------------- the scenarios
# Per-month overrides are keyed by "YYYY-MM". Anything not listed follows the account's normal behaviour.
SCENARIOS = [
    {   # 1. salary day 5, EMI due 7. Salary slips to the 7th (Aug) and 8th (Sep); the EMI is paid later each month: Jun 8th ... Sep 11th.
        #    Tuned on the model: this mix is flagged for salary_delay. (Bigger salary delays, or salary delay alone, are NOT flagged for it:
        #    the model reads salary-only delay as weak and a stronger EMI-late pattern as the main reason.)
        "id": "DEMO-001", "name": "Sneha Kulkarni", "product": "Personal Loan", "branch": "Pune", "emi": 9000, "salary": 60000,
        "sal_day": 5, "due_day": 7, "emp": "ACME TECH PVT LTD", "util": 0.30, "score": 745,
        "sal_day_by_month": {"2026-08": 7, "2026-09": 8}, "emi_late_by_month": {"2026-06": 1, "2026-07": 2, "2026-08": 3, "2026-09": 4},
    },
    {   # 2. salary on time, EMI due 15: paid 3, 4, 4 days late in Jul, Aug, Sep; card use up a little in Sep, two enquiries in Aug
        "id": "DEMO-002", "name": "Rohan Deshmukh", "product": "Vehicle Loan", "branch": "Mumbai", "emi": 7500, "salary": 55000,
        "sal_day": 3, "due_day": 15, "emp": "SUNRISE LOGISTICS", "util": 0.28, "score": 735,
        "emi_late_by_month": {"2026-07": 3, "2026-08": 4, "2026-09": 4}, "util_by_month": {"2026-09": 0.45},
        "enquiries": [date(2026, 8, 10), date(2026, 8, 20)],
    },
    {   # 3. salary and EMI on time, but another loan bounces, new enquiries pile up, card use jumps in Sep
        "id": "DEMO-003", "name": "Anita Rao", "product": "Home Loan", "branch": "Chennai", "emi": 12000, "salary": 80000,
        "sal_day": 2, "due_day": 15, "emp": "METRO RETAIL LTD", "util": 0.30, "score": 770,
        "util_by_month": {"2026-08": 0.34, "2026-09": 0.72},
        "bounces": [date(2026, 9, 2), date(2026, 9, 12)],
        "enquiries": [date(2026, 8, 28), date(2026, 9, 1), date(2026, 9, 5)],
        "score_by_month": {"2026-09": 700},
    },
    {   # 4. control: a single late salary in July (7 days), everything else normal
        "id": "DEMO-004", "name": "Imran Shaikh", "product": "Personal Loan", "branch": "Delhi", "emi": 6000, "salary": 45000,
        "sal_day": 3, "due_day": 12, "emp": "BLUEWAVE SERVICES", "util": 0.27, "score": 760,
        "sal_day_by_month": {"2026-07": 10},
    },
]
MERCHANTS = ["SWIGGY", "ZOMATO", "BIGBASKET", "DMART", "JIO RECHARGE", "IRCTC", "UBER", "AIRTEL"]

# What each account is meant to show (for the UI and the demo script). "expect" = what the agent should do with it.
STORY = {
    "DEMO-001": ("Salary arrives later each month and the EMI follows", "Flag for salary delay, nudge: EMI date change"),
    "DEMO-002": ("Salary on time, but the EMI is paid later every month", "Flag for late EMI payments, nudge: auto-pay"),
    "DEMO-003": ("Another loan bounces, new credit enquiries pile up, card use jumps", "Flag for the bounce, nudge: auto-pay"),
    "DEMO-004": ("Control: one late salary in July, then back to normal", "No flag (a one-off is not a pattern)"),
}


class DemoError(Exception):
    pass


# The three-account file set exported to <repo>/demo_csv (python demo_accounts.py export): three different stories, three different nudges.
# Tuned on the model like the sample set: with the replay at day 120 nothing is flagged yet; advancing the clock flags them (see demo_csv/README.txt).
THREE = [
    {**SCENARIOS[0], "id": "DEMO-101", "name": "Kavita Nair"},                       # salary slips, EMI follows   -> salary_delay      -> EMI date change
    {**SCENARIOS[1], "id": "DEMO-102", "name": "Arjun Menon"},                       # salary fine, EMI later      -> late_emi_payments -> auto-pay
    {**{k: v for k, v in SCENARIOS[2].items() if k not in ("bounces", "enquiries", "util_by_month", "score_by_month")},
     "id": "DEMO-103", "name": "Divya Sharma",                                       # card use jumps, bureau score falls -> card_utilisation -> reminder
     "util_by_month": {"2026-09": 0.70}, "score_by_month": {"2026-09": 705},
     "enquiries": [date(2026, 8, 30), date(2026, 9, 3), date(2026, 9, 7)]},
]


def build_scenario(s: dict) -> dict:
    """Events and bureau snapshots for one account, in the same shape generate_bi03.py writes."""
    acct, emi, salary = s["id"], s["emi"], s["salary"]
    stm, other, snaps = [], [], []
    bounces, enqs = s.get("bounces", []), s.get("enquiries", [])
    open_loans = 2
    for i, m0 in enumerate(months()):
        k = ym(m0)
        sal_date = date(m0.year, m0.month, s.get("sal_day_by_month", {}).get(k, s["sal_day"]))
        stm.append((sal_date, "credit", float(salary), f"SALARY CREDIT {s['emp']}"))

        due = date(m0.year, m0.month, s["due_day"])
        late = s.get("emi_late_by_month", {}).get(k, 0)
        other.append((due, 0, "repayment", "emi_due", float(emi), None, None))
        paid = due + timedelta(days=late)
        other.append((paid, 0, "repayment", "emi_paid", float(emi), float(late), None))
        stm.append((paid, "debit", float(emi), f"ACH DR LOAN EMI {acct}"))

        for day in (9, 20):
            stm.append((date(m0.year, m0.month, day), "debit", round(salary * 0.15, 2), f"UPI/DR/{100000000 + i * 7919 + day}/{MERCHANTS[(i + day) % len(MERCHANTS)]}"))

        snap = date(m0.year, m0.month, 5)
        enq30 = sum(1 for d in enqs if snap - timedelta(days=30) < d <= snap)
        b90 = sum(1 for d in bounces if snap - timedelta(days=90) < d <= snap)
        recent_b = any(snap - timedelta(days=60) < d <= snap for d in bounces)
        snaps.append([acct, snap.isoformat(), (snap + timedelta(days=5)).isoformat(), round(s.get("util_by_month", {}).get(k, s["util"]), 3),
                      enq30, open_loans, 25 if recent_b else 0, b90, s.get("score_by_month", {}).get(k, s["score"])])

    for d in bounces:
        other.append((d, 1, "bureau", "other_loan_bounce", round(emi * 0.6, 2), None, "OTHER LOAN EMI BOUNCE"))
    for d in enqs:
        other.append((d, 1, "bureau", "new_enquiry", None, 1.0, "NEW CREDIT ENQUIRY"))

    rows, bal = [], emi * 3.0
    for d, typ, amt, narr in sorted(stm, key=lambda t: (t[0], 0 if t[1] == "credit" else 1)):
        if d > STREAM_END:
            continue
        bal = bal + amt if typ == "credit" else max(0.0, bal - amt)
        rows.append((d, d, "statement", typ, round(amt, 2), None, round(bal, 2), narr))
    for d, lag, src, typ, amt, val, narr in other:
        if d <= STREAM_END:
            rows.append((d, d + timedelta(days=lag), src, typ, amt, val, None, narr))
    rows.sort(key=lambda t: (t[0], t[2]))
    first_due = next(date(m.year, m.month, s["due_day"]) for m in months() if date(m.year, m.month, s["due_day"]) >= STREAM_START)
    return {"events": [[acct, d.isoformat(), a.isoformat(), src, typ, amt, val, bal_, narr] for d, a, src, typ, amt, val, bal_, narr in rows],
            "snaps": [r for r in snaps if date.fromisoformat(r[2]) <= STREAM_END + timedelta(days=10)], "first_due": first_due}


def write_csvs(out: Path = OUT, scenarios: list | None = None, only: tuple | None = None) -> dict:
    """Write the CSV files for `scenarios` (default: the sample set). `only` limits which of the five files are written."""
    out.mkdir(parents=True, exist_ok=True)
    heads = {"bp_accounts": ["account_id", "customer_name", "product", "branch", "loan_amount", "emi_amount", "due_date", "dpd", "bucket", "outstanding", "status"],
             "events": ["account_id", "event_date", "available_from", "source", "event_type", "amount", "value", "balance", "narration"],
             "bureau_snapshots": ["account_id", "snapshot_date", "available_from", "card_utilisation", "enquiries_30d", "open_loans", "other_loan_dpd",
                                  "other_loan_bounces_90d", "bureau_score"],
             "outcome_labels": ["account_id", "dpd1_date"], "outcome_labels_hidden": ["account_id", "dpd1_date"]}
    data = {k: [] for k in heads}
    for s in (scenarios or SCENARIOS):
        b = build_scenario(s)
        data["bp_accounts"].append([s["id"], s["name"], s["product"], s["branch"], s["emi"] * 60, s["emi"], b["first_due"].isoformat(), 0, "Current", s["emi"] * 40, "current"])
        data["events"] += b["events"]
        data["bureau_snapshots"] += b["snaps"]
        data["outcome_labels"].append([s["id"], ""])
        data["outcome_labels_hidden"].append([s["id"], ""])
    for name, rows in data.items():
        if only and name not in only:
            continue
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(heads[name])
            w.writerows(["" if v is None else v for v in r] for r in rows)
    return {k: len(v) for k, v in data.items() if not only or k in only}


# ----------------------------------------------------------------------------------------------- database side
LOAD_TYPES = {
    "bp_accounts": {"loan_amount": float, "emi_amount": float, "dpd": int, "outstanding": float},
    "bureau_snapshots": {"card_utilisation": float, "enquiries_30d": int, "open_loans": int, "other_loan_dpd": int, "other_loan_bounces_90d": int, "bureau_score": int},
    "events": {"amount": float, "value": float, "balance": float},
    "outcome_labels": {}, "outcome_labels_hidden": {},
}
CHILD_ORDER = ["nudges", "flag_outcomes", "flags", "risk_scores", "events", "bureau_snapshots", "outcome_labels", "outcome_labels_hidden", "bp_accounts"]


def demo_count(tenant: str) -> int:
    return db.query_one(tenant, "SELECT COUNT(*) n FROM bp_accounts WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%'", database=DB)["n"]


def remove(tenant: str) -> dict:
    out = {}
    for t in CHILD_ORDER:
        out[t] = db.execute(tenant, f"DELETE FROM {t} WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%'", database=DB)
    return out


def load_csvs(tenant: str, src: Path = OUT) -> dict:
    import load_bi03 as L
    if demo_count(tenant):
        raise DemoError("DEMO- accounts are already loaded. Remove them first.")
    missing = [f"{t}.csv" for t in LOAD_TYPES if not (src / f"{t}.csv").exists()]
    if missing:
        raise DemoError(f"CSV files not found in {src}: {missing}")
    loaded = {}
    for table in ("bp_accounts", "outcome_labels", "outcome_labels_hidden", "bureau_snapshots", "events"):
        loaded[table] = L.load_table(tenant, table, src / f"{table}.csv", LOAD_TYPES[table], {})
    return loaded


def score_and_nudge(tenant: str) -> dict:
    """Score only the demo accounts for every day the replay has already covered, raise flags, draft nudges.
    Same functions the daily runner uses, so the numbers match what a later replay of the whole book would give."""
    import pandas as pd
    import features as F
    import model as M
    import nudge as N
    import replay as RP
    import runner as R

    if not demo_count(tenant):
        raise DemoError("Load the CSV first: there are no DEMO- accounts to analyse.")
    for t in ("nudges", "flag_outcomes", "flags", "risk_scores"):          # re-running is safe: start from a clean slate for the demo accounts
        db.execute(tenant, f"DELETE FROM {t} WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%'", database=DB)

    ev = db.query_df(tenant, "SELECT event_id, account_id, event_date, available_from, source, event_type, amount, value, balance, narration "
                             "FROM events WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%'", database=DB)
    sn = db.query_df(tenant, "SELECT account_id, snapshot_date, available_from, card_utilisation, enquiries_30d, open_loans, other_loan_dpd, "
                             "other_loan_bounces_90d, bureau_score FROM bureau_snapshots WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%'", database=DB)
    ac = db.query_df(tenant, "SELECT account_id, emi_amount FROM bp_accounts WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%' ORDER BY account_id", database=DB)
    eng = F.FeatureEngine(F.prepare_events(ev), F.prepare_snaps(sn), ac)

    st = RP.status(tenant)
    start = M.stream_start(tenant)
    bundle = M.load_bundle(R.MODEL_NAME)
    version, tau = R.active_threshold(tenant, bundle)
    bundle = {**bundle, "tau": tau}
    last_flag = pd.Series(dtype="datetime64[ns]")
    n_scores = n_flags = 0
    last_day = min(st["replay_day"], st["last_scored_day"])      # never score days the real book has not been processed for
    for day in range(st["first_scored_day"], last_day + 1):
        t = M.day_to_date(start, day)
        res = R.compute_day(eng, bundle, t, last_flag, confirm=False)
        sc = res["scores"].reset_index().rename(columns={"index": "account_id"})
        sc["as_of_date"], sc["model_version"] = t.date().isoformat(), bundle["version"]
        db.insert_many(tenant, "risk_scores", R._records(sc[["account_id", "as_of_date", "score", "dominant_signal", "signals_json", "model_version"]]), database=DB)
        n_scores += len(sc)
        fl = res["flags"].reset_index().rename(columns={"index": "account_id"})
        if len(fl):
            fl["flagged_on"], fl["threshold_version"], fl["status"] = t.date().isoformat(), version, "open"
            db.insert_many(tenant, "flags", R._records(fl[["account_id", "flagged_on", "score", "dominant_signal", "signals_json", "threshold_version", "status"]]), database=DB)
            for a in fl["account_id"]:
                last_flag.loc[a] = t
            n_flags += len(fl)
    nudged = N.process_flags(tenant, eng=eng)
    return {"score_rows": n_scores, "flags": n_flags, "nudges": nudged, "tau": round(tau, 4), "days": [st["first_scored_day"], last_day]}


# ----------------------------------------------------------------------------------------------- template and upload
TEMPLATE_README = """PortfolioIQ BI-03: CSV template for "Try new data"

Three files. Edit them (or replace the rows), keep the file names and the column names, then choose all three in the app
(Early warning > Try new data (CSV) > Choose files > Load chosen files > Run analysis).

bp_accounts.csv   one row per account
  account_id (must start with DEMO-), customer_name, product, branch, emi_amount, due_date (first EMI due date, YYYY-MM-DD)
  optional: loan_amount, outstanding

events.csv        one row per event
  account_id, event_date, available_from, source, event_type, amount, value, balance, narration
  available_from = first day the system may see the row (same as event_date, or later). Never earlier.
  source / event_type:
    statement / credit        salary or other money in. narration starting with SALARY marks a salary credit (amount = money, balance = account balance after it)
    statement / debit         money out (EMI debit, spends). amount = money, balance = account balance after it
    repayment / emi_due       the EMI falls due on event_date. amount = EMI
    repayment / emi_paid      the EMI was paid on event_date. amount = EMI, value = days after the due date it was paid (0 = on time)
    bureau / other_loan_bounce  another loan of the customer bounced. amount = bounced EMI
    bureau / new_enquiry      the customer applied for new credit. value = 1

bureau_snapshots.csv   one row per account per month (the monthly bureau refresh)
  account_id, snapshot_date, available_from (after snapshot_date), card_utilisation (0 to 1), enquiries_30d, open_loans,
  other_loan_dpd, other_loan_bounces_90d, bureau_score

Rules the app checks
  - dates are YYYY-MM-DD and no later than 2026-09-27 (the end of the data stream)
  - give each account about a year of history (monthly salary, EMI due and paid, monthly bureau snapshot) before 2026-05-01,
    then the months of the stream. The agent compares today with the account's own normal.
  - at most 1,000 accounts, 150,000 events, 20,000 bureau snapshots (each file up to 20 MB)

What the agent reads: a salary that arrives later than the account's usual day, EMIs paid late, bounces on other loans,
new credit enquiries, card utilisation going up, a falling bureau score and a low balance compared with the EMI.
A flag can only appear from the day the agent first sees the pattern (the replay runs 30 Jun to 27 Sep 2026).
"""

LIMITS = {"accounts": 1_000, "events": 150_000, "snapshots": 20_000}
SOURCES = {"statement": {"credit", "debit"}, "repayment": {"emi_due", "emi_paid"}, "bureau": {"new_enquiry", "other_loan_bounce"}}


def template_zip() -> bytes:
    """The sample CSVs (the four demo accounts) plus a README, as one zip the user can edit and upload."""
    import io
    import zipfile
    if not (OUT / "events.csv").exists():
        write_csvs()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("bp_accounts", "events", "bureau_snapshots"):
            z.write(OUT / f"{name}.csv", f"{name}.csv")
        z.writestr("README.txt", TEMPLATE_README)
    return buf.getvalue()


def bulk_zip(n: int = 200, seed: int = 20261120) -> bytes:
    """A bulk sample: n accounts from the same generator as the 6,000-account book (healthy, recovering and about 5% heading for a miss),
    under their own ids DEMO-B0001... and their own seed, in the three-file template format."""
    import io
    import random
    import zipfile
    import generate_bi03 as G

    r = random.Random(seed)
    bufs = {k: io.StringIO() for k in ("bp_accounts", "events", "bureau_snapshots")}
    w = {k: csv.writer(b, lineterminator="\n") for k, b in bufs.items()}
    w["bp_accounts"].writerow(["account_id", "customer_name", "product", "branch", "loan_amount", "emi_amount", "due_date", "dpd", "bucket", "outstanding", "status"])
    w["events"].writerow(["account_id", "event_date", "available_from", "source", "event_type", "amount", "value", "balance", "narration"])
    w["bureau_snapshots"].writerow(["account_id", "snapshot_date", "available_from", "card_utilisation", "enquiries_30d", "open_loans", "other_loan_dpd",
                                    "other_loan_bounces_90d", "bureau_score"])
    for i in range(1, n + 1):
        acct = f"DEMO-B{i:04d}"
        a = G.generate_account(r, acct, w["events"], w["bureau_snapshots"])
        emi = a["emi"]
        w["bp_accounts"].writerow([acct, f"{r.choice(G.FIRST)} {r.choice(G.LAST)}", r.choice(G.PRODUCTS), r.choice(G.BRANCHES), emi * r.randint(36, 180), emi,
                                   a["first_due"].isoformat(), 0, "Current", emi * r.randint(10, 100), "current"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, b in bufs.items():
            z.writestr(f"{name}.csv", b.getvalue())
        z.writestr("README.txt", TEMPLATE_README + f"\nThis bulk sample holds {n} generated accounts (ids DEMO-B0001 to DEMO-B{n:04d}). About 5% are heading for a missed EMI.\n")
    return out.getvalue()


def _read(text: str, label: str, required: list[str], limit: int, errors: list[str]) -> list[dict]:
    import io
    reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
    cols = [(c or "").strip() for c in (reader.fieldnames or [])]
    reader.fieldnames = cols
    missing = [c for c in required if c not in cols]
    if missing:
        errors.append(f"{label}: missing column(s) {', '.join(missing)}")
        return []
    rows = [{k: (v or "").strip() for k, v in r.items() if k} for r in reader]
    rows = [r for r in rows if any(r.values())]
    if not rows:
        errors.append(f"{label}: no rows")
    if len(rows) > limit:
        errors.append(f"{label}: {len(rows)} rows, the limit is {limit}")
        return []
    return rows


def _date(v: str, what: str, errors: list[str], where: str) -> date | None:
    try:
        return date.fromisoformat(v)
    except ValueError:
        errors.append(f"{where}: {what} '{v}' is not a date (use YYYY-MM-DD)")
        return None


def _num(v: str, what: str, errors: list[str], where: str, required: bool = False) -> float | None:
    if v == "":
        if required:
            errors.append(f"{where}: {what} is empty")
        return None
    try:
        return float(v)
    except ValueError:
        errors.append(f"{where}: {what} '{v}' is not a number")
        return None


def load_uploaded(tenant: str, texts: dict) -> dict:
    """Load the user's own three CSVs (as text). Everything is checked first; if anything is wrong nothing is written and the errors are listed."""
    import tempfile
    errors: list[str] = []
    accounts = _read(texts.get("accounts"), "bp_accounts.csv", ["account_id", "customer_name", "product", "branch", "emi_amount", "due_date"], LIMITS["accounts"], errors)
    events = _read(texts.get("events"), "events.csv", ["account_id", "event_date", "source", "event_type"], LIMITS["events"], errors)
    snaps = _read(texts.get("snapshots"), "bureau_snapshots.csv", ["account_id", "snapshot_date", "available_from", "card_utilisation", "enquiries_30d", "open_loans",
                                                                    "other_loan_dpd", "other_loan_bounces_90d", "bureau_score"], LIMITS["snapshots"], errors)
    ids = set()
    acc_out = []
    for i, r in enumerate(accounts, 2):
        w = f"bp_accounts.csv line {i}"
        if not r["account_id"].startswith(PREFIX):
            errors.append(f"{w}: account_id '{r['account_id']}' must start with {PREFIX} (so demo data can never mix with real accounts)")
        if r["account_id"] in ids:
            errors.append(f"{w}: account_id '{r['account_id']}' appears twice")
        ids.add(r["account_id"])
        emi = _num(r["emi_amount"], "emi_amount", errors, w, True)
        if emi is not None and emi <= 0:
            errors.append(f"{w}: emi_amount must be above 0")
        _date(r["due_date"], "due_date", errors, w)
        acc_out.append([r["account_id"], r["customer_name"], r["product"], r["branch"], _num(r.get("loan_amount", ""), "loan_amount", errors, w) or (emi or 0) * 60,
                        emi, r["due_date"], 0, "Current", _num(r.get("outstanding", ""), "outstanding", errors, w) or (emi or 0) * 40, "current"])

    ev_out, salary_count = [], {}
    for i, r in enumerate(events, 2):
        w = f"events.csv line {i}"
        if r["account_id"] not in ids:
            errors.append(f"{w}: account_id '{r['account_id']}' is not in bp_accounts.csv")
        d = _date(r["event_date"], "event_date", errors, w)
        av = _date(r.get("available_from") or r["event_date"], "available_from", errors, w)
        if d and av and av < d:
            errors.append(f"{w}: available_from {av} is before event_date {d} (a row cannot be visible before it happened)")
        if d and d > STREAM_END:
            errors.append(f"{w}: event_date {d} is after the end of the data stream ({STREAM_END})")
        if r["source"] not in SOURCES or r["event_type"] not in SOURCES.get(r["source"], set()):
            errors.append(f"{w}: source/event_type '{r['source']}/{r['event_type']}' is not allowed (see README)")
        amt = _num(r.get("amount", ""), "amount", errors, w, r["event_type"] in ("credit", "debit", "emi_due", "emi_paid"))
        val = _num(r.get("value", ""), "value", errors, w, r["event_type"] == "emi_paid")
        bal = _num(r.get("balance", ""), "balance", errors, w)
        if r["event_type"] == "credit" and r.get("narration", "").upper().startswith("SALARY"):
            salary_count[r["account_id"]] = salary_count.get(r["account_id"], 0) + 1
        ev_out.append([r["account_id"], r["event_date"], (av or d).isoformat() if (av or d) else r["event_date"], r["source"], r["event_type"], amt, val, bal, r.get("narration", "")])

    sn_out = []
    for i, r in enumerate(snaps, 2):
        w = f"bureau_snapshots.csv line {i}"
        if r["account_id"] not in ids:
            errors.append(f"{w}: account_id '{r['account_id']}' is not in bp_accounts.csv")
        sd, av = _date(r["snapshot_date"], "snapshot_date", errors, w), _date(r["available_from"], "available_from", errors, w)
        if sd and av and av <= sd:
            errors.append(f"{w}: available_from must be after snapshot_date (bureau data arrives with a lag)")
        nums = {c: _num(r[c], c, errors, w, True) for c in ("card_utilisation", "enquiries_30d", "open_loans", "other_loan_dpd", "other_loan_bounces_90d", "bureau_score")}
        if nums["card_utilisation"] is not None and not 0 <= nums["card_utilisation"] <= 1:
            errors.append(f"{w}: card_utilisation must be between 0 and 1")
        sn_out.append([r["account_id"], r["snapshot_date"], r["available_from"], *[nums[c] for c in ("card_utilisation", "enquiries_30d", "open_loans", "other_loan_dpd", "other_loan_bounces_90d", "bureau_score")]])

    have_ev = {r[0] for r in ev_out}
    have_sn = {r[0] for r in sn_out}
    for a in sorted(ids):
        if a not in have_ev:
            errors.append(f"{a}: no rows in events.csv")
        if a not in have_sn:
            errors.append(f"{a}: no rows in bureau_snapshots.csv")
    if errors:
        more = f" (and {len(errors) - 12} more)" if len(errors) > 12 else ""
        raise DemoError("Nothing was loaded. Fix these and try again: " + " | ".join(errors[:12]) + more)

    warnings_ = [f"{a}: only {salary_count.get(a, 0)} salary credit(s). With less than about 6 months of salary history the agent has little to compare against, scores will be weak."
                 for a in sorted(ids) if salary_count.get(a, 0) < 6]
    heads = {"bp_accounts": ["account_id", "customer_name", "product", "branch", "loan_amount", "emi_amount", "due_date", "dpd", "bucket", "outstanding", "status"],
             "events": ["account_id", "event_date", "available_from", "source", "event_type", "amount", "value", "balance", "narration"],
             "bureau_snapshots": ["account_id", "snapshot_date", "available_from", "card_utilisation", "enquiries_30d", "open_loans", "other_loan_dpd",
                                  "other_loan_bounces_90d", "bureau_score"],
             "outcome_labels": ["account_id", "dpd1_date"], "outcome_labels_hidden": ["account_id", "dpd1_date"]}
    data = {"bp_accounts": acc_out, "events": ev_out, "bureau_snapshots": sn_out,
            "outcome_labels": [[a, ""] for a in sorted(ids)], "outcome_labels_hidden": [[a, ""] for a in sorted(ids)]}
    with tempfile.TemporaryDirectory() as tmp:
        for name, rows in data.items():
            with open(Path(tmp) / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(heads[name])
                w.writerows(["" if v is None else v for v in r] for r in rows)
        loaded = load_csvs(tenant, Path(tmp))
    return {"rows_loaded": loaded, "warnings": warnings_}


def csv_info() -> dict:
    """What is in the CSV folder (built on first use): file names, row counts and the first rows of the event file."""
    if not (OUT / "events.csv").exists():
        write_csvs()
    files = []
    for name in LOAD_TYPES:
        with open(OUT / f"{name}.csv", newline="", encoding="utf-8") as f:
            files.append({"file": f"{name}.csv", "rows": max(sum(1 for _ in f) - 1, 0)})
    with open(OUT / "events.csv", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        preview = [row for _, row in zip(range(5), r)]
    return {"folder": OUT.name, "files": files, "preview": preview}


def validate(tenant: str) -> list[dict]:
    """Integrity checks on the loaded demo rows (same spirit as load_bi03.py)."""
    q = lambda sql: db.query_one(tenant, sql, database=DB)["n"]
    d = "AND account_id LIKE 'DEMO-%'"
    checks = [
        ("every account has events and bureau snapshots", q(f"SELECT COUNT(*) n FROM bp_accounts a WHERE a.tenant_id=:tenant_id AND a.account_id LIKE 'DEMO-%' AND (NOT EXISTS "
                                                          "(SELECT 1 FROM events e WHERE e.account_id=a.account_id) OR NOT EXISTS (SELECT 1 FROM bureau_snapshots s WHERE s.account_id=a.account_id))") == 0),
        ("no event is available before it happened", q(f"SELECT COUNT(*) n FROM events WHERE tenant_id=:tenant_id {d} AND available_from < event_date") == 0),
        ("every bureau snapshot arrives after its as-of date", q(f"SELECT COUNT(*) n FROM bureau_snapshots WHERE tenant_id=:tenant_id {d} AND available_from <= snapshot_date") == 0),
        ("every account starts current (no DPD)", q(f"SELECT COUNT(*) n FROM bp_accounts WHERE tenant_id=:tenant_id {d} AND (status != 'current' OR dpd != 0)") == 0),
        ("no event after the stream end", q(f"SELECT COUNT(*) n FROM events WHERE tenant_id=:tenant_id {d} AND event_date > '{STREAM_END.isoformat()}'") == 0),
    ]
    with db.get_conn(DB) as conn:
        checks.append(("foreign keys all resolve", not conn.execute("PRAGMA foreign_key_check").fetchall()))
    return [{"name": n, "ok": bool(ok)} for n, ok in checks]


def status(tenant: str) -> dict:
    """The demo accounts as the UI shows them: story, latest score and flag (nothing after the replay pointer exists in the tables)."""
    accts = db.query(tenant, "SELECT account_id, customer_name, product, branch, emi_amount FROM bp_accounts WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%' ORDER BY account_id", database=DB)
    ptr = db.query_one(tenant, "SELECT value FROM bp_meta WHERE tenant_id=:tenant_id AND key='replay_date'", database=DB)
    last = db.query_one(tenant, "SELECT MAX(as_of_date) d FROM risk_scores WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%'", database=DB)["d"]
    score = {r["account_id"]: r["score"] for r in db.query(tenant, "SELECT account_id, score FROM risk_scores WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%' AND as_of_date=:d", {"d": last}, database=DB)} if last else {}
    flag = {}                                                      # latest flag of each demo account (rows are in date order, the last one wins)
    for r in db.query(tenant, "SELECT f.account_id, f.flag_id, f.flagged_on, f.dominant_signal, n.nudge_type, n.status AS nudge_status FROM flags f "
                              "LEFT JOIN nudges n ON n.flag_id=f.flag_id AND n.tenant_id=f.tenant_id WHERE f.tenant_id=:tenant_id AND f.account_id LIKE 'DEMO-%' "
                              "ORDER BY f.flagged_on, f.flag_id", database=DB):
        flag[r["account_id"]] = {k: r[k] for k in ("flag_id", "flagged_on", "dominant_signal", "nudge_type", "nudge_status")}
    out = []
    for a in accts:
        story, expect = STORY.get(a["account_id"], ("", ""))
        s = score.get(a["account_id"])
        out.append({**a, "story": story, "expect": expect, "score": round(s, 3) if s is not None else None, "score_date": last if s is not None else None, "flag": flag.get(a["account_id"])})
    out.sort(key=lambda a: (a["flag"] is None, -(a["score"] or 0)))      # flagged first, then the highest score
    return {"loaded": bool(accts), "scored": bool(score), "replay_date": ptr["value"] if ptr else None, "accounts_total": len(accts), "flagged_total": len(flag),
            "accounts": out[:10], "database": counts(tenant)}


def counts(tenant: str) -> dict:
    """What is in the database right now: everything, and the part that came from the demo CSVs (ids start with DEMO-)."""
    tables = {"accounts": "bp_accounts", "events": "events", "snapshots": "bureau_snapshots", "scores": "risk_scores", "flags": "flags"}
    n = lambda t, like: db.query_one(tenant, f"SELECT COUNT(*) n FROM {t} WHERE tenant_id=:tenant_id" + (" AND account_id LIKE 'DEMO-%'" if like else ""), database=DB)["n"]
    return {"all": {k: n(t, False) for k, t in tables.items()}, "demo": {k: n(t, True) for k, t in tables.items()}}


def show(tenant: str) -> None:
    rows = db.query(tenant, "SELECT f.flag_id, f.account_id, f.flagged_on, ROUND(f.score, 3) AS score, f.dominant_signal, n.nudge_type, n.status AS nudge_status, n.gate_passed "
                            "FROM flags f LEFT JOIN nudges n ON n.flag_id = f.flag_id AND n.tenant_id = f.tenant_id "
                            "WHERE f.tenant_id = :tenant_id AND f.account_id LIKE 'DEMO-%' ORDER BY f.account_id, f.flagged_on", database=DB)
    flagged = {r["account_id"] for r in rows}
    start = db.query_one(tenant, "SELECT value FROM bp_meta WHERE tenant_id = :tenant_id AND key = 'stream_start_date'", database=DB)["value"]
    s0 = date.fromisoformat(start)
    for r in rows:
        day = (date.fromisoformat(r["flagged_on"]) - s0).days + 1
        print(f"{r['account_id']}  flag {r['flag_id']}  {r['flagged_on']} (day {day})  score {r['score']}  signal={r['dominant_signal']}  nudge={r['nudge_type']} [{r['nudge_status']}, gate_passed={r['gate_passed']}]")
    for s in SCENARIOS:
        if s["id"] not in flagged:
            print(f"{s['id']}  no flag")
    print("\nscore (every 10th day from day 90):")
    sc = db.query(tenant, "SELECT account_id, as_of_date, score FROM risk_scores WHERE tenant_id = :tenant_id AND account_id LIKE 'DEMO-%' ORDER BY account_id, as_of_date", database=DB)
    by = {}
    for r in sc:
        d = (date.fromisoformat(r["as_of_date"]) - s0).days + 1
        if d >= 90 and d % 10 == 0:
            by.setdefault(r["account_id"], []).append(f"d{d}:{r['score']:.2f}")
    for a, v in by.items():
        print(f"  {a}  " + "  ".join(v))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "export", "load", "remove", "show"])
    ap.add_argument("--tenant", default="abc_bank")
    a = ap.parse_args()
    try:
        if a.cmd == "build":
            print(write_csvs())
        elif a.cmd == "export":                      # the three-account set, ready to choose in the UI
            target = HERE.parents[1] / "demo_csv"
            print(write_csvs(target, THREE, only=("bp_accounts", "events", "bureau_snapshots")), "->", target)
        elif a.cmd == "load":
            print("csv:", write_csvs())
            db.init_db(DB)
            print("loaded:", load_csvs(a.tenant))
            print("scored:", json.dumps(score_and_nudge(a.tenant)))
            show(a.tenant)
        elif a.cmd == "remove":
            print(remove(a.tenant))
        else:
            show(a.tenant)
    except DemoError as e:
        sys.exit(str(e))
