"""
LC-04 role-based access. Everything a user can read goes through here.

    p = login("u_l1")                                   # scope is read from the user record in the database
    scoped_compute(p, "connect_rate", start, end, group_by=["city"])
    scoped_query(p, "SELECT ... FROM alerts WHERE tenant_id = :tenant_id ...", params)   # tenant-scoped output tables
    visible_catalog(p)                                  # only the metrics / dimensions this role may use

Guarantees (each one is tested in tests/test_lc04_access.py):
  - the lender scope comes from the user record, never from a request: scoped_compute / scoped_query have no scope parameter
  - a Principal that was forged or edited is useless: every call re-reads the user and role from the database and compares
  - the role limits metrics and dimensions; asking for something outside it raises AccessError (it is never silently empty)
  - lender filters can only narrow the scope; a lender outside it is refused
  - data is read one lender at a time through db.py (":tenant_id" is bound by the data layer), then combined
"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute as C  # noqa: E402
import registry  # noqa: E402
import roles  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db  # noqa: E402

DB = "lc04"


class AccessError(Exception):
    """The caller asked for something its role / scope does not allow (maps to HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    role_id: str
    role_name: str
    scope_type: str
    lenders: tuple
    metrics: frozenset
    dims: frozenset


def login(user_id: str, database: str = DB) -> Principal:
    u = db.query_one("GLOBAL", "SELECT * FROM lc_users WHERE tenant_id=:tenant_id AND user_id=:u", {"u": str(user_id)}, database)
    if not u:
        raise AccessError("unknown user")
    r = db.query_one("GLOBAL", "SELECT * FROM lc_roles WHERE tenant_id=:tenant_id AND role_id=:r", {"r": u["role_id"]}, database)
    if not r:
        raise AccessError("user has no valid role")
    lenders = roles.LENDERS if u["lender_scope"] == "*" else tuple(json.loads(u["lender_scope"]))
    if r["scope_type"] == "single_lender" and len(lenders) != 1:
        raise AccessError("a single-lender role must have exactly one lender")
    if not set(lenders) <= set(roles.LENDERS):
        raise AccessError("unknown lender in scope")
    mets = frozenset(registry.METRICS) if r["allowed_metrics"] == "*" else frozenset(json.loads(r["allowed_metrics"]))
    return Principal(u["user_id"], u["display_name"], r["role_id"], r["name"], r["scope_type"], tuple(sorted(lenders)),
                     mets & frozenset(registry.METRICS), frozenset(json.loads(r["allowed_dims"])))


def _verify(p) -> Principal:
    """Re-read the user and compare: a forged or modified Principal never reaches the data."""
    if not isinstance(p, Principal):
        raise AccessError("not a principal")
    if login(p.user_id) != p:
        raise AccessError("principal does not match the user record")
    return p


def effective_dims(p: Principal, metric_id: str):
    return [d for d in registry.get(metric_id).allowed_dims if d in p.dims]


def visible_catalog(p: Principal):
    p = _verify(p)
    out = []
    for m in registry.catalog():
        if m["metric_id"] in p.metrics:
            out.append({**m, "allowed_dims": [d for d in m["allowed_dims"] if d in p.dims]})
    return out


def check(p: Principal, metric_id, group_by=(), filters=None):
    if metric_id not in registry.METRICS:
        raise AccessError(f"{metric_id!r} is not a governed metric")
    if metric_id not in p.metrics:
        raise AccessError(f"your role ({p.role_name}) cannot see {metric_id}")
    filters = dict(filters or {})
    for d in list(group_by or []) + list(filters):
        if d not in p.dims:
            raise AccessError(f"your role ({p.role_name}) cannot use the dimension {d!r}")
    if "lender" in filters:
        want = filters["lender"]
        want = [want] if isinstance(want, str) else list(want)
        if not set(want) <= set(p.lenders):
            raise AccessError("that lender is outside your scope")


def scoped_compute(p: Principal, metric_id, start, end, group_by=(), grain=None, filters=None):
    """The governed metric for THIS user: scope from the user record, metric and dimensions limited by the role."""
    p = _verify(p)
    check(p, metric_id, group_by, filters)
    return C.compute(metric_id, C.Scope(p.lenders), start, end, group_by=group_by, grain=grain, filters=filters)


def scoped_query(p: Principal, sql: str, params: dict | None = None, database: str = DB) -> list[dict]:
    """Run server-written SQL (it must use :tenant_id) once per lender in the user's scope and concatenate the rows."""
    p = _verify(p)
    rows = []
    for t in p.lenders:
        rows += db.query(t, sql, params, database)
    return rows


if __name__ == "__main__":
    for uid in ("u_leader", "u_rm_a", "u_l1"):
        p = login(uid)
        print(uid, p.role_id, p.lenders, len(p.metrics), "metrics", sorted(p.dims))
