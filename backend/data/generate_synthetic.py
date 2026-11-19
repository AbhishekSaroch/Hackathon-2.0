"""
Synthetic BI-02 data -> CSV files (no tenant_id column: the loader adds it).

Run:  python generate_synthetic.py [--seed 42] [--out <dir>]
Writes: accounts, repayments, calls, promises, statement_txns, gold_labels (.csv)

300 delinquent accounts, 5 labels:
  forgot 55 | cant_pay 75 | wont_pay 75 | disputed 45 | insufficient 50
Built-in hard cases:
  - wont_contradiction: says "no job" but salary keeps arriving
  - wont_fake_dispute:  claims a dispute, statement shows none
  - *_thin:             first statement pull is thin, fresh pull (pull_no=2) settles it
  - insufficient:       irregular income, still unclear after a fresh pull -> agent must abstain
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 11, 19)
OUT_DIR = Path(__file__).resolve().parent / "csv"

FIRST = ["Rahul", "Amit", "Neha", "Priya", "Vikram", "Karan", "Sana", "Rohit", "Anjali", "Mohd", "Rajesh",
         "Asha", "Mayur", "Shreya", "Deepak", "Pooja", "Suresh", "Kavita", "Imran", "Lakshmi", "Arjun", "Meena"]
LAST = ["Sharma", "Singh", "Gupta", "Verma", "Joshi", "Mehta", "Khan", "Das", "Rao", "Patel", "Saxena",
        "Moghe", "Vador", "Iyer", "Nair", "Reddy", "Kapoor", "Shaikh", "Thakur", "Pillai"]
PRODUCTS = ["Home Loan", "Personal Loan", "Business Loan", "Vehicle Loan"]
BRANCHES = ["Mumbai", "Pune", "Chennai", "Delhi", "Bengaluru", "Hyderabad"]
EMPLOYERS = ["ACME TECH PVT LTD", "SUNRISE LOGISTICS", "BLUEWAVE SERVICES", "METRO RETAIL LTD",
             "GREENFIELD INFRA", "NOVA PHARMA"]
MERCHANTS = ["SWIGGY", "ZOMATO", "BIGBASKET", "DMART", "JIO RECHARGE", "IRCTC", "UBER", "AIRTEL"]
BIG_SPEND = ["AMAZON PAY", "CROMA STORES", "MAKEMYTRIP", "APPLE STORE", "BOOKMYSHOW", "TANISHQ"]

# ---------------------------------------------------------------- scenarios
SCENARIOS = {
    "forgot":            dict(n=55, label="forgot",     income="steady",    late=(0, 0), missed=(1, 1), calls=["forgot"],
                              ptp=(1, 1, 0.7), spend="normal", buffer=2.0, thin=False, dispute=False),
    "cant_stopped":      dict(n=30, label="cant_pay",   income="stopped",   late=(1, 2), missed=(2, 3),
                              calls=["job_loss", "job_loss", "job_loss", "business", "medical"],
                              ptp=(2, 3, 0.15), spend="low", buffer=0, thin=False, dispute=False),
    "cant_dropped":      dict(n=30, label="cant_pay",   income="dropped",   late=(1, 2), missed=(2, 3), calls=["income_cut"],
                              ptp=(2, 3, 0.2), spend="low", buffer=0, thin=False, dispute=False),
    "cant_thin":         dict(n=15, label="cant_pay",   income="stopped",   late=(1, 2), missed=(2, 3), calls=["job_loss"],
                              ptp=(2, 3, 0.15), spend="low", buffer=0, thin=True, dispute=False),
    "wont_contradiction": dict(n=25, label="wont_pay",  income="steady",    late=(0, 0), missed=(3, 4), calls=["job_loss"],
                              ptp=(3, 4, 0.0), spend="big", buffer=2.0, thin=False, dispute=False),
    "wont_stall":        dict(n=30, label="wont_pay",   income="steady",    late=(0, 0), missed=(3, 4), calls=["stall"],
                              ptp=(3, 4, 0.05), spend="big", buffer=2.0, thin=False, dispute=False),
    "wont_fake_dispute": dict(n=20, label="wont_pay",   income="steady",    late=(0, 1), missed=(2, 4), calls=["fake_dispute"],
                              ptp=(0, 1, 0.0), spend="big", buffer=2.0, thin=False, dispute=False),
    "disputed":          dict(n=35, label="disputed",   income="steady",    late=(0, 0), missed=(3, 4), calls=["dispute"],
                              ptp=(0, 1, 0.0), spend="normal", buffer=2.0, thin=False, dispute=True),
    "disputed_thin":     dict(n=10, label="disputed",   income="steady",    late=(0, 0), missed=(3, 4), calls=["dispute_vague"],
                              ptp=(0, 1, 0.0), spend="normal", buffer=2.0, thin=True, dispute=True),
    "insufficient":      dict(n=50, label="insufficient", income="irregular", late=(0, 0), missed=(1, 1), calls=["vague"],
                              ptp=(4, 4, None), spend="normal", buffer=0, thin="half", dispute=False,
                              mixed_repay=True, mixed_ptp=True),
}

RATIONALE = {
    "forgot": "First miss after {n_paid} of 5 earlier EMIs on time. Salary still credited and balance covered the EMI. Customer said they forgot.",
    "cant_stopped": "Salary credits stopped about {stop_ago} months ago, balance is low, customer states income loss. {n_ptp} promises, {n_kept} kept.",
    "cant_dropped": "Salary fell by about {drop_pct}% over the last months, balance is low, customer states an income cut. {n_ptp} promises, {n_kept} kept.",
    "cant_thin": "Salary stopped about {stop_ago} months ago (visible only in the fresh statement), balance low, customer states job loss.",
    "wont_contradiction": "Customer says income is gone, but a salary of Rs {salary} is still credited and balance covers the EMI. {n_ptp} promises, {n_kept} kept.",
    "wont_stall": "Salary still credited, large discretionary spends, no reason given, {n_ptp} promises, {n_kept} kept.",
    "wont_fake_dispute": "Customer claims a dispute but the statement shows no double debit or reversal. Salary and balance are fine.",
    "disputed": "Statement shows the EMI debited twice on {dd}, reversal came later. Misses started right after the double debit.",
    "disputed_thin": "Fresh statement shows the EMI debited twice on {dd}, reversal later. First pull was too thin to see it.",
    "insufficient": "Irregular freelance income, mixed promise history, no clear reason stated. Evidence stays inconclusive even with a fresh statement.",
}

# ---------------------------------------------------------------- calls
OPENERS = ["Namaste, ABC Bank se bol rahe hain. Aapki EMI pending hai, kab tak payment ho payega?",
           "Hello sir, ABC Bank collections se call hai. EMI due hai abhi tak, kya problem hai?",
           "Namaste, aapka loan EMI overdue hai. Payment kab karenge?"]
FOLLOWUPS = ["Theek hai, kab tak confirm kar sakte hain?", "Samajh gaya. To payment kab tak hoga?"]
GENERIC_CLOSE = ["Haan dekhte hain.", "Ok theek hai.", "Haan ji, okay."]
CLOSERS = {"forgot": ["Aaj hi kar deta hoon.", "Pakka, aaj shaam tak."]}
POOLS = {
    "forgot": ("forgot", ["Oh ho, bhool gaya tha main. Aaj hi kar deta hoon.",
                          "Arre sorry, yaad nahi raha. Shaam tak kar dungi.",
                          "Mujhe laga ho gaya hoga, aaj app se bhar dunga."]),
    "job_loss": ("job_loss", ["Sir meri job chali gayi hai, abhi paisa nahi hai.",
                              "Company ne nikaal diya do mahine pehle, abhi koi income nahi hai.",
                              "Salary band ho gayi hai, naya kaam milte hi bhar dunga."]),
    "business": ("business_loss", ["Dukaan band hai sir, dhandha bilkul thanda hai.",
                                   "Business me loss ho gaya, abhi EMI ka intezaam nahi hai."]),
    "medical": ("medical", ["Ghar me medical emergency thi, saara paisa wahan lag gaya."]),
    "income_cut": ("income_cut", ["Company ne salary aadhi kar di hai, expenses nikalna mushkil hai.",
                                  "Salary cut ho gayi hai kuch mahine se, EMI bhar nahi pa raha."]),
    "stall": ("no_reason", ["Baad me karunga, abhi mood nahi hai.",
                            "Dekhte hain, jab hoga tab dunga.",
                            "Abhi nahi de sakta, baad me baat karo."]),
    "fake_dispute": ("dispute", ["Ye charge galat hai, mera dispute chal raha hai, jab tak solve nahi hota nahi dunga.",
                                 "Maine complaint dali hai, is loan pe dispute hai."]),
    "dispute": ("dispute", ["Mere account se EMI do baar kat gayi hai, pehle wo refund karo.",
                            "Double debit hua tha, maine bank me complaint ki hai."]),
    "dispute_vague": ("dispute", ["Mere charges me kuch gadbad hai, dispute hai.",
                                  "Bank ne kuch galat kata hai mera."]),
    "vague": ("unclear", ["Dekhte hain sir, baad me baat karte hain.",
                          "Abhi pata nahi, kuch bol nahi sakta.",
                          "Haan haan, dekhta hoon.",
                          "Network issue hai, awaaz nahi aa rahi."]),
}
SUBS = {"paisa": "pesa", "nahi": "nai", "hai": "he", "kar": "kr", "bhool": "bhul", "gaya": "gya", "gayi": "gai",
        "sir": "sar", "salary": "salry", "job": "jab", "chali": "chalee", "dukaan": "dukan", "kat": "cut",
        "baad": "bad", "dispute": "dispit", "naya": "nya", "hoon": "hu", "double": "dabal"}


def add_months(d, k):
    y, m = divmod(d.year * 12 + d.month - 1 + k, 12)
    return date(y, m + 1, min(d.day, 28))


def asr_noise(text, r, p=0.12):
    """Mimic speech-to-text errors: spelling variants, dropped words, swapped letters."""
    out = []
    for w in text.split(" "):
        core = w.strip(".,?!")
        low = core.lower()
        x = r.random()
        if low in SUBS and x < p * 2.5:
            w = w.replace(core, SUBS[low])
        elif x < p * 0.4:
            continue
        elif len(core) > 4 and x < p * 0.8:
            i = r.randint(1, len(core) - 3)
            swapped = core[:i] + core[i + 1] + core[i] + core[i + 2:]
            w = w.replace(core, swapped)
        out.append(w)
    return " ".join(out)


# ---------------------------------------------------------------- builders
def build_repayments(acct, emi, latest_due, cfg, r):
    late_n, miss_n = r.randint(*cfg["late"]), r.randint(*cfg["missed"])
    dues = [add_months(latest_due, -k) for k in range(5, -1, -1)]
    if cfg.get("mixed_repay"):
        st = [r.choice(["paid", "paid", "late", "late", "missed"]) for _ in range(5)] + ["missed"]
    else:
        st = ["paid"] * (6 - late_n - miss_n) + ["late"] * late_n + ["missed"] * miss_n
    rows = []
    for d, s in zip(dues, st):
        if s == "paid":
            paid = d + timedelta(days=r.randint(-2, 1))
        elif s == "late":
            paid = d + timedelta(days=r.randint(5, 15))
        else:
            paid = None
        rows.append(dict(account_id=acct, due_date=d, paid_date=paid,
                         amount_paid=emi if paid else 0, status=s))
    first_missed = next(d for d, s in zip(dues, st) if s == "missed")
    return rows, (AS_OF - first_missed).days


def build_statement(acct, emi, salary, repay, cfg, dpd, r):
    """Full 12-month statement. Returns (rows, meta)."""
    income = cfg["income"]
    months = [add_months(AS_OF.replace(day=1), -k) for k in range(11, -1, -1)]
    emp, sal_day = r.choice(EMPLOYERS), r.randint(1, 5)
    # income trouble starts around when EMIs started failing
    months_in = round(dpd / 30)
    stop_ago = max(2, min(5, months_in + r.choice([0, 1])))
    drop_n, drop_factor = max(2, min(3, months_in)), r.uniform(0.3, 0.45)
    t = []  # (date, kind, narration, amount, type)

    for idx, m in enumerate(months):
        sal_narr = r.choice([f"NEFT-SAL-{emp}-{m:%b}", f"SAL CR {emp} {m:%m%y}", f"SALARY {emp}"])
        if income == "steady" or (income == "stopped" and idx <= 11 - stop_ago):
            t.append((m.replace(day=sal_day), "credit", sal_narr, salary, "credit"))
        elif income == "dropped":
            amt = salary if idx <= 11 - drop_n else round(salary * drop_factor, -2)
            t.append((m.replace(day=sal_day), "credit", sal_narr, amt, "credit"))
        elif income == "stopped" and r.random() < 0.4:
            t.append((m.replace(day=r.randint(8, 25)), "credit", "UPI/CR/FAMILY TRANSFER",
                      r.randrange(2000, 5001, 500), "credit"))
        elif income == "irregular":
            for _ in range(r.choice([0, 1, 1, 2, 3])):
                narr = r.choice(["UPI/CR/{n}/FREELANCE", "NEFT CR CLIENT PAYMENT", "IMPS CR {n}", "CASH DEP BRANCH"])
                t.append((m.replace(day=r.randint(1, 28)), "credit", narr.format(n=r.randint(10**8, 10**9 - 1)),
                          r.randrange(3000, 40001, 500), "credit"))

        t.append((m.replace(day=5), "gen", f"NACH DR RENT {r.choice(['LANDLORD', 'HOUSING SOC'])}",
                  round(salary * 0.18, -2), "debit"))
        # living costs scale with salary, so people do not pile up savings (spend ~ income)
        scale = r.uniform(0.05, 0.08) if cfg["spend"] == "big" else r.uniform(0.07, 0.12)
        for _ in range(r.randint(6, 9)):
            t.append((m.replace(day=r.randint(1, 28)), "gen",
                      f"UPI/DR/{r.randint(10**8, 10**9 - 1)}/{r.choice(MERCHANTS)}", round(salary * scale, -1), "debit"))
        if r.random() < 0.8:
            t.append((m.replace(day=r.randint(1, 28)), "gen", "ATM WDL",
                      round(salary * r.uniform(0.03, 0.06), -2), "debit"))
        if cfg["spend"] == "big":
            for _ in range(r.randint(1, 3)):
                t.append((m.replace(day=r.randint(1, 28)), "gen", f"POS/{r.choice(BIG_SPEND)}",
                          round(salary * r.uniform(0.08, 0.25), -2), "debit"))

    dd = None
    for row in repay:
        if row["paid_date"]:
            t.append((row["paid_date"], "emi", f"ACH DR LOAN EMI {acct}", emi, "debit"))
    if cfg["dispute"]:
        last_paid = max(x["paid_date"] for x in repay if x["paid_date"])
        dd = last_paid
        t.append((dd, "dup", f"ACH DR LOAN EMI {acct} REF{r.randint(10000, 99999)}", emi, "debit"))
        t.append((dd + timedelta(days=4), "credit", f"REVERSAL EMI DEBIT {acct}", emi, "credit"))

    t = [x for x in t if x[0] <= AS_OF]
    t.sort(key=lambda x: (x[0], 0 if x[4] == "credit" else 1))
    bal = emi * (r.uniform(2, 4) if cfg["buffer"] else r.uniform(1.5, 3))
    buffer = emi * cfg["buffer"]
    rows = []
    for d, kind, narr, amt, typ in t:
        if typ == "credit":
            bal += amt
        else:
            if kind == "gen" and bal - amt < buffer:
                continue
            bal = max(0.0, bal - amt)
        rows.append(dict(txn_date=d, narration=narr, amount=round(amt, 2), type=typ, balance=round(bal, 2)))
    return rows, dict(stop_ago=stop_ago, drop_pct=round((1 - drop_factor) * 100), dd=dd.isoformat() if dd else "")


def build_calls(acct, cfg, counter, r):
    pool_name = r.choice(cfg["calls"])
    tag, lines = POOLS[pool_name]
    n = r.randint(1, 2) if cfg["label"] == "insufficient" else r.randint(1, 4)
    dates = sorted(AS_OF - timedelta(days=r.randint(1, 60)) for _ in range(n))
    rows = []
    for d in dates:
        counter[0] += 1
        reply = asr_noise(r.choice(lines), r)
        close = asr_noise(r.choice(CLOSERS.get(pool_name, GENERIC_CLOSE)), r)
        transcript = (f"Collector: {r.choice(OPENERS)} Customer: {reply} "
                      f"Collector: {r.choice(FOLLOWUPS)} Customer: {close}")
        rows.append(dict(call_id=f"C-{counter[0]:06d}", account_id=acct, call_date=d,
                         agent_id=f"A-{r.randint(1, 25):02d}", transcript=transcript,
                         stated_reason_raw="" if r.random() < 0.3 else tag))
    return rows


def build_promises(acct, emi, cfg, counter, r):
    lo, hi, kept_p = cfg["ptp"]
    n = r.randint(lo, hi)
    kept = [1, 1, 0, 0] if cfg.get("mixed_ptp") else [1 if r.random() < kept_p else 0 for _ in range(n)]
    r.shuffle(kept)
    dates = sorted(AS_OF - timedelta(days=r.randint(5, 70)) for _ in range(n))
    rows = []
    for d, k in zip(dates, kept):
        counter[0] += 1
        amt = emi if r.random() < 0.6 else round(emi * r.uniform(0.3, 0.7), -2)
        rows.append(dict(ptp_id=f"P-{counter[0]:06d}", account_id=acct, promised_date=d,
                         promised_amount=amt, kept=k))
    return rows


def bucket_of(dpd):
    return "Bucket 1" if dpd <= 30 else "Bucket 2" if dpd <= 60 else "Bucket 3" if dpd <= 90 else "NPA"


# ---------------------------------------------------------------- main
FILES = {
    "accounts": ["account_id", "customer_name", "product", "branch", "loan_amount", "emi_amount",
                 "due_date", "dpd", "bucket", "outstanding", "status"],
    "repayments": ["account_id", "due_date", "paid_date", "amount_paid", "status"],
    "calls": ["call_id", "account_id", "call_date", "agent_id", "transcript", "stated_reason_raw"],
    "promises": ["ptp_id", "account_id", "promised_date", "promised_amount", "kept"],
    "statement_txns": ["account_id", "pull_no", "txn_date", "narration", "amount", "type", "balance"],
    "gold_labels": ["account_id", "expert_label", "rationale", "is_ambiguous", "split"],
}


def write_csv(out_dir, name, rows):
    with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FILES[name])
        w.writeheader()
        for row in rows:
            w.writerow({k: (v.isoformat() if isinstance(v, date) else v) for k, v in row.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()
    r = random.Random(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    order = [name for name, c in SCENARIOS.items() for _ in range(c["n"])]
    r.shuffle(order)
    data = {k: [] for k in FILES}
    call_ctr, ptp_ctr, seq = [0], [0], 3900

    for scen in order:
        cfg = SCENARIOS[scen]
        seq += r.randint(1, 6)
        acct = f"005{seq:08d}"
        salary = r.randrange(25000, 90001, 500)
        emi = round(salary * r.uniform(0.12, 0.3), -2)
        latest_due = AS_OF - timedelta(days=r.randint(2, 25))

        repay, dpd = build_repayments(acct, emi, latest_due, cfg, r)
        stmt, meta = build_statement(acct, emi, salary, repay, cfg, dpd, r)
        calls = build_calls(acct, cfg, call_ctr, r)
        ptps = build_promises(acct, emi, cfg, ptp_ctr, r)

        thin = r.random() < 0.5 if cfg["thin"] == "half" else cfg["thin"]
        pull1 = [x for x in stmt if x["txn_date"] >= AS_OF - timedelta(days=30)] if thin else stmt
        data["statement_txns"] += [dict(account_id=acct, pull_no=1, **x) for x in pull1]
        if thin or cfg["label"] == "insufficient":  # fresh pull exists
            data["statement_txns"] += [dict(account_id=acct, pull_no=2, **x) for x in stmt]

        data["accounts"].append(dict(
            account_id=acct, customer_name=f"{r.choice(FIRST)} {r.choice(LAST)}", product=r.choice(PRODUCTS),
            branch=r.choice(BRANCHES), loan_amount=emi * r.randint(36, 180), emi_amount=emi,
            due_date=latest_due, dpd=dpd, bucket=bucket_of(dpd), outstanding=emi * r.randint(12, 100), status="delinquent"))
        data["repayments"] += repay
        data["calls"] += calls
        data["promises"] += ptps

        ctx = dict(meta, salary=int(salary), n_paid=sum(1 for x in repay[:5] if x["status"] != "missed"),
                   n_ptp=len(ptps), n_kept=sum(p["kept"] for p in ptps))
        data["gold_labels"].append(dict(
            account_id=acct, expert_label=cfg["label"], rationale=RATIONALE[scen].format(**ctx),
            is_ambiguous=1 if cfg["label"] == "insufficient" else 0, split="public"))

    for name, rows in data.items():
        write_csv(out_dir, name, rows)
        print(f"{name:15s} {len(rows):6d} rows")
    print(f"\nWritten to {out_dir}")


if __name__ == "__main__":
    main()
