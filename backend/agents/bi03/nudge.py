"""
BI-03 nudge engine: a flag becomes a helpful service message, or is stopped by the compliance gate.

  python nudge.py process [--reset] [--auto-send]     turn every flag without a nudge into a nudge (in date order)
  python nudge.py dispatch --nudge-id 12              (mock) send one approved nudge, refused unless the gate passed
  python nudge.py show --account 00606000133          every nudge of one account

What happens for each flag (nothing here uses an LLM: the words come only from the approved D11 templates):
  1 choose   the dominant signal picks the nudge type: late salary -> EMI date change offer, other-loan bounce or late EMI
             payments -> auto-pay setup, anything else -> friendly reminder
  2 draft    take an approved template (Hinglish by default), fill {name}, {new_date}, {emi_day} from the account's own data
  3 gate     compliance gate (gates/compliance.py). If the WORDING is rejected the agent tries the next approved variant, then the
             other language. If the SITUATION is rejected (hours, 7-day cap, account past due) it stops, a different wording cannot help.
  4 record   one row in `nudges` with the full gate result. Passed -> pending_approval. Rejected -> blocked, with the reasons.
  5 dispatch only a nudge whose gate passed can be sent, and the gate's content checks run once more at send time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
sys.path[:0] = [str(HERE), str(BACKEND / "db"), str(BACKEND / "gates")]
import compliance as G  # noqa: E402
import db  # noqa: E402
import features as F  # noqa: E402
import runner as R  # noqa: E402

DB = "bi03"
SEND_HOUR = 10                       # nudges are scheduled for 10:00 on the day of the flag
LANGUAGE = "hinglish"                # customer-facing text: Hinglish in Roman letters (English is the fallback)
SIGNAL_TO_NUDGE = {"salary_delay": "emi_date_change", "other_loan_bounce": "autopay_setup", "late_emi_payments": "autopay_setup"}
DEFAULT_NUDGE = "reminder"


class GateError(Exception):
    """Raised when something tries to send a message the compliance gate has not passed."""


# ------------------------------------------------------------------ templates and wording
def load_templates(tenant: str) -> dict:
    df = db.query_df(tenant, "SELECT template_id, nudge_type, language, body, placeholders FROM service_templates WHERE tenant_id = :tenant_id ORDER BY template_id", database=DB)
    out: dict = {}
    for r in df.to_dict("records"):
        out.setdefault((r["nudge_type"], r["language"]), []).append(r)
    return out


def day_label(n: int, language: str) -> str:
    if language == "english":
        return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
    return str(n)


def render(body: str, params: dict, language: str) -> str:
    return body.format(name=params["name"], new_date=day_label(params["new_day"], language), emi_day=day_label(params["emi_day"], language))


def first_name(full_name: str | None) -> str:
    token = re.sub(r"[^A-Za-z]", "", (full_name or "").split(" ")[0])
    return token.capitalize()


def pick_new_day(emi_day: int, salary_day: int) -> int:
    """Move the EMI to after the (late) salary: at least 5 days after today's EMI date and 12 days after the usual salary day."""
    return int(min(28, max(emi_day + 5, salary_day + 12)))


def variants_for(templates: dict, nudge_type: str, language: str, account_id: str) -> list[tuple[dict, str]]:
    """Approved wordings in the order to try: this language first (rotated by account so wording varies), then the other language."""
    order = []
    for lang in (language, "english" if language == "hinglish" else "hinglish"):
        t = templates.get((nudge_type, lang), [])
        shift = int(hashlib.md5(account_id.encode()).hexdigest(), 16) % max(len(t), 1)
        order += [(x, lang) for x in t[shift:] + t[:shift]]
    return order


def draft_and_gate(templates: dict, account_id: str, signal: str | None, params: dict, send_at: datetime, is_current: bool, recent: int, language: str = LANGUAGE) -> dict:
    """Choose, draft, gate. Returns the nudge as it will be stored."""
    ntype = SIGNAL_TO_NUDGE.get(signal, DEFAULT_NUDGE)
    attempts, chosen, last = [], None, None
    for tpl, lang in variants_for(templates, ntype, language, account_id):
        msg = render(tpl["body"], params, lang)
        res = G.evaluate(msg, render(tpl["body"], params, lang), send_at, is_current, recent)
        attempts.append({"template_id": tpl["template_id"], "language": lang, "passed": res["passed"], "failed": res["failed"]})
        last = (tpl, lang, msg, res)
        if res["passed"]:
            chosen = last
            break
        if res["context_failed"]:                      # hours / cap / past due: different words cannot fix that
            break
    tpl, lang, msg, res = chosen or last or (None, language, None, {"passed": False, "failed": ["no_approved_template"], "checks": []})
    return {"nudge_type": ntype, "template_id": tpl["template_id"] if tpl else None, "message": msg, "gate_passed": int(bool(chosen)),
            "status": "pending_approval" if chosen else "blocked",
            "gate_result": json.dumps({"passed": bool(chosen), "failed": res["failed"], "checks": res["checks"], "attempts": attempts,
                                       "params": params, "language": lang, "send_at": send_at.isoformat()})}


# ------------------------------------------------------------------ processing flags
def _flag_context(eng: F.FeatureEngine, accounts: list[str], day: pd.Timestamp, names: dict, due_day: dict) -> dict:
    """What we know about each flagged account on the flag day (only rows visible that day)."""
    ev = eng.events[eng.events["account_id"].isin(accounts) & (eng.events["available_from"] <= day)]
    not_current = set(R.not_current_accounts(ev, day))
    sal = ev[ev["is_salary"]]
    base = sal[(sal["event_date"] <= day - pd.Timedelta(days=90)) & (sal["event_date"] > day - pd.Timedelta(days=420))]
    usual = base.assign(dom=base["event_date"].dt.day).groupby("account_id")["dom"].median()
    usual = usual.combine_first(sal.assign(dom=sal["event_date"].dt.day).groupby("account_id")["dom"].median())
    last_due = ev[ev["event_type"] == "emi_due"].sort_values(["account_id", "event_date"]).groupby("account_id").tail(1).set_index("account_id")["event_date"].dt.day
    out = {}
    for a in accounts:
        emi_day = int(last_due.get(a, due_day.get(a, 10)))
        sd = int(usual.get(a, 5))
        out[a] = {"is_current": a not in not_current, "params": {"name": first_name(names.get(a)), "emi_day": emi_day, "new_day": pick_new_day(emi_day, sd)}}
    return out


def process_flags(tenant: str, eng: F.FeatureEngine | None = None, auto_send: bool = False, language: str = LANGUAGE, quiet: bool = False) -> dict:
    eng = eng or F.FeatureEngine.from_db(tenant)
    templates = load_templates(tenant)
    flags = db.query_df(tenant, "SELECT f.flag_id, f.account_id, f.flagged_on, f.dominant_signal, a.customer_name, a.due_date FROM flags f "
                                "JOIN bp_accounts a ON a.account_id = f.account_id AND a.tenant_id = f.tenant_id "
                                "LEFT JOIN nudges n ON n.flag_id = f.flag_id AND n.tenant_id = f.tenant_id "
                                "WHERE f.tenant_id = :tenant_id AND n.nudge_id IS NULL ORDER BY f.flagged_on, f.flag_id", database=DB)
    if flags.empty:
        return {"flags": 0}
    existing = db.query_df(tenant, "SELECT account_id, created_at FROM nudges WHERE tenant_id = :tenant_id AND status IN ('pending_approval', 'sent')", database=DB)
    history: dict[str, list[datetime]] = {}
    for a, c in zip(existing["account_id"], existing["created_at"]):
        history.setdefault(a, []).append(datetime.fromisoformat(str(c)))

    names = dict(zip(flags["account_id"], flags["customer_name"]))
    due_day = {a: int(str(d)[8:10]) for a, d in zip(flags["account_id"], flags["due_date"]) if d}
    rows, passed_flags = [], []
    for day_str, grp in flags.groupby("flagged_on"):
        day = pd.Timestamp(day_str)
        ctx = _flag_context(eng, list(grp["account_id"].unique()), day, names, due_day)
        for f in grp.itertuples():
            send_at = datetime(day.year, day.month, day.day, SEND_HOUR, 0)
            recent = sum(1 for t in history.get(f.account_id, []) if timedelta(0) <= send_at - t < timedelta(days=G.CAP_DAYS))
            c = ctx[f.account_id]
            n = draft_and_gate(templates, f.account_id, f.dominant_signal, c["params"], send_at, c["is_current"], recent, language)
            rows.append({"flag_id": int(f.flag_id), "account_id": f.account_id, "created_at": send_at.isoformat(), **n})
            if n["gate_passed"]:
                history.setdefault(f.account_id, []).append(send_at)
                passed_flags.append(int(f.flag_id))

    run_id = f"bi03-nudge-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
    db.insert(tenant, "agent_runs", {"run_id": run_id, "agent_name": "bi03_nudge", "entity_id": f"{len(rows)} flags", "status": "running"}, database=DB)
    db.insert_many(tenant, "nudges", rows, database=DB)
    for i in range(0, len(passed_flags), 500):
        chunk = {f"f{j}": v for j, v in enumerate(passed_flags[i:i + 500])}
        db.execute(tenant, "UPDATE flags SET status = 'nudged' WHERE tenant_id = :tenant_id AND status = 'open' AND flag_id IN (" + ", ".join(":" + k for k in chunk) + ")", chunk, database=DB)

    sent = 0
    if auto_send:
        ids = db.query_df(tenant, "SELECT nudge_id FROM nudges WHERE tenant_id = :tenant_id AND status = 'pending_approval'", database=DB)["nudge_id"]
        for nid in ids:
            nudge_dispatch(tenant, int(nid))
            sent += 1
    summary = {"flags": len(rows), "pending_approval": sum(r["gate_passed"] for r in rows), "blocked": sum(1 - r["gate_passed"] for r in rows), "sent": sent}
    steps = [("choose", {"flags": len(rows)}), ("draft", {"by_type": pd.Series([r["nudge_type"] for r in rows]).value_counts().to_dict()}),
             ("gate", {"passed": summary["pending_approval"], "blocked": summary["blocked"]}), ("dispatch", {"sent": sent})]
    db.insert_many(tenant, "agent_steps", [{"run_id": run_id, "step_no": i + 1, "tool": t, "input": "{}", "output": json.dumps(o)} for i, (t, o) in enumerate(steps)], database=DB)
    db.execute(tenant, "UPDATE agent_runs SET status = 'done', finished_at = CURRENT_TIMESTAMP WHERE tenant_id = :tenant_id AND run_id = :r", {"r": run_id}, database=DB)
    return summary


def reset_nudges(tenant: str) -> None:
    db.execute(tenant, "DELETE FROM nudges WHERE tenant_id = :tenant_id", database=DB)
    db.execute(tenant, "UPDATE flags SET status = 'open' WHERE tenant_id = :tenant_id AND status = 'nudged'", database=DB)


# ------------------------------------------------------------------ the (mock) dispatch tool
def nudge_dispatch(tenant: str, nudge_id: int, send_at: datetime | None = None) -> dict:
    """Send one nudge to the test number (mock). Refuses anything the compliance gate has not passed, and re-runs the gate's
    content and hours checks right before sending. send_at defaults to the nudge's scheduled time (simulation); pass
    datetime.now() in real operation."""
    n = db.query_one(tenant, "SELECT * FROM nudges WHERE tenant_id = :tenant_id AND nudge_id = :i", {"i": nudge_id}, database=DB)
    if n is None:
        raise GateError(f"nudge {nudge_id} does not exist")
    if n["status"] == "sent":
        raise GateError(f"nudge {nudge_id} was already sent")
    if not n["gate_passed"] or n["status"] != "pending_approval":
        raise GateError(f"refused: nudge {nudge_id} has not passed the compliance gate (status {n['status']})")
    meta = json.loads(n["gate_result"])
    tpl = db.query_one(tenant, "SELECT body FROM service_templates WHERE tenant_id = :tenant_id AND template_id = :t", {"t": n["template_id"]}, database=DB)
    if tpl is None:
        raise GateError(f"refused: template {n['template_id']} is not an approved template")
    when = send_at or datetime.fromisoformat(n["created_at"])
    res = G.evaluate(n["message"], render(tpl["body"], meta["params"], meta["language"]), when, True, 0)
    failed = res["content_failed"] + [c for c in res["context_failed"] if c == "allowed_hours"]
    if failed:
        raise GateError(f"refused at dispatch: {', '.join(failed)}")
    ref = "MOCK-WA-" + hashlib.sha1(f"{nudge_id}|{n['message']}".encode()).hexdigest()[:10].upper()
    db.execute(tenant, "UPDATE nudges SET status = 'sent', sent_at = :s, dispatch_ref = :r WHERE tenant_id = :tenant_id AND nudge_id = :i",
               {"s": when.isoformat(), "r": ref, "i": nudge_id}, database=DB)
    return {"nudge_id": nudge_id, "status": "sent", "dispatch_ref": ref, "sent_at": when.isoformat(), "to": "test number only (mock)"}


# ------------------------------------------------------------------ CLI
def _summary_tables(tenant: str) -> None:
    q = lambda sql: db.query_df(tenant, sql, database=DB)
    print("by status:", dict(q("SELECT status, COUNT(*) n FROM nudges WHERE tenant_id = :tenant_id GROUP BY status").values))
    print("by nudge type:", dict(q("SELECT nudge_type, COUNT(*) n FROM nudges WHERE tenant_id = :tenant_id GROUP BY 1").values))
    print("by dominant signal -> nudge type:")
    print(q("SELECT f.dominant_signal AS signal, n.nudge_type, COUNT(*) AS nudges FROM nudges n JOIN flags f ON f.flag_id = n.flag_id "
            "WHERE n.tenant_id = :tenant_id GROUP BY 1, 2 ORDER BY 3 DESC").to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process"); p.add_argument("--reset", action="store_true"); p.add_argument("--auto-send", action="store_true")
    d = sub.add_parser("dispatch"); d.add_argument("--nudge-id", type=int, required=True)
    s = sub.add_parser("show"); s.add_argument("--account", required=True)
    for x in (p, d, s):
        x.add_argument("--tenant", default="abc_bank")
    a = ap.parse_args()
    if a.cmd == "process":
        if a.reset:
            reset_nudges(a.tenant)
        print(process_flags(a.tenant, auto_send=a.auto_send))
        _summary_tables(a.tenant)
    elif a.cmd == "dispatch":
        print(nudge_dispatch(a.tenant, a.nudge_id))
    else:
        df = db.query_df(a.tenant, "SELECT nudge_id, flag_id, nudge_type, template_id, status, created_at, message FROM nudges WHERE tenant_id = :tenant_id AND account_id = :a ORDER BY nudge_id", {"a": a.account}, database=DB)
        print(df.to_string(index=False))
