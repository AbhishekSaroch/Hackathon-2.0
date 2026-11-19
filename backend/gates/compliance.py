"""
Compliance gate for service messages (BI-03 nudges today, any customer message later).

A message may only be sent if EVERY check passes. The checks are plain code, not a prompt, so they cannot be talked around.
There is no opt-out / DND check on purpose: that data is not part of the brief's inputs.

content checks (about the words):
  template_only         the message is exactly an approved D11 template with its placeholders filled, nothing added
  no_dues_language      no "pending / overdue / dues / legal / penalty / recovery ..." words (this is a service message, not a collection message)
  no_amounts            no money amounts
  no_urgency_or_threat  no "immediately / urgent / last warning / or else" pressure
  no_signal_disclosure  never tell the customer which distress signal we saw (e.g. "your other loan bounced")
  roman_script          Roman letters only (Hinglish or English, never Devanagari)
  length                fits a WhatsApp message
  placeholders_filled   no "{name}" left over, the name is not empty
context checks (about the situation):
  allowed_hours         send time between 08:00 and 19:00
  frequency_cap         at most 1 nudge per account in 7 days
  account_is_current    the account must still be current (no dues pressure on a current account, and no nudge to an account already past due)
"""
from __future__ import annotations

import re
from datetime import datetime

DUES = re.compile(r"\b(pending|dues?|overdue|outstanding|arrears?|defaulters?|defaulted|default|npa|recovery|legal|lawyer|court|notice|penalt\w*|fines?|"
                  r"blacklist\w*|cibil|credit score|action will be taken|must pay|bakaya|baki|baaki|kanoon\w*|kanun\w*|jurmana|karyavahi)\b", re.I)
URGENCY = re.compile(r"\b(immediately|urgent(ly)?|asap|last warning|final notice|or else|right now|at once|turant|fauran|abhi ke abhi|jaldi se jaldi|warna|aaj hi)\b", re.I)
SIGNAL_WORDS = re.compile(r"\b(bounce\w*|dishonou?r\w*|declined|utili[sz]ation|enquir(y|ies)|late salary|salary (is )?(late|delayed))\b", re.I)
AMOUNT = re.compile(r"(₹|\brs\.?|\binr\b|\brupees?\b)\s*\d|\d[\d,]{3,}", re.I)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
PLACEHOLDER = re.compile(r"\{\w*\}")

OPEN_HOUR, CLOSE_HOUR = 8, 19
MAX_LEN = 320
CAP_DAYS = 7


def _c(name: str, passed: bool, detail: str, kind: str) -> dict:
    return {"check": name, "passed": bool(passed), "detail": detail, "kind": kind}


def evaluate(message: str, expected_message: str, send_at: datetime, account_is_current: bool, recent_nudges: int) -> dict:
    """Run every check. `expected_message` is the template rendered with its placeholders (what the message must be)."""
    hit = lambda rx: sorted({m.group(0).lower() for m in rx.finditer(message)})
    dues, urg, sig, amt = hit(DUES), hit(URGENCY), hit(SIGNAL_WORDS), hit(AMOUNT)
    checks = [
        _c("template_only", message == expected_message, "matches the approved template" if message == expected_message else "differs from the approved template", "content"),
        _c("no_dues_language", not dues, "none" if not dues else f"found: {', '.join(dues)}", "content"),
        _c("no_amounts", not amt, "none" if not amt else f"found: {', '.join(amt)}", "content"),
        _c("no_urgency_or_threat", not urg, "none" if not urg else f"found: {', '.join(urg)}", "content"),
        _c("no_signal_disclosure", not sig, "none" if not sig else f"found: {', '.join(sig)}", "content"),
        _c("roman_script", not DEVANAGARI.search(message), "Roman letters only" if not DEVANAGARI.search(message) else "Devanagari found", "content"),
        _c("length", len(message) <= MAX_LEN, f"{len(message)} of {MAX_LEN} characters", "content"),
        _c("placeholders_filled", not PLACEHOLDER.search(message) and bool(message.strip()), "all filled" if not PLACEHOLDER.search(message) else "placeholder left in text", "content"),
        _c("allowed_hours", OPEN_HOUR <= send_at.hour < CLOSE_HOUR, f"send time {send_at:%H:%M}, allowed {OPEN_HOUR:02d}:00-{CLOSE_HOUR:02d}:00", "context"),
        _c("frequency_cap", recent_nudges == 0, f"{recent_nudges} nudge(s) in the last {CAP_DAYS} days", "context"),
        _c("account_is_current", bool(account_is_current), "account is current" if account_is_current else "account is past due: no nudge", "context"),
    ]
    failed = [c["check"] for c in checks if not c["passed"]]
    return {"passed": not failed, "failed": failed, "content_failed": [c["check"] for c in checks if not c["passed"] and c["kind"] == "content"],
            "context_failed": [c["check"] for c in checks if not c["passed"] and c["kind"] == "context"], "checks": checks}
