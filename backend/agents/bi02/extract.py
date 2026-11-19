"""
BI-02 step "extract": read the call transcripts and pull out what the customer SAYS
is the reason for not paying. It does NOT judge if the claim is true (that is the decider's job).

Guardrail in code: every quote must really appear in the transcript (quote_verified),
otherwise it is flagged, so the final label never cites made-up evidence.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "llm"))
import groq_client  # noqa: E402

CATEGORIES = ["forgot", "income_loss", "medical_emergency", "dispute", "no_reason_given", "unclear"]

SYSTEM = """You read call transcripts between a bank collector and a customer who missed an EMI.
The transcripts are Hinglish (Hindi written in Roman letters mixed with English) produced by speech-to-text,
so words can be misspelled (for example 'jab' for 'job', 'sar' for 'sir', 'cut' for 'kat', 'he' for 'hai').

For EACH call, extract only what the CUSTOMER states as the reason for not paying. Do not judge if it is true.

Categories:
- forgot: customer says they forgot or missed it by mistake
- income_loss: lost job, salary stopped or cut, business closed or doing badly
- medical_emergency: illness or family emergency
- dispute: says the charge or debit is wrong, double debit, has a complaint or dispute
- no_reason_given: postpones or refuses without giving a reason (e.g. "baad me karunga", "abhi nahi dunga")
- unclear: vague, off-topic or unintelligible, the reason cannot be told

Return JSON: {"calls": [{"call_id": "...", "category": "<one category>",
 "claim": "one short English sentence of what the customer said",
 "quote": "the shortest exact words copied from the CUSTOMER's lines that show the reason, empty string if none"}]}
Rules: copy the quote verbatim from the transcript (do not fix spelling). One entry per call_id."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()


def extract_stated_reasons(calls: list[dict], model: str | None = None) -> dict:
    if not calls:
        return {"statements": [], "overall_category": "no_calls", "categories": [], "consistent": True,
                "claims_income_loss": False, "claims_dispute": False}

    payload = [{"call_id": c["call_id"], "date": c["call_date"], "transcript": c["transcript"]} for c in calls]
    raw = groq_client.chat_json(SYSTEM, json.dumps(payload, ensure_ascii=False), model=model or groq_client.MODEL_FAST)
    got = {x.get("call_id"): x for x in raw.get("calls", []) if isinstance(x, dict)}

    statements = []
    for c in calls:
        x = got.get(c["call_id"], {})
        cat = x.get("category") if x.get("category") in CATEGORIES else "unclear"
        quote = (x.get("quote") or "").strip()
        statements.append({
            "call_id": c["call_id"], "call_date": c["call_date"], "category": cat,
            "claim": (x.get("claim") or "").strip(), "quote": quote,
            "quote_verified": (_norm(quote) in _norm(c["transcript"])) if quote else None,
        })

    cats = [s["category"] for s in statements]
    known = [s for s in statements if s["category"] != "unclear"]
    overall = max(known, key=lambda s: s["call_date"])["category"] if known else "unclear"
    return {
        "statements": statements, "overall_category": overall, "categories": sorted(set(cats)),
        "consistent": len(set(c for c in cats if c != "unclear")) <= 1,
        "claims_income_loss": "income_loss" in cats, "claims_dispute": "dispute" in cats,
    }
