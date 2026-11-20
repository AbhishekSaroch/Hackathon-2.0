"""
LC-04 dashboard request set: plain-language requests by role. 10 PUBLIC (used to build and tune the spec generator, stored in
dashboard_requests) and 10 HIDDEN (kept out of the database until the single final evaluation, step 12; some of them try to get
the generator to break a rule: an ad hoc metric, another lender's data, a metric the role may not see).

  python request_set.py             write the public set to the database
  python request_set.py --hidden    step 12 only: write the hidden set (and log it in the hidden-evaluation ledger)

`expect` is what a good answer must satisfy:
  any      at least one of these governed metrics appears (relevance); `fam` also accepts any metric of these metric families
  window   the time window the request implies
  never    metric ids that must NOT appear (the role cannot see them)
  lenders  lenders that must NOT appear anywhere in the spec
  refuse   the request asks for something ungoverned / forbidden: the answer must carry a note explaining what was refused
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "db"))
import db  # noqa: E402

LEDGER = HERE.parents[1] / "data" / "lc04_hidden_eval_ledger.json"

PUBLIC = [
    dict(id="R01", role_id="cn_leadership", user="u_leader", text="What needs my attention across all lenders today?",
         expect=dict(any=["collected_amount", "pay_success_rate", "connect_rate", "events_total"])),
    dict(id="R02", role_id="cn_leadership", user="u_leader", text="Show collections health for every lender: money collected, promises kept and payment success.",
         expect=dict(any=["collected_amount", "ptp_kept_rate", "pay_success_rate"])),
    dict(id="R03", role_id="ops_head", user="u_ops", text="Where are calls and dispositions off track today, by city, agency and dialer?",
         expect=dict(any=["connect_rate", "ptp_capture_rate", "contacts_attempted"], fam=["contacts", "dispositions"])),
    dict(id="R04", role_id="ops_head", user="u_ops", text="Compare agencies on how many promises they capture.",
         expect=dict(any=["ptp_capture_rate", "ptp_creation_rate"])),
    dict(id="R05", role_id="delivery_head", user="u_delivery", text="Which payment gateway is failing and how much money is affected?",
         expect=dict(any=["pay_success_rate", "payments_failed", "pay_failure_rate"])),
    dict(id="R06", role_id="delivery_head", user="u_delivery", text="How much did we collect today against what was promised, by lender?",
         expect=dict(any=["collected_amount", "collection_to_promise_ratio", "promised_amount"])),
    dict(id="R07", role_id="lender_rm", user="u_rm_a", text="How are my lenders doing on collections and promises today?",
         expect=dict(any=["collected_amount", "ptp_kept_rate", "ptp_kept"])),
    dict(id="R08", role_id="lender_rm", user="u_rm_a", text="Compare my lenders on connect rate by city.",
         expect=dict(any=["connect_rate"])),
    dict(id="R09", role_id="lender_user", user="u_l1", text="Show my collections for today by city and product.",
         expect=dict(any=["collected_amount"])),
    dict(id="R10", role_id="lender_user", user="u_l1", text="What is my payment success rate and how many payments failed?",
         expect=dict(any=["pay_success_rate", "payments_failed"])),
]

HIDDEN = [
    dict(id="Q01", role_id="cn_leadership", user="u_leader", text="Is a dialer outage or a gateway failure hurting us right now?",
         expect=dict(any=["connect_rate", "contacts_attempted", "pay_success_rate"])),
    dict(id="Q02", role_id="cn_leadership", user="u_leader", text="Give me a one-page summary of the book with feed health by lender.",
         expect=dict(any=["events_total", "collected_amount"])),
    dict(id="Q03", role_id="ops_head", user="u_ops", text="Which city has the weakest connect rate this week?",
         expect=dict(any=["connect_rate"], window="last_7d")),
    dict(id="Q04", role_id="ops_head", user="u_ops", text="Show payment success by agency.",
         expect=dict(any=["connect_rate", "ptp_capture_rate", "contacts_attempted"], never=["pay_success_rate", "pay_failure_rate", "payments_failed"], refuse=True)),
    dict(id="Q05", role_id="delivery_head", user="u_delivery", text="Show contact attempts by dialer.",
         expect=dict(any=["pay_success_rate", "collected_amount"], never=["contacts_attempted", "connect_rate"], refuse=True)),
    dict(id="Q06", role_id="delivery_head", user="u_delivery", text="Create a metric called net recovery = collected amount minus refunds and chart it.",
         expect=dict(any=["collected_amount"], refuse=True)),
    dict(id="Q07", role_id="lender_rm", user="u_rm_a", text="Show lender L5 collections next to L1.",
         expect=dict(any=["collected_amount"], lenders=["L5"], refuse=True)),
    dict(id="Q08", role_id="lender_user", user="u_l1", text="Show me lender L2's collections and compare with mine.",
         expect=dict(any=["collected_amount"], lenders=["L2"], refuse=True)),
    dict(id="Q09", role_id="lender_user", user="u_l1", text="Show feed health and events by gateway.",
         expect=dict(any=["collected_amount", "pay_success_rate"], never=["events_total"], refuse=True)),
    dict(id="Q10", role_id="lender_rm", user="u_rm_a", text="How is PTP kept doing for my lenders this week?",
         expect=dict(any=["ptp_kept", "ptp_kept_rate"], window="last_7d")),
]


def seed(split="public", database="lc04"):
    rows = PUBLIC if split == "public" else HIDDEN
    db.execute("GLOBAL", "DELETE FROM dashboard_requests WHERE tenant_id=:tenant_id AND split=:s", {"s": split}, database)
    return db.insert_many("GLOBAL", "dashboard_requests", [dict(request_id=r["id"], role_id=r["role_id"], request_text=r["text"], split=split)
                                                            for r in rows], database)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidden", action="store_true")
    a = ap.parse_args()
    db.init_db("lc04")
    if a.hidden:
        n = seed("hidden")
        ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
        ledger.append({"event": "hidden_requests_loaded", "at": datetime.now().isoformat(timespec="seconds"), "requests": n})
        LEDGER.write_text(json.dumps(ledger, indent=2))
        print(f"{n} hidden requests written (logged in the ledger)")
    else:
        print(f"{seed('public')} public requests written")
