"""
What may be said, and to whom.

Customer reminders (BI-02 "forgot to pay"): only these approved templates, with the blanks filled by code. No AI writes customer text.
  The BI-03 service gate (gates/compliance.py) is for nudges to CURRENT accounts and refuses dues words and past-due accounts on purpose.
  A reminder to a customer who is already a little late is a different message type, so it has its own gate (below). It keeps every limit that still
  makes sense (no threats, no urgency, no amounts, Roman letters, length, 08:00-19:00, at most one a week) and adds: never contact a disputed account.
Internal handoffs (dispute team, collectors): plain templated emails built from the agent's own recorded facts, in code. No AI writes them either.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "gates") not in sys.path:
    sys.path.append(str(BACKEND / "gates"))
import compliance as G  # noqa: E402  (its regexes and limits are reused so the two gates can never drift apart)

APP_URL = os.getenv("OUTREACH_APP_URL", "http://localhost:5173")

TEMPLATES = {      # approved courteous payment reminders (no amounts, no threats, no urgency). English is the default, polite Hinglish the fallback.
    "T-COL-H1": "Namaste {name} ji, hamare record ke anusar aapki EMI ka bhugtan abhi prapt nahi hua hai. Yadi aap bhugtan kar chuke hain, to kripya is sandesh ko anadekha karein. Sahayata ke liye HELP likhein. Dhanyavaad.",
    "T-COL-H2": "Namaste {name} ji, yeh ek vinamr yaad dilana hai ki aapki EMI ka bhugtan abhi prapt nahi hua hai. Aap ise app se kisi bhi samay kar sakte hain. Sahayata ke liye HELP likhein. Dhanyavaad.",
    "T-COL-E1": "Dear {name}, our records show that your EMI payment has not yet been received. If you have already paid, please disregard this message. You can pay at any time through the app. For any assistance, please reply HELP. Thank you.",
    "T-COL-E2": "Hello {name}, this is a courteous reminder that we have not yet received your EMI payment. If you have already paid, please ignore this message. You may pay at any time in the app. Reply HELP if you need any assistance. Thank you.",
}
THREAT = re.compile(r"\b(legal|lawyer|court|notice|penalt\w*|fines?|blacklist\w*|cibil|credit score|action will be taken|must pay|police|jail|arrest\w*|"
                    r"default\w*|npa|recovery|kanoon\w*|kanun\w*|jurmana|karyavahi)\b", re.I)

TEAMS = {          # recommendation channel -> (team shown in the email, what the person should do)
    "dispute_team": "Dispute team",
    "telecall": "Collections team (callback)",
    "field_visit": "Field visit team",
    "human": "Collections team (human collector)",
}
LABELS = {"forgot": "Forgot to pay", "cant_pay": "Cannot pay", "wont_pay": "Will not pay", "disputed": "Disputed", "insufficient": "Not sure"}


def first_name(full: str | None) -> str:
    return (full or "").strip().split(" ")[0]


def pick_template(account_id: str, language: str = "english") -> str:
    ids = sorted(t for t in TEMPLATES if t.startswith("T-COL-H" if language == "hinglish" else "T-COL-E"))
    return ids[sum(map(ord, account_id)) % len(ids)]


def render_reminder(account_id: str, customer_name: str, language: str = "english") -> tuple[str, str]:
    tid = pick_template(account_id, language)
    return tid, TEMPLATES[tid].format(name=first_name(customer_name))


def reminder_gate(message: str, expected: str, send_at: datetime, recent: int, label: str | None) -> dict:
    """Every check must pass for a customer reminder to be sent. Same result shape as gates/compliance.evaluate."""
    hit = lambda rx: sorted({m.group(0).lower() for m in rx.finditer(message)})
    threat, urg, amt = hit(THREAT), hit(G.URGENCY), hit(G.AMOUNT)
    c = lambda name, ok, detail, kind: {"check": name, "passed": bool(ok), "detail": detail, "kind": kind}
    checks = [
        c("template_only", message == expected, "matches the approved template" if message == expected else "differs from the approved template", "content"),
        c("no_threats", not threat, "none" if not threat else f"found: {', '.join(threat)}", "content"),
        c("no_urgency", not urg, "none" if not urg else f"found: {', '.join(urg)}", "content"),
        c("no_amounts", not amt, "none" if not amt else f"found: {', '.join(amt)}", "content"),
        c("roman_script", not G.DEVANAGARI.search(message), "Roman letters only", "content"),
        c("length", len(message) <= G.MAX_LEN, f"{len(message)} of {G.MAX_LEN} characters", "content"),
        c("placeholders_filled", not G.PLACEHOLDER.search(message) and bool(message.strip()), "all filled", "content"),
        c("not_disputed", label != "disputed", "no open dispute" if label != "disputed" else "the customer disputes this account: no customer message", "context"),
        c("allowed_hours", G.OPEN_HOUR <= send_at.hour < G.CLOSE_HOUR, f"send time {send_at:%H:%M}, allowed {G.OPEN_HOUR:02d}:00-{G.CLOSE_HOUR:02d}:00", "context"),
        c("frequency_cap", recent == 0, f"{recent} reminder(s) in the last {G.CAP_DAYS} days", "context"),
    ]
    failed = [x["check"] for x in checks if not x["passed"]]
    return {"passed": not failed, "failed": failed, "checks": checks}


def team_email(ctx: dict, approved_on: datetime) -> tuple[str, str, str]:
    """(team, subject, body) for an internal handoff. Built only from recorded facts; nothing is generated."""
    team = TEAMS[ctx["rec_channel"]]
    label = LABELS.get(ctx["label"], ctx["label"] or "")
    conf = f"{round(float(ctx['confidence']) * 100)}%" if ctx.get("confidence") is not None else "n/a"
    lines = [
        f"To: {team}",
        f"Recommended by the PortfolioIQ agent, approved by a person on {approved_on:%d %b %Y %H:%M}.",
        "",
        f"Customer: {ctx['customer_name']}   Account: {ctx['account_id']}",
        f"Product: {ctx['product']}   Branch: {ctx['branch']}",
        f"Days past due: {ctx['dpd']}   Outstanding: Rs {ctx['outstanding']:,.0f}   EMI: Rs {ctx['emi_amount']:,.0f}",
        f"Agent's reading: {label} (confidence {conf})",
        "",
        f"What to do: {ctx['rec_action']}",
        f"Why: {ctx['rec_reason']}",
    ]
    if ctx.get("reasoning_summary"):
        lines += ["", f"Evidence: {ctx['reasoning_summary']}"]
    if ctx["rec_channel"] == "dispute_team":
        lines += ["", "Collection calls on this account are paused until the dispute is resolved."]
    lines += ["", f"Open the account: {APP_URL}/accounts/{ctx['account_id']}", "",
              "Demo note: in this demo every message goes only to the test recipient; the real recipient would be the team named above."]
    subject = f"{team}: {ctx['customer_name']} ({ctx['account_id']}) {label}"
    return team, subject, "\n".join(lines)
