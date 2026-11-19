"""
BI-02 agent: assess one delinquent account.

Loop (the agentic part):
  1. gather   -> evidence pack (Python numbers) from the DB tools
  2. extract  -> LLM reads transcripts: what does the customer SAY?
  3. decide   -> LLM tests that claim against the statement, gives label + confidence + cited evidence
  4. more data? if statement is thin or confidence is low -> request a fresh statement (once) -> decide again
  5. abstain  -> if still not confident, label = insufficient

Guardrails in code: evidence refs must exist in the data we gave (no invented citations),
confidence is clamped, abstain threshold is applied by code, no customer name / protected attribute is sent.

Quick test:  python decide.py <account_id>
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "tools"), str(HERE.parents[1] / "llm"), str(HERE)]
import account_tools as tools  # noqa: E402
import extract  # noqa: E402
import groq_client  # noqa: E402

LABELS = ["forgot", "cant_pay", "wont_pay", "disputed", "insufficient"]
PULL_BELOW = 0.70     # below this confidence the agent tries to get more data
ABSTAIN_BELOW = 0.55  # still below this after re-check -> "insufficient"

SYSTEM = """You are a collections analyst. Decide WHY a delinquent bank customer is not paying their EMI.
You get: repayment history, promise history, bank-statement facts (computed by code, trust the numbers),
and what the customer SAID on calls (extracted already). Test the customer's claim against the statement.

Labels:
- forgot: first miss after a clean record (perfect_history_before_miss), money is available, customer says they forgot.
- cant_pay: real inability. Income stopped or fell materially (salary_drop_pct >= 40, or no recent salary) AND the
  current balance does not cover the EMI (current_balance_covers_emi false). Some salary may still arrive, a
  big drop with a low balance is still cant_pay. Promises broken for lack of money.
- wont_pay: money or income is available (salary unchanged, balance covers the EMI, discretionary spending) but the
  customer does not pay. This includes false claims: "no job" while full salary keeps coming, a "dispute" with
  no double debit in the statement, or stalling with no reason.
- disputed: the customer cites a dispute / double debit AND the statement backs it: double_debit is present and
  double_debit_before_first_miss is true (misses started right after it). A reversal credit does not cancel this,
  the dispute is still the reason they stopped paying.
- insufficient: evidence is missing, thin, irregular or still contradictory, you cannot tell.

Rules:
- A claim you cannot test (statement thin or unavailable) is unverified: confidence must be 0.5 or lower and set needs_fresh_statement=true.
- If the customer gave no reason (no_reason_given / unclear) AND income is irregular (income_variability above 0.6)
  or no salary exists, you cannot tell why they are not paying: label insufficient, confidence 0.5 or lower.
- Calibrate confidence. 0.85 or more only when at least three independent facts agree and nothing contradicts.
  If any important fact points the other way, stay at 0.7 or below.
- Do not use or guess age, gender, religion, caste or location. Only the data given.
- Cite only evidence that is in the data. ref must be: for D5 one call_id (like C-000123); for D8 a txn_id given in the
  statement facts OR the exact name of a statement fact (like salary_drop_pct); for D3 "repayments" or a repayment fact
  name; for D6 "promises" or a promise fact name. Never invent ids.
- confidence is 0 to 1: how sure you are of the label given the evidence.

Return JSON:
{"label": "<one label>", "confidence": 0.0,
 "claim_vs_evidence": "consistent" | "contradicted" | "unverifiable" | "no_claim",
 "needs_fresh_statement": true|false,
 "reasoning": "2 short sentences",
 "evidence": [{"source": "D3"|"D5"|"D6"|"D8", "ref": "<allowed ref>", "text": "short fact, with the number", "points_to": "<label it supports>"}]}
Give 3 to 5 evidence items."""

SOURCE_REFS = {"D3": {"repayments"}, "D6": {"promises"}}


def _allowed_refs(pack: dict) -> dict[str, set]:
    s = pack["statement"]
    txn_ids = {str(x["txn_id"]) for x in s.get("last_salary_credits", [])}
    txn_ids |= {str(i) for d in s.get("double_debit", []) for i in d["txn_ids"]}
    txn_ids |= {str(x["txn_id"]) for x in s.get("reversal_credits", [])}
    return {"D5": {c["call_id"] for c in pack["calls"]},
            "D8": txn_ids | {"statement_summary"} | set(s),
            "D3": {"repayments"} | set(pack["repayment"]), "D6": {"promises"} | set(pack["promises"])}


def _slim(pack: dict, stated: dict, fresh: bool) -> str:
    a = pack["account"]
    return json.dumps({
        "account": {k: a[k] for k in ("product", "emi_amount", "dpd", "bucket", "outstanding")},
        "as_of": pack["as_of"], "statement_is_fresh_pull": fresh,
        "repayments_D3": pack["repayment"], "promises_D6": pack["promises"],
        "statement_D8": pack["statement"],
        "customer_statements_D5": [{k: s[k] for k in ("call_id", "call_date", "category", "claim", "quote")}
                                   for s in stated["statements"]],
    }, ensure_ascii=False, default=str)


def decide(pack: dict, stated: dict, fresh: bool = False) -> dict:
    raw = groq_client.chat_json(SYSTEM, _slim(pack, stated, fresh), model=groq_client.MODEL_SMART, reasoning="medium")
    allowed, evidence, dropped = _allowed_refs(pack), [], 0
    for e in raw.get("evidence", []):
        if isinstance(e, dict) and str(e.get("ref")) in allowed.get(e.get("source"), set()):
            evidence.append({"source": e["source"], "ref": str(e["ref"]), "text": str(e.get("text", "")),
                             "points_to": e.get("points_to", "")})
        else:
            dropped += 1
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    label = raw.get("label") if raw.get("label") in LABELS else "insufficient"
    # code backstop: no stated reason + irregular / no income = cannot tell why, never be confident
    s = pack["statement"]
    no_story = stated["overall_category"] in ("unclear", "no_reason_given", "no_calls")
    if no_story and s.get("available") and (not s["salary_found"] or (s["income_variability"] or 0) > 0.6):
        conf = min(conf, 0.5)
    return {"label": label, "confidence": conf, "claim_vs_evidence": raw.get("claim_vs_evidence", "unverifiable"),
            "needs_fresh_statement": bool(raw.get("needs_fresh_statement")),
            "reasoning": str(raw.get("reasoning", "")), "evidence": evidence, "dropped_evidence": dropped}


def _facts(pack: dict) -> dict:
    """Plain numbers from the data (not from the LLM) used later to write the Hinglish note."""
    s, r, p, a = pack["statement"], pack["repayment"], pack["promises"], pack["account"]
    keys = ("current_balance", "current_balance_covers_emi", "emi_covered_on_due_date", "salary_drop_pct",
            "months_since_last_salary", "salary_found", "double_debit", "double_debit_before_first_miss")
    return {"emi_amount": a["emi_amount"], "n_missed": r["n_missed"], "n_paid_on_time": r["n_paid_on_time"],
            "n_promises": p["n_promises"], "n_kept": p["n_kept"], **{k: s.get(k) for k in keys}}


def assess_account(tenant: str, account_id: str) -> dict:
    steps: list[dict] = []
    t0 = time.time()

    def log(tool, inp, out):
        steps.append({"step_no": len(steps) + 1, "tool": tool, "input": inp, "output": out})
        print(f"[bi02 {account_id}] step {len(steps)} done: {tool} (+{time.time() - t0:.1f}s)", flush=True)

    pack = tools.build_evidence_pack(tenant, account_id, pull_no=1)
    if pack is None:
        raise ValueError(f"unknown account {account_id}")
    s = pack["statement"]
    log("get_evidence_pack", {"account_id": account_id, "pull_no": 1},
        {"statement_thin": s["is_thin"], "statement_available": s["available"],
         "n_calls": len(pack["calls"]), "n_missed": pack["repayment"]["n_missed"]})

    stated = extract.extract_stated_reasons(pack["calls"])
    log("extract_stated_reasons", {"n_calls": len(pack["calls"])},
        {"overall": stated["overall_category"], "categories": stated["categories"]})

    d = decide(pack, stated)
    log("decide", {"pull_no": 1}, {"label": d["label"], "confidence": d["confidence"],
                                   "claim_vs_evidence": d["claim_vs_evidence"]})

    pulled, used_pull = False, 1
    if d["needs_fresh_statement"] or d["confidence"] < PULL_BELOW or s["is_thin"]:
        fr = tools.request_fresh_statement(tenant, account_id)
        log("request_fresh_statement", {"account_id": account_id}, fr)
        if fr["new_data"]:
            pulled, used_pull = True, fr["pull_no"]
            pack = tools.build_evidence_pack(tenant, account_id, pull_no=used_pull)
            d = decide(pack, stated, fresh=True)
            log("decide", {"pull_no": used_pull, "re_assess": True},
                {"label": d["label"], "confidence": d["confidence"], "claim_vs_evidence": d["claim_vs_evidence"]})

    raw_label, abstained = d["label"], False
    if d["label"] == "insufficient" or d["confidence"] < ABSTAIN_BELOW:
        d["label"], abstained = "insufficient", True
    log("finalize", {"raw_label": raw_label, "confidence": d["confidence"]},
        {"label": d["label"], "abstained": abstained})

    return {"account_id": account_id, "label": d["label"], "raw_label": raw_label, "confidence": d["confidence"],
            "abstained": abstained, "claim_vs_evidence": d["claim_vs_evidence"], "reasoning": d["reasoning"],
            "evidence": d["evidence"], "dropped_evidence": d["dropped_evidence"],
            "stated": stated, "pulled_fresh_statement": pulled, "statement_pull_used": used_pull,
            "facts": _facts(pack),
            "as_of": pack["as_of"], "steps": steps}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python decide.py <account_id>")
    out = assess_account("abc_bank", sys.argv[1])
    out["stated"] = {k: out["stated"][k] for k in ("overall_category", "categories")}
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
