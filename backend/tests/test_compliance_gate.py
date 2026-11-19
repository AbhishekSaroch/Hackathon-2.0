"""
Compliance gate tests.   python test_compliance_gate.py   (exit code 1 on any failure)

Every rule is attacked on its own (all other rules satisfied), so a failure points at exactly one check.
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "gates"), str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import compliance as G  # noqa: E402

fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


MORNING = datetime(2026, 8, 10, 10, 0)


def only(msg: str, **kw) -> list:
    """Run the gate with the message equal to its 'template' so template_only passes; return what failed."""
    args = {"send_at": MORNING, "account_is_current": True, "recent_nudges": 0, **kw}
    return G.evaluate(msg, msg, args["send_at"], args["account_is_current"], args["recent_nudges"])["failed"]


# ---- 1 the real D11 templates must all pass (the gate must not block what the business approved) ----
tpl = list(csv.DictReader(open(BACKEND / "data" / "csv_bi03" / "service_templates.csv", encoding="utf-8")))
bad = []
for t in tpl:
    lang = t["language"]
    msg = t["body"].format(name="Asha", new_date="10th" if lang == "english" else "10", emi_day="5th" if lang == "english" else "5")
    r = G.evaluate(msg, msg, MORNING, True, 0)
    if not r["passed"]:
        bad.append((t["template_id"], r["failed"]))
ok("all 12 approved D11 templates pass the gate when filled in", len(tpl) == 12 and not bad, str(bad) if bad else "12 of 12")

# ---- 2 each rule on its own ----
cases = [
    ("no_dues_language", "Hi Asha, your payment is overdue. Reply HELP."),
    ("no_dues_language", "Hi Asha, legal action will be taken if you do not reply."),
    ("no_dues_language", "Namaste Asha, aapka bakaya jaldi bharein."),
    ("no_amounts", "Hi Asha, your EMI of Rs 12,000 is coming up. Reply HELP."),
    ("no_amounts", "Hi Asha, aapki EMI ₹4500 hai. Reply HELP."),
    ("no_amounts", "Hi Asha, reference 123456 for you. Reply HELP."),
    ("no_urgency_or_threat", "Hi Asha, please reply immediately."),
    ("no_urgency_or_threat", "Hi Asha, last warning, reply HELP."),
    ("no_urgency_or_threat", "Namaste Asha, turant reply karein."),
    ("no_signal_disclosure", "Hi Asha, we saw your other loan payment bounce. Reply HELP."),
    ("no_signal_disclosure", "Hi Asha, your card utilisation is high. Reply HELP."),
    ("roman_script", "Hi Asha, मदद चाहिए तो HELP लिखें."),
    ("length", "Hi Asha, " + "x" * 400),
    ("placeholders_filled", "Hi {name}, reply HELP."),
]
for check, msg in cases:
    f = only(msg)
    ok(f"rule {check}: blocks '{msg[:48]}'", f == [check] or (check in f and "template_only" not in f), str(f))

# ---- 3 situation rules ----
ok("hours: 07:59 blocked", only("Hi Asha, reply HELP.", send_at=datetime(2026, 8, 10, 7, 59)) == ["allowed_hours"])
ok("hours: 08:00 allowed", only("Hi Asha, reply HELP.", send_at=datetime(2026, 8, 10, 8, 0)) == [])
ok("hours: 18:59 allowed", only("Hi Asha, reply HELP.", send_at=datetime(2026, 8, 10, 18, 59)) == [])
ok("hours: 19:00 blocked", only("Hi Asha, reply HELP.", send_at=datetime(2026, 8, 10, 19, 0)) == ["allowed_hours"])
ok("frequency cap: a nudge in the last 7 days blocks", only("Hi Asha, reply HELP.", recent_nudges=1) == ["frequency_cap"])
ok("account past due: no nudge", only("Hi Asha, reply HELP.", account_is_current=False) == ["account_is_current"])

# ---- 4 tampering and the pushy message from the very first prototype ----
approved = "Hi Asha, EMI date ki chinta chhodiye! 2 minute me auto-pay set kar lein. Madad chahiye toh HELP likhein."
ok("approved text passes", G.evaluate(approved, approved, MORNING, True, 0)["passed"])
r = G.evaluate(approved + " Pay your dues now.", approved, MORNING, True, 0)
ok("adding even one sentence to an approved template is caught (template_only + dues words)", "template_only" in r["failed"] and "no_dues_language" in r["failed"], str(r["failed"]))
r = G.evaluate("Hi Rahul, your pending dues of Rs 18,000 must be paid immediately or legal action will be taken.", approved, MORNING, True, 0)
ok("the pushy collection message is blocked by 4 independent rules", {"template_only", "no_dues_language", "no_amounts", "no_urgency_or_threat"} <= set(r["failed"]), str(r["failed"]))

# ---- 5 structure ----
r = G.evaluate("Hi Asha, we saw your other loan payment bounce", "x", datetime(2026, 8, 10, 6, 0), False, 1)
ok("result separates wording problems from situation problems", set(r["context_failed"]) == {"allowed_hours", "frequency_cap", "account_is_current"} and "no_signal_disclosure" in r["content_failed"])
ok("every check reports a reason", all(c["detail"] for c in r["checks"]) and len(r["checks"]) == 11, f"{len(r['checks'])} checks")
ok("there is no opt-out/DND check (not in the provided data)", not any("opt" in c["check"] or "dnd" in c["check"] for c in r["checks"]))

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
