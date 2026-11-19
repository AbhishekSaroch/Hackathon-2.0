"""
BI-02 step "act": turn an assessment into a next-action recommendation and save everything.

  1. next_action()      -> PYTHON RULES pick the channel (so the AI can never choose a risky action)
  2. explain()          -> manager-facing note in plain ENGLISH. Customer quotes stay verbatim (their own words).
                           Built in code from the real numbers: we tried LLM-written notes and they
                           sometimes flipped facts (said the balance was below the EMI when it was not),
                           which is unacceptable in collections. The AI's job is label + evidence; text is templated.
  3. save()             -> writes agent_runs, agent_steps, assessments, evidence, recommendations

Nothing is sent to the customer here. The recommendation waits as 'pending' for a human to approve.

Quick test:  python act.py <account_id>
"""
import json
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "db"), str(HERE.parents[1] / "llm")]
import db  # noqa: E402
import decide  # noqa: E402

FIELD_VISIT_MIN_DPD = 90
FIELD_VISIT_MIN_OUTSTANDING = 500_000
FIELD_VISIT_MIN_CONF = 0.8
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


# ------------------------------------------------------------------ 1. rules
def next_action(result: dict, account: dict, status: str) -> dict:
    label, conf = result["label"], result["confidence"]
    guard = []

    if status == "current":  # BI-03 territory: a current account only ever gets a service nudge
        return {"channel": "whatsapp", "rule": "current_account_service_nudge",
                "action": "Send only a service nudge on WhatsApp. No pressure about dues.",
                "guardrails": ["Current account: no calls or field visits"]}

    if label == "forgot":
        ch, act, rule = "whatsapp", "Send a friendly WhatsApp reminder, with no pressure about dues.", "forgot_reminder"
    elif label == "cant_pay":
        ch, act, rule = "telecall", "Collector to call and offer an easy payment plan or EMI restructure.", "cant_pay_restructure"
    elif label == "wont_pay":
        big = account["dpd"] >= FIELD_VISIT_MIN_DPD and account["outstanding"] >= FIELD_VISIT_MIN_OUTSTANDING
        if big and conf >= FIELD_VISIT_MIN_CONF:
            ch, act, rule = "field_visit", "Assign a field visit, along with a call from a senior collector.", "wont_pay_high_value_field_visit"
        else:
            ch, act, rule = "telecall", "Senior collector to call and point out the gap between what the customer says and what the statement shows.", "wont_pay_senior_call"
            if big:
                guard.append(f"No field visit: confidence {conf:.2f} is below {FIELD_VISIT_MIN_CONF}")
    elif label == "disputed":
        ch, act, rule = "dispute_team", "Send to the dispute team and pause collection calls until the dispute is resolved.", "disputed_pause_collection"
        guard.append("No collection calls or field visits while a dispute is open")
    else:  # insufficient / abstained
        ch, act, rule = "human", "Collector to talk to the customer directly and ask the reason, because the data was not conclusive.", "insufficient_human_review"
    return {"channel": ch, "action": act, "rule": rule, "guardrails": guard}


# ------------------------------------------------------------------ 2. English text (templates + real numbers)
ACTION_WHY = {
    "forgot_reminder": "The customer's record is clean, so a light reminder is enough.",
    "cant_pay_restructure": "The customer's income has dropped, so an easy plan will work better than pressure.",
    "wont_pay_senior_call": "The money is there but the EMI is not coming, so a senior collector should speak to the customer.",
    "wont_pay_high_value_field_visit": "The loan is large and old and the customer has the money, so a field visit is justified.",
    "disputed_pause_collection": "A dispute is open, so no collection pressure until it is resolved.",
    "insufficient_human_review": "The data could not explain the reason, so a collector should ask the customer directly.",
    "current_account_service_nudge": "The customer is still current, so only a service nudge.",
}
ABSTAIN_TEXT = "Not enough evidence, or the evidence conflicts, so no label was given."


def _rs(x) -> str:
    return f"Rs {x:,.0f}"


def _date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b}"


def _balance(f: dict) -> str:
    """Direction (above / below) comes from the data, never from a guess."""
    rel = "above" if f["current_balance_covers_emi"] else "below"
    return f"the balance is {_rs(f['current_balance'])}, which is {rel} the EMI of {_rs(f['emi_amount'])}"


def _promises(f: dict) -> str:
    return f"{f['n_kept']} of {f['n_promises']} promises kept" if f["n_promises"] else "No promises on record"


def _said(result: dict) -> str:
    q = next((s["quote"] for s in reversed(result["stated"]["statements"]) if s["quote"] and s["quote_verified"]), "")
    q = q.strip().rstrip(".,!?")
    return f'Customer said: "{q}". ' if q else ""


def explain(result: dict, rec: dict) -> dict:
    f, label, cats = result["facts"], result["label"], result["stated"]["categories"]
    note = ACTION_WHY[rec["rule"]]
    if result["abstained"] or label == "insufficient":
        return {"reason": ABSTAIN_TEXT, "action_note": note, "source": "template"}
    if label == "forgot":
        body = (f"First miss; the previous {f['n_paid_on_time']} EMIs were paid on time and {_balance(f)}. "
                "Looks like the customer simply forgot.")
    elif label == "cant_pay":
        if f.get("salary_drop_pct") is not None and f["salary_drop_pct"] > 0 and f.get("salary_found"):
            inc = f"Salary dropped {f['salary_drop_pct']}%"
        else:
            m = f.get("months_since_last_salary")
            inc = f"No salary for {m} months" if m is not None else "Income has stopped or is very low"
        body = f"{inc} and {_balance(f)}. {_promises(f)}."
    elif label == "wont_pay":
        if "income_loss" in cats:
            claim = "The customer claimed loss of income, but the statement shows otherwise. "
        elif "dispute" in cats:
            claim = "The customer claimed a dispute, but no double debit was found in the statement. "
        elif "no_reason_given" in cats:
            claim = "The customer gave no reason. "
        else:
            claim = ""
        body = (f"{claim}Salary is still coming and {_balance(f)}, yet {f['n_missed']} EMIs were missed. "
                f"{_promises(f)}.")
    else:  # disputed
        dd = f["double_debit"][0]["date"] if f.get("double_debit") else None
        body = ((f"The statement shows the EMI was debited twice on {_date(dd)}, and the misses started after that. " if dd else "")
                + "The customer's dispute looks genuine.")
    return {"reason": _said(result) + body, "action_note": note, "source": "template"}


# ------------------------------------------------------------------ 3. write-back
def save(tenant: str, result: dict, rec: dict, text: dict) -> dict:
    aid, now = result["account_id"], datetime.now().isoformat(timespec="seconds")
    run_id = "bi02-" + uuid.uuid4().hex[:10]
    db.insert(tenant, "agent_runs", {"run_id": run_id, "agent_name": "bi02", "entity_id": aid,
                                     "status": "done", "finished_at": now})
    for s in result["steps"]:
        db.insert(tenant, "agent_steps", {
            "run_id": run_id, "step_no": s["step_no"], "tool": s["tool"],
            "input": json.dumps(s["input"], ensure_ascii=False, default=str),
            "output": json.dumps(s["output"], ensure_ascii=False, default=str)})
    assessment_id = db.insert(tenant, "assessments", {
        "account_id": aid, "run_id": run_id, "label": result["label"],
        "confidence": round(result["confidence"], 3), "reasoning_summary": text["reason"], "as_of_date": result["as_of"]})
    for e in result["evidence"]:
        db.insert(tenant, "evidence", {"assessment_id": assessment_id, "source": e["source"],
                                       "source_ref": e["ref"], "text": e["text"], "supports": e["points_to"]})
    db.execute(tenant, "UPDATE recommendations SET status='superseded' WHERE tenant_id=:tenant_id "
                       "AND account_id=:a AND source='bi02' AND status='pending'", {"a": aid})
    rec_id = db.insert(tenant, "recommendations", {
        "account_id": aid, "source": "bi02", "source_ref": assessment_id, "channel": rec["channel"],
        "action": rec["action"], "reason": text["action_note"], "status": "pending"})
    return {"run_id": run_id, "assessment_id": assessment_id, "recommendation_id": rec_id}


def run_and_save(tenant: str, account_id: str) -> dict:
    result = decide.assess_account(tenant, account_id)
    acct = db.query_one(tenant, "SELECT dpd, outstanding, status FROM accounts "
                                "WHERE tenant_id=:tenant_id AND account_id=:a", {"a": account_id})
    rec = next_action(result, acct, acct["status"])
    text = explain(result, rec)
    assert not DEVANAGARI.search(text["reason"] + text["action_note"]), "Devanagari in generated text"
    ids = save(tenant, result, rec, text)
    return {**ids, "account_id": account_id, "label": result["label"], "confidence": round(result["confidence"], 2),
            "abstained": result["abstained"], "pulled_fresh_statement": result["pulled_fresh_statement"],
            "recommendation": rec, "text": text, "n_evidence": len(result["evidence"])}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python act.py <account_id>")
    print(json.dumps(run_and_save("abc_bank", sys.argv[1]), indent=2, ensure_ascii=False))
