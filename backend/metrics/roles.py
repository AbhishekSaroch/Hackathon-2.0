"""
LC-04 roles and demo users. 5 roles; a role decides (a) which lenders' data it may read, (b) which governed metrics and
(c) which dimensions. The lender scope is a property of the USER record, never of a prompt or a request parameter.

  cn_leadership   all lenders, every metric, every dimension
  ops_head        all lenders, collections-operations metrics (calls, dispositions, PTPs, feed health)
  delivery_head   all lenders, payments / money / PTP metrics (gateways, collections)
  lender_rm       a SET of lenders (relationship manager), every metric
  lender_user     ONE lender, its own data only, no feed-health internals

Like the metric registry, this lives in code and is mirrored into lc_roles / lc_users (sync_to_db).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
from registry import DIMS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

LENDERS = ("L1", "L2", "L3", "L4", "L5")


def _ids(*families):
    return [m.metric_id for m in registry.all_metrics() if m.family in families]


ROLES = [
    dict(role_id="cn_leadership", name="CN leadership", scope_type="all_lenders", allowed_metrics="*",
         allowed_dims=list(DIMS), default_request="Show me what needs my attention across all lenders today"),
    dict(role_id="ops_head", name="Operations head", scope_type="all_lenders",
         allowed_metrics=_ids("contacts", "dispositions", "ptp", "volume", "feed", "compliance"),
         allowed_dims=["lender", "city", "agency", "dialer", "product", "dpd_bucket"],
         default_request="Where are calls and dispositions off track today, by city, agency and dialer?"),
    dict(role_id="delivery_head", name="Delivery head", scope_type="all_lenders",
         allowed_metrics=_ids("payments", "money", "ptp", "volume", "feed"),
         allowed_dims=["lender", "city", "gateway", "product", "dpd_bucket"],
         default_request="How are payments and collections doing today, and which gateway or lender is off?"),
    dict(role_id="lender_rm", name="Lender relationship manager", scope_type="lender_set", allowed_metrics="*",
         allowed_dims=list(DIMS), default_request="Show me how my lenders' collections are doing today"),
    dict(role_id="lender_user", name="Lender user", scope_type="single_lender",
         allowed_metrics=_ids("contacts", "dispositions", "ptp", "payments", "money", "compliance"),
         allowed_dims=["city", "agency", "product", "dpd_bucket"],
         default_request="Show me how my collections are doing today"),
]

USERS = [
    dict(user_id="u_leader", display_name="Asha (CN leadership)", role_id="cn_leadership", lender_scope="*"),
    dict(user_id="u_ops", display_name="Rohan (Operations head)", role_id="ops_head", lender_scope="*"),
    dict(user_id="u_delivery", display_name="Meera (Delivery head)", role_id="delivery_head", lender_scope="*"),
    dict(user_id="u_rm_a", display_name="Kabir (RM: L1, L3)", role_id="lender_rm", lender_scope=["L1", "L3"]),
    dict(user_id="u_rm_b", display_name="Nisha (RM: L2, L4, L5)", role_id="lender_rm", lender_scope=["L2", "L4", "L5"]),
] + [dict(user_id=f"u_{l.lower()}", display_name=f"{l} user", role_id="lender_user", lender_scope=[l]) for l in LENDERS]


def validate():
    ids = {r["role_id"] for r in ROLES}
    assert len(ROLES) == 5 and len(ids) == 5
    for r in ROLES:
        assert set(r["allowed_dims"]) <= set(DIMS)
        if r["allowed_metrics"] != "*":
            assert set(r["allowed_metrics"]) <= set(registry.METRICS)
    for u in USERS:
        role = next(r for r in ROLES if r["role_id"] == u["role_id"])
        sc = u["lender_scope"]
        if role["scope_type"] == "all_lenders":
            assert sc == "*"
        elif role["scope_type"] == "single_lender":
            assert isinstance(sc, list) and len(sc) == 1 and sc[0] in LENDERS
        else:
            assert isinstance(sc, list) and len(sc) >= 1 and set(sc) <= set(LENDERS)


def sync_to_db(database: str = "lc04") -> tuple[int, int]:
    validate()
    for t in ("lc_users", "lc_roles"):
        db.execute("GLOBAL", f"DELETE FROM {t} WHERE tenant_id=:tenant_id", database=database)
    roles = [dict(role_id=r["role_id"], name=r["name"], scope_type=r["scope_type"],
                  allowed_metrics=r["allowed_metrics"] if r["allowed_metrics"] == "*" else json.dumps(r["allowed_metrics"]),
                  allowed_dims=json.dumps(r["allowed_dims"]), default_request=r["default_request"]) for r in ROLES]
    users = [dict(user_id=u["user_id"], display_name=u["display_name"], role_id=u["role_id"],
                  lender_scope=u["lender_scope"] if u["lender_scope"] == "*" else json.dumps(u["lender_scope"])) for u in USERS]
    return (db.insert_many("GLOBAL", "lc_roles", roles, database=database),
            db.insert_many("GLOBAL", "lc_users", users, database=database))


if __name__ == "__main__":
    db.init_db("lc04")
    print("roles, users written:", sync_to_db())
