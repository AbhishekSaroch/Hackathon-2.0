# Outreach: real email / WhatsApp / SMS after a person approves (BI-02 "To approve")

Nothing here is committed. Code: `backend/outreach/`, `backend/routers/outreach.py`, `frontend/src/pages/Approve.jsx`, tests `backend/tests/test_outreach_*.py`.
BI-02's own files are untouched (`check_bi02.py` stays at 31 passed): its approve endpoint still only records; "Approve and send" is a separate call.

## What happens when you approve

| Recommendation | Action |
|---|---|
| Forgot (WhatsApp) | An approved reminder template (no AI-written text, no amount, no threat) to the customer, after the reminder gate. Channel: `OUTREACH_CUSTOMER_CHANNEL` = whatsapp (default) / sms / email |
| Disputed | Email to the dispute team, and collection calls on the account are paused (`collection_holds`). A disputed customer never gets a customer message |
| Can't pay / Won't pay / Field visit / Not sure | Handoff email to that team (templated from the agent's own recorded facts) |

Reminder gate (`outreach/messages.py`): template only, no threats, no urgency, no amounts, Roman letters, length, not disputed, 08:00 to 19:00, at most one reminder per account per 7 days. A blocked or failed one stays "Waiting for you" and can be retried.

## Safety rails (in code, tested)
- Default mode is **mock** (nothing leaves). Live only when `OUTREACH_MODE` is exactly `live`; a typo means mock. `OUTREACH_DISABLED=1` stops everything.
- Every live message goes **only to the test recipient** (`OUTREACH_TEST_EMAIL`, `OUTREACH_TEST_PHONE`). No code path takes a customer's address. The text starts with `[PortfolioIQ demo, for <customer>]`.
- Live mode with a missing setting is **blocked** (never silently mock). Secrets are never shown or logged; error texts are scrubbed.
- Nothing is sent twice for the same recommendation. Every attempt (sent, mock, blocked, failed) is in the send log (Send log tab, `GET /outreach/log`).
- "Approve many": the AI only reads the sentence and picks groups from a fixed list; code cuts its answer down (cannot name a recipient, cannot lower the 70% confidence floor, max 25 per batch). You see the exact list first (Preview), and only Confirm acts, once, on that list. "Not sure" recommendations (all below 70%) are never part of a bulk action.

## Settings (names only; put the values in `backend/.env` yourself)
`OUTREACH_MODE`, `OUTREACH_TEST_EMAIL`, `OUTREACH_TEST_PHONE` (+91...), `OUTREACH_CUSTOMER_CHANNEL`, `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` (or `SMTP_USER` + `SMTP_APP_PASSWORD`),
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TWILIO_SMS_FROM`, and optional `TWILIO_WHATSAPP_CONTENT_SID`, `TWILIO_SMS_CONTENT_SID`.

Check: `cd backend; python -m outreach status` (no secrets). Send one test per channel to yourself: `python -m outreach test [email|whatsapp|sms]`.

## Twilio trial: WhatsApp and SMS are blocked by the account, not by our code
Email works (the user confirmed the mails arrive). This Twilio trial account rejects free text: WhatsApp says "ContentSid Required" (21654) and SMS says "Trial accounts can only use predefined SMS templates" (572006).
The usual fix does not exist on a trial: the console's Templates page says "Upgrade", and the Content API answers "not available on a Trial account" (20003, checked with `python -m outreach templates`).
So WhatsApp and SMS need one of: upgrading the Twilio account (paid, the user's decision), or another provider (for example the Meta WhatsApp Cloud API test number, a new provider module). The code for both channels is written and tested against a fake network, including Content-Template mode (`TWILIO_WHATSAPP_CONTENT_SID`, `TWILIO_SMS_CONTENT_SID`) for an account that has templates.
Keep `OUTREACH_CUSTOMER_CHANNEL=email` so the customer reminders reach the test inbox. Say it plainly in the demo: "email is live; WhatsApp and SMS are integrated but this trial account only allows pre-approved templates".

## Tests
`test_outreach_guard.py` (35: mode, recipient, secrets, log), `test_outreach_send.py` (providers with a fake network, sender, every action, the gate at night / disputed / weekly cap, bulk command and plan rules, API and tenant isolation). Both run on a copy of the database and cannot send anything.
Live rehearsal done on a copy of the database: 2 callback handoffs and 1 customer reminder by email arrived at the test inbox; a WhatsApp attempt failed cleanly with the Twilio hint and stayed pending.
