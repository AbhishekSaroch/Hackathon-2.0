"""
Data tools + evidence features for one account (BI-02).

Two layers:
  1. Getters (get_repayments, get_calls, get_promises, get_statement, request_fresh_statement)
     -> raw rows from the DB, always tenant-scoped via db.py
  2. build_evidence_pack(): Python computes the numbers (salary credits, income drop,
     balance on due date, double debit, promise ratio ...). The LLM only READS this pack
     plus the raw call transcripts. No customer name / protected attribute is included.

Quick test:  python account_tools.py <account_id> [pull_no]
"""
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

SALARY_RE = re.compile(r"\bSAL(ARY)?\b", re.I)
DISCRETIONARY_RE = re.compile(r"POS/|AMAZON|CROMA|MAKEMYTRIP|APPLE|BOOKMYSHOW|TANISHQ", re.I)
EMI_RE = re.compile(r"LOAN EMI", re.I)
REVERSAL_RE = re.compile(r"REVERSAL|REFUND", re.I)
THIN_DAYS = 120  # a statement covering fewer days than this is "thin"


# ------------------------------------------------------------------ getters (tools)
def get_account(tenant: str, account_id: str) -> dict | None:
    return db.query_one(
        tenant,
        "SELECT account_id, product, loan_amount, emi_amount, due_date, dpd, bucket, outstanding "
        "FROM accounts WHERE tenant_id=:tenant_id AND account_id=:account_id",
        {"account_id": account_id})


def get_repayments(tenant: str, account_id: str) -> list[dict]:
    return db.query(
        tenant,
        "SELECT id AS repay_id, due_date, paid_date, amount_paid, status FROM repayments "
        "WHERE tenant_id=:tenant_id AND account_id=:account_id ORDER BY due_date",
        {"account_id": account_id})


def get_calls(tenant: str, account_id: str) -> list[dict]:
    return db.query(
        tenant,
        "SELECT call_id, call_date, transcript, stated_reason_raw FROM calls "
        "WHERE tenant_id=:tenant_id AND account_id=:account_id ORDER BY call_date",
        {"account_id": account_id})


def get_promises(tenant: str, account_id: str) -> list[dict]:
    return db.query(
        tenant,
        "SELECT ptp_id, promised_date, promised_amount, kept FROM promises "
        "WHERE tenant_id=:tenant_id AND account_id=:account_id ORDER BY promised_date",
        {"account_id": account_id})


def get_statement(tenant: str, account_id: str, pull_no: int = 1) -> list[dict]:
    return db.query(
        tenant,
        "SELECT id AS txn_id, txn_date, narration, amount, type, balance FROM statement_txns "
        "WHERE tenant_id=:tenant_id AND account_id=:account_id AND pull_no=:pull_no "
        "ORDER BY txn_date, id",
        {"account_id": account_id, "pull_no": pull_no})


def request_fresh_statement(tenant: str, account_id: str) -> dict:
    """Mock Account Aggregator pull. Says whether the fresh pull actually has new information."""
    old, fresh = get_statement(tenant, account_id, 1), get_statement(tenant, account_id, 2)
    if len(fresh) > len(old):
        return {"pull_no": 2, "new_data": True, "txn_count": len(fresh), "previous_txn_count": len(old)}
    return {"pull_no": 1, "new_data": False, "txn_count": len(old), "previous_txn_count": len(old)}


# ------------------------------------------------------------------ features
def _d(s):
    return date.fromisoformat(s) if s else None


def _month_keys(start: date, end: date) -> list[tuple]:
    y, m, keys = start.year, start.month, []
    while (y, m) <= (end.year, end.month):
        keys.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return keys


def repayment_features(rows: list[dict]) -> dict:
    st = [r["status"] for r in rows]
    missed = [r for r in rows if r["status"] == "missed"]
    tail = 0
    for s in reversed(st):
        if s != "missed":
            break
        tail += 1
    return {
        "n_emis": len(rows), "n_paid_on_time": st.count("paid"), "n_paid_late": st.count("late"),
        "n_missed": len(missed), "consecutive_missed_at_end": tail,
        "first_missed_due": missed[0]["due_date"] if missed else None,
        # first ever miss after a clean record
        "perfect_history_before_miss": len(missed) == 1 and st.count("late") == 0 and st[-1] == "missed",
    }


def promise_features(rows: list[dict]) -> dict:
    n = len(rows)
    kept = sum(1 for r in rows if r["kept"] == 1)
    return {"n_promises": n, "n_kept": kept, "n_broken": n - kept,
            "kept_ratio": round(kept / n, 2) if n else None}


def statement_features(rows: list[dict], emi: float, first_missed_due: str | None, as_of: date) -> dict:
    if not rows:
        return {"available": False, "is_thin": True, "reason": "no transactions in this statement pull"}
    dates = [_d(r["txn_date"]) for r in rows]
    start, end = min(dates), max(dates)
    coverage = (end - start).days
    credits = [r for r in rows if r["type"] == "credit" and not REVERSAL_RE.search(r["narration"])]
    salary = [r for r in credits if SALARY_RE.search(r["narration"])]

    def monthly_rate(items, w_from, w_to):
        total = sum(r["amount"] for r in items if w_from <= _d(r["txn_date"]) < w_to)
        return total / max((w_to - w_from).days / 30, 1)

    recent_from, recent_to = as_of - timedelta(days=60), as_of + timedelta(days=1)
    base_from, base_to = max(start, as_of - timedelta(days=360)), as_of - timedelta(days=90)
    has_base = (base_to - base_from).days >= 90

    def drop_pct(items):
        if not has_base:
            return None
        base = monthly_rate(items, base_from, base_to)
        if base <= 0:
            return None
        return max(0, min(100, round(100 * (1 - monthly_rate(items, recent_from, recent_to) / base))))

    monthly = {k: 0.0 for k in _month_keys(start, end)}
    for r in credits:
        d = _d(r["txn_date"])
        monthly[(d.year, d.month)] += r["amount"]
    vals = list(monthly.values())
    income_cv = round(pstdev(vals) / mean(vals), 2) if len(vals) >= 3 and mean(vals) > 0 else None

    last_salary = max((_d(r["txn_date"]) for r in salary), default=None)
    bal_row = None
    if first_missed_due:
        before = [r for r in rows if _d(r["txn_date"]) <= _d(first_missed_due)]
        bal_row = before[-1] if before else None
    bal = bal_row["balance"] if bal_row else None

    emi_debits = [r for r in rows if r["type"] == "debit" and EMI_RE.search(r["narration"])]
    by_day: dict[str, list] = {}
    for r in emi_debits:
        by_day.setdefault(r["txn_date"], []).append(r)
    double = [{"date": d, "txn_ids": [x["txn_id"] for x in v]} for d, v in by_day.items() if len(v) >= 2]
    fm = _d(first_missed_due)
    dd_gaps = [(fm - _d(x["date"])).days for x in double] if fm else []
    dd_before = any(0 <= g <= 90 for g in dd_gaps) if fm else None
    reversals = [{"txn_id": r["txn_id"], "date": r["txn_date"], "amount": r["amount"]}
                 for r in rows if r["type"] == "credit" and REVERSAL_RE.search(r["narration"])]

    d90 = as_of - timedelta(days=90)
    disc = [r for r in rows if r["type"] == "debit" and DISCRETIONARY_RE.search(r["narration"]) and _d(r["txn_date"]) >= d90]
    inc90 = sum(r["amount"] for r in credits if _d(r["txn_date"]) >= d90)

    return {
        "available": True, "start": start.isoformat(), "end": end.isoformat(),
        "coverage_days": coverage, "is_thin": coverage < THIN_DAYS, "n_txns": len(rows),
        "salary_found": bool(salary),
        "last_salary_date": last_salary.isoformat() if last_salary else None,
        "months_since_last_salary": round((as_of - last_salary).days / 30, 1) if last_salary else None,
        "last_salary_credits": [{"txn_id": r["txn_id"], "date": r["txn_date"], "amount": r["amount"]}
                                for r in sorted(salary, key=lambda r: r["txn_date"])[-3:]],
        "salary_drop_pct": drop_pct(salary),
        "income_drop_pct": drop_pct(credits),
        "income_variability": income_cv,  # high (> ~1) = irregular income
        "income_last_90d": round(inc90),
        "balance_on_first_missed_due": bal,
        "emi_covered_on_due_date": (bal >= emi) if bal is not None else None,
        "current_balance": rows[-1]["balance"],
        "current_balance_covers_emi": rows[-1]["balance"] >= emi,
        # did the customer's misses start right after a double debit? (supports a real dispute)
        "double_debit_before_first_miss": dd_before,
        "days_from_double_debit_to_first_miss": min((g for g in dd_gaps if g >= 0), default=None),
        "double_debit": double, "reversal_credits": reversals,
        "discretionary_spend_90d": round(sum(r["amount"] for r in disc)),
        "discretionary_share_of_income": round(sum(r["amount"] for r in disc) / inc90, 2) if inc90 else None,
    }


# ------------------------------------------------------------------ evidence pack
def build_evidence_pack(tenant: str, account_id: str, pull_no: int = 1) -> dict | None:
    acct = get_account(tenant, account_id)
    if not acct:
        return None
    rep = get_repayments(tenant, account_id)
    stm = get_statement(tenant, account_id, pull_no)
    calls = get_calls(tenant, account_id)
    ptp = get_promises(tenant, account_id)

    rep_f = repayment_features(rep)
    as_of = max((_d(r["txn_date"]) for r in stm), default=None) \
        or max((_d(r["due_date"]) for r in rep), default=date.today())
    return {
        "account": acct,
        "as_of": as_of.isoformat(),
        "statement_pull_no": pull_no,
        "repayment": rep_f,
        "promises": promise_features(ptp),
        "statement": statement_features(stm, acct["emi_amount"], rep_f["first_missed_due"], as_of),
        "calls": calls,  # raw transcripts: the LLM reads these
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python account_tools.py <account_id> [pull_no]")
    pack = build_evidence_pack("abc_bank", sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    print(json.dumps(pack, indent=2, ensure_ascii=False, default=str))
