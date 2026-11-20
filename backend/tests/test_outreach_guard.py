"""
Outreach safety-rail tests (Step 0).   python test_outreach_guard.py   (exit code 1 on any failure)

  A defaults    nothing is sent unless OUTREACH_MODE is exactly live; a typo means mock; the kill switch beats live
  B not ready   live mode with missing or invalid settings is BLOCKED (never silently downgraded to mock, never sent)
  C recipient   the only address a message can go to is the test recipient; there is no way to pass another one
  D secrets     status() and the error texts never contain a secret value; only variable names and masked recipients
  E log         every attempt is logged, tenant-scoped, and "already done" works (nothing is sent twice)
Runs on a throw-away database and ignores backend/.env (OUTREACH_NO_DOTENV=1), so it cannot send anything and never touches real settings.
"""
import inspect
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
TMP = Path(tempfile.mkdtemp()) / "outreach_test.db"
os.environ["PORTFOLIOIQ_DB"] = str(TMP)                      # before db.py is imported
os.environ["OUTREACH_NO_DOTENV"] = "1"
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND / "db")]
import db  # noqa: E402
from outreach import config, log  # noqa: E402

fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


ALL_VARS = [n for names in config.ALIASES.values() for n in names] + ["OUTREACH_MODE", "OUTREACH_DISABLED", "SMTP_HOST", "SMTP_PORT"]


def clean() -> None:
    for n in ALL_VARS:
        os.environ.pop(n, None)


def full_live() -> None:
    clean()
    os.environ.update({"OUTREACH_MODE": "live", "OUTREACH_TEST_EMAIL": "tester@example.com", "OUTREACH_TEST_PHONE": "+919876543210",
                       "TWILIO_ACCOUNT_SID": "ACsecretsid1234567890", "TWILIO_AUTH_TOKEN": "SECRET-TOKEN-abcdef123456",
                       "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886", "TWILIO_SMS_FROM": "+14155238886",
                       "SMTP_USER": "sender@example.com", "SMTP_APP_PASSWORD": "app-password-xyz-98765"})


# ---------------------------------------------------------------- A defaults
clean()
ok("A nothing set: mode is mock and every channel would only mock", config.mode() == "mock" and all(config.plan(c)[0] == "mock" for c in config.CHANNELS))
for typo in ("LIVE ", "Live", "yes", "1", "true", "on", "prod", ""):
    os.environ["OUTREACH_MODE"] = typo
    expect = "live" if typo.strip().lower() == "live" else "mock"
    ok(f"A OUTREACH_MODE={typo!r} gives {expect}", config.mode() == expect)
full_live()
os.environ["OUTREACH_DISABLED"] = "1"
ok("A kill switch beats live: every channel blocked, recipient lookup refused", all(config.plan(c)[0] == "blocked" for c in config.CHANNELS))
try:
    config.recipient_for("email")
    ok("A kill switch: recipient lookup is refused", False)
except config.OutreachBlocked:
    ok("A kill switch: recipient lookup is refused", True)
ok("A a channel that does not exist is blocked", config.plan("telegram")[0] == "blocked")

# ---------------------------------------------------------------- B not ready
full_live()
ok("B live and fully configured: all three channels would go live", all(config.plan(c)[0] == "live" for c in config.CHANNELS))
for name in ("TWILIO_AUTH_TOKEN", "SMTP_APP_PASSWORD", "OUTREACH_TEST_PHONE", "OUTREACH_TEST_EMAIL"):
    full_live()
    os.environ.pop(name)
    p = {c: config.plan(c)[0] for c in config.CHANNELS}
    hit = {"TWILIO_AUTH_TOKEN": ["whatsapp", "sms"], "SMTP_APP_PASSWORD": ["email"], "OUTREACH_TEST_PHONE": ["whatsapp", "sms"], "OUTREACH_TEST_EMAIL": ["email"]}[name]
    ok(f"B live without {name}: {hit} are BLOCKED (not mock, not sent), the others stay live", all(p[c] == "blocked" for c in hit) and all(p[c] == "live" for c in config.CHANNELS if c not in hit), str(p))
full_live()
os.environ["OUTREACH_TEST_PHONE"] = "9876543210"
ok("B a test phone without + and country code is refused", config.plan("sms")[0] == "blocked" and config.plan("email")[0] == "live")
full_live()
os.environ["OUTREACH_TEST_EMAIL"] = "not-an-email"
ok("B an invalid test email is refused", config.plan("email")[0] == "blocked")
full_live()
os.environ["TWILIO_WHATSAPP_FROM"] = "+14155238886"
ok("B a WhatsApp sender without the whatsapp: prefix is refused", config.plan("whatsapp")[0] == "blocked" and config.plan("sms")[0] == "live")
full_live()
for n in ("SMTP_USER", "SMTP_APP_PASSWORD"):
    os.environ.pop(n)
os.environ.update({"GMAIL_ADDRESS": "g@example.com", "GMAIL_APP_PASSWORD": "gmail-app-pw-4567"})
ok("B Gmail-style variable names are accepted as aliases", config.plan("email")[0] == "live" and config.found("smtp_user")[0] == "GMAIL_ADDRESS" and config.found("smtp_password")[0] == "GMAIL_APP_PASSWORD")

# ---------------------------------------------------------------- C recipient
full_live()
ok("C email goes only to the test email, WhatsApp and SMS only to the test phone",
   config.recipient_for("email") == "tester@example.com" and config.recipient_for("whatsapp") == "+919876543210" and config.recipient_for("sms") == "+919876543210")
ok("C recipient_for takes ONLY the channel: there is no way to pass a customer's address", list(inspect.signature(config.recipient_for).parameters) == ["channel"])
os.environ.pop("OUTREACH_TEST_PHONE")
try:
    config.recipient_for("sms")
    ok("C no test phone: the lookup is refused", False)
except config.OutreachBlocked:
    ok("C no test phone: the lookup is refused", True)

# ---------------------------------------------------------------- D secrets
full_live()
SECRET_VALUES = ["ACsecretsid1234567890", "SECRET-TOKEN-abcdef123456", "app-password-xyz-98765", "sender@example.com"]
st = json.dumps(config.status())
ok("D status() contains no secret value", not any(s in st for s in SECRET_VALUES), "")
ok("D status() shows masked recipients, not the full address or number", "tester@example.com" not in st and "+919876543210" not in st and "t***@example.com" in st)
ok("D status() names the variables it found (names only)", json.loads(st)["settings_found_as"]["twilio_token"] == "TWILIO_AUTH_TOKEN")
err = "401 for https://api.twilio.com with auth ACsecretsid1234567890:SECRET-TOKEN-abcdef123456 failed; login sender@example.com / app-password-xyz-98765"
sc = config.scrub(err)
ok("D scrub() removes every secret from an error text", not any(s in sc for s in SECRET_VALUES) and "***" in sc, sc[:60])

# ---------------------------------------------------------------- E log
db.init_db("main")
rid = log.record("t_a", channel="email", mode="mock", status="mock_sent", rec_id=11, account_id="A1", purpose="team_handoff", intended_for="Dispute team",
                 recipient_hint=config.mask("tester@example.com"), message="hello")
log.record("t_b", channel="whatsapp", mode="live", status="sent", rec_id=11, purpose="customer_reminder", provider_ref="SM123")
ok("E a log row is written", isinstance(rid, int) and rid > 0)
ok("E the log is tenant-scoped: t_a sees only its own row", [r["channel"] for r in log.recent("t_a")] == ["email"] and [r["channel"] for r in log.recent("t_b")] == ["whatsapp"] and log.recent("t_c") == [])
ok("E already_done is true for a sent or mock_sent purpose, false for another purpose or tenant",
   log.already_done("t_a", 11, "team_handoff") and not log.already_done("t_a", 11, "customer_reminder") and not log.already_done("t_c", 11, "team_handoff"))
log.record("t_a", channel="sms", mode="live", status="failed", rec_id=12, purpose="customer_reminder", error="boom SECRET-TOKEN-abcdef123456")
ok("E a failed or blocked attempt does not count as done, so it can be retried", not log.already_done("t_a", 12, "customer_reminder"))
ok("E secrets are scrubbed out of the stored error", "SECRET-TOKEN" not in log.recent("t_a")[0]["error"])
try:
    log.record("t_a", channel="sms", mode="live", status="delivered")
    ok("E an unknown status is refused", False)
except ValueError:
    ok("E an unknown status is refused", True)

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
