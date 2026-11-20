"""
PortfolioIQ outreach: the part that actually sends (email, WhatsApp, SMS) after a person approves.

Safety rails live in config.py and are enforced in code, not in prompts:
  - the default mode is "mock": nothing leaves the system
  - in "live" mode every message goes ONLY to the test recipient from the settings (the customer's own address is never used)
  - a kill switch (OUTREACH_DISABLED) stops everything
  - settings and secrets are never printed, and error texts are scrubbed of secret values
Every attempt (sent, mock, blocked, failed) is written to the outreach_log table (log.py): the audit trail.
"""
