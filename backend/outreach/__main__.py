"""
  python -m outreach status             the setup in one screen: mode, which channels are ready, where messages would go (masked). No secrets.
  python -m outreach test [channel...]  one short test message per channel (email, whatsapp, sms) to the test recipient only
  python -m outreach templates          the Twilio Content Templates this account can use (read-only)

Run it from the backend folder (cd backend).
"""
import json
import sys

from . import config


def test(which: list[str]) -> int:
    """One short test message per channel, to the test recipient only (really sent when OUTREACH_MODE=live). Twilio's own delivery status is shown."""
    from . import providers, sender
    ok = True
    for ch in which:
        body = f"PortfolioIQ test message ({ch}). If you can read this, {ch} sending works."
        r = sender.send("abc_bank", ch, body, subject="Test message", purpose="test", intended_for="you (test)")
        line = f"{ch:9s} {r['status']:10s}" + (f" {r['error']}" if r["error"] else "")
        if r["status"] == "sent" and ch in ("whatsapp", "sms"):
            try:
                st = providers.twilio_status(r["provider_ref"])
                line += f" | Twilio says: {st['status']}" + (f" (error {st['error_code']}: {st['error_message']})" if st["error_code"] else "")
                ok = ok and st["status"] in ("queued", "sending", "sent", "delivered", "read")
            except Exception as e:
                line += f" | could not read the delivery status: {config.scrub(str(e))}"
        elif r["status"] == "sent":
            line += " | SMTP accepted it"
        else:
            ok = ok and r["status"] == "mock_sent"
        print(line)
    return 0 if ok else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "test":
        which = sys.argv[2:] or ["email", "whatsapp", "sms"]
        return test(which)
    if cmd == "templates":
        from . import providers
        try:
            rows = providers.list_templates()
        except Exception as e:
            print("could not list the Twilio templates:", config.scrub(str(e)))
            return 1
        print(f"{len(rows)} template(s) available to this Twilio account")
        for t in rows:
            print(f"  {t['sid']}  {t['name']}  types={t['types']}  variables={t['variables']}\n      {t['body'][:140]!r}")
        return 0
    if cmd != "status":
        print(__doc__)
        return 2
    s = config.status()
    print(f"mode: {s['mode']}" + ("   KILL SWITCH ON" if s["kill_switch"] else ""))
    print(f"test recipient: email {s['test_recipient']['email']}, phone {s['test_recipient']['phone']}   (every live message goes only here)")
    for c, v in s["channels"].items():
        print(f"  {c:9s} {'ready' if v['configured'] else 'NOT ready':9s} would: {v['would_do']:8s} {v['why']}")
        if v["missing"]:
            print(f"            missing: {', '.join(v['missing'])}")
    print("settings found as (variable names only):")
    print(json.dumps(s["settings_found_as"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
