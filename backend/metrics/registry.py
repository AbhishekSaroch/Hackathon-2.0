"""
LC-04 governed metric registry: the ONLY allowed metric definitions (shared with LC-01 later).

A metric is a formula over the cube's base counters (cube_15m): numerator / denominator (or a plain sum), a unit, a
"good direction", the dimensions it may be sliced by, an owner and a version. Nothing else can be shown on a view:
the spec generator, the detector, the drill-down and the UI all call compute.py with a metric id from here.

Allowed dimensions are DERIVED from the counters a metric uses, so a view can never slice a metric by a dimension
that does not exist for it (a payment has a gateway but no agency or dialer; a call has an agency and a dialer but no gateway).
The registry lives in code (diffable, testable) and is mirrored into the metric_defs table (sync_to_db) for LC-01 / the UI.
"""
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "agents" / "lc04"))
import cube  # noqa: E402
import db  # noqa: E402

DIMS = ["lender", "city", "agency", "dialer", "gateway", "product", "dpd_bucket"]
DIM_COL = {"lender": "tenant_id", "city": "city", "agency": "agency_id", "dialer": "dialer_id", "gateway": "gateway_id",
           "product": "product", "dpd_bucket": "bucket"}
_CONTACT = {"lender", "city", "agency", "dialer", "product", "dpd_bucket"}
_PTP = {"lender", "city", "agency", "product", "dpd_bucket"}
_PAY = {"lender", "city", "gateway", "product", "dpd_bucket"}
COUNTER_DIMS = {"ev_total": set(DIMS), "pay_amount": _PAY, "ptp_amount": _PTP}
for _c in cube.COUNTER_COLS:
    if _c.startswith(("contacts_", "disp_")):
        COUNTER_DIMS[_c] = _CONTACT
    elif _c.startswith("ptp_"):
        COUNTER_DIMS[_c] = _PTP
    elif _c.startswith("pay_"):
        COUNTER_DIMS[_c] = _PAY
    elif _c.startswith("legal_"):
        COUNTER_DIMS[_c] = _PTP                      # a legal communication has a sending agency, a city, a product and a bucket
UNITS = {"count": 1, "rs": 1, "pct": 100, "ratio": 1}      # unit -> multiplier applied to numerator / denominator
_TERM = re.compile(r"\s*([+-]?)\s*([a-z_]+)\s*")


class MetricError(Exception):
    """Raised for anything that would break governance: unknown metric, dimension or counter."""


def parse_terms(expr: str):
    """'a + b - c' -> [('a', 1), ('b', 1), ('c', -1)]. Only cube counters are allowed."""
    pos, out = 0, []
    while pos < len(expr):
        m = _TERM.match(expr, pos)
        if not m or m.end() == pos:
            raise MetricError(f"cannot parse {expr!r}")
        if m.group(2) not in cube.COUNTER_COLS:
            raise MetricError(f"unknown counter {m.group(2)!r} in {expr!r}")
        if out and not m.group(1):
            raise MetricError(f"missing + or - in {expr!r}")
        out.append((m.group(2), -1 if m.group(1) == "-" else 1))
        pos = m.end()
    if not out:
        raise MetricError("empty expression")
    return out


@dataclass(frozen=True)
class Metric:
    metric_id: str
    name: str
    description: str
    unit: str
    numerator: str
    denominator: str | None
    good_direction: str          # up | down | neutral
    is_key: bool                 # monitored by the detector
    owner: str
    family: str
    version: int = 1

    @property
    def num_terms(self):
        return parse_terms(self.numerator)

    @property
    def den_terms(self):
        return parse_terms(self.denominator) if self.denominator else None

    @property
    def allowed_dims(self):
        used = [c for c, _ in self.num_terms] + [c for c, _ in (self.den_terms or [])]
        dims = set(DIMS)
        for c in used:
            dims &= COUNTER_DIMS[c]
        return [d for d in DIMS if d in dims]

    @property
    def formula(self):
        f = self.numerator if not self.denominator else f"({self.numerator}) / ({self.denominator})"
        return f + (" x 100" if self.unit == "pct" else "")


# id, name, description, unit, numerator, denominator, good direction, key?, owner, family
_C, _R, _M, _O = "Collections Ops", "Collections Ops", "Collections Finance", "Data Platform"
_K = "Compliance"
_P = "Payments"
_DEFS = [
    # ---------------------------------------------------------------- volumes (21)
    ("events_total", "Events received", "All events the system received in the period (volume and feed health)", "count", "ev_total", None, "neutral", True, _O, "volume"),
    ("contacts_attempted", "Contact attempts", "Outbound contact attempts made", "count", "contacts_attempted", None, "up", True, _C, "contacts"),
    ("contacts_connected", "Connected calls", "Contact attempts that reached the customer", "count", "contacts_connected", None, "up", False, _C, "contacts"),
    ("contacts_not_connected", "Calls not connected", "Attempts that did not connect (no answer or dialer failure)", "count", "contacts_attempted - contacts_connected", None, "down", False, _C, "contacts"),
    ("dispositions_total", "Dispositions recorded", "Call outcomes recorded by agents", "count", "disp_total", None, "neutral", False, _C, "dispositions"),
    ("disp_rpc_count", "RPC dispositions", "Right-party contacts without a promise", "count", "disp_rpc", None, "up", False, _C, "dispositions"),
    ("disp_ptp_count", "PTP dispositions", "Calls ended with a promise to pay", "count", "disp_ptp", None, "up", False, _C, "dispositions"),
    ("disp_refused_count", "Refused dispositions", "Customer refused to pay", "count", "disp_refused", None, "down", False, _C, "dispositions"),
    ("disp_dispute_count", "Dispute dispositions", "Customer disputed the dues", "count", "disp_dispute", None, "down", False, _C, "dispositions"),
    ("disp_other_count", "Other dispositions", "Wrong number or call-back requested", "count", "disp_other", None, "neutral", False, _C, "dispositions"),
    ("positive_dispositions", "Positive dispositions", "RPC plus PTP dispositions", "count", "disp_rpc + disp_ptp", None, "up", False, _C, "dispositions"),
    ("negative_dispositions", "Negative dispositions", "Refused plus dispute dispositions", "count", "disp_refused + disp_dispute", None, "down", False, _C, "dispositions"),
    ("unexplained_dispositions", "Dispositions without a connected call", "Dispositions minus connected calls; far from zero means dispositions that did not happen", "count", "disp_total - contacts_connected", None, "down", False, _C, "dispositions"),
    ("ptp_created", "PTPs created", "New promises to pay", "count", "ptp_created", None, "up", False, _C, "ptp"),
    ("ptp_kept", "PTPs kept", "Promises honoured", "count", "ptp_kept", None, "up", True, _C, "ptp"),
    ("ptp_broken", "PTPs broken", "Promises not honoured", "count", "ptp_broken", None, "down", False, _C, "ptp"),
    ("ptp_resolved", "PTPs resolved", "Promises kept or broken", "count", "ptp_kept + ptp_broken", None, "neutral", False, _C, "ptp"),
    ("payments_attempted", "Payment attempts", "Payment attempts through a gateway", "count", "pay_attempts", None, "up", False, _P, "payments"),
    ("payments_success", "Successful payments", "Payment attempts that succeeded", "count", "pay_success", None, "up", False, _P, "payments"),
    ("payments_failed", "Failed payments", "Payment attempts that failed", "count", "pay_failed", None, "down", False, _P, "payments"),
    # ---------------------------------------------------------------- rates (24)
    ("connect_rate", "Connect rate", "Share of attempts that connected", "pct", "contacts_connected", "contacts_attempted", "up", True, _C, "contacts"),
    ("non_connect_rate", "Non-connect rate", "Share of attempts that did not connect", "pct", "contacts_attempted - contacts_connected", "contacts_attempted", "down", False, _C, "contacts"),
    ("disposition_coverage", "Disposition coverage", "Dispositions per connected call; about 100 when agents record what happened", "pct", "disp_total", "contacts_connected", "neutral", True, _C, "dispositions"),
    ("rpc_rate", "RPC rate", "RPC dispositions per connected call", "pct", "disp_rpc", "contacts_connected", "up", False, _C, "dispositions"),
    ("ptp_capture_rate", "PTP capture rate", "PTP dispositions per connected call", "pct", "disp_ptp", "contacts_connected", "up", True, _C, "dispositions"),
    ("refusal_rate", "Refusal rate", "Refused dispositions per connected call", "pct", "disp_refused", "contacts_connected", "down", False, _C, "dispositions"),
    ("dispute_rate", "Dispute rate", "Dispute dispositions per connected call", "pct", "disp_dispute", "contacts_connected", "down", False, _C, "dispositions"),
    ("other_disposition_rate", "Other disposition rate", "Wrong-number or call-back dispositions per connected call", "pct", "disp_other", "contacts_connected", "neutral", False, _C, "dispositions"),
    ("positive_disposition_rate", "Positive disposition rate", "RPC plus PTP dispositions per connected call", "pct", "disp_rpc + disp_ptp", "contacts_connected", "up", False, _C, "dispositions"),
    ("negative_disposition_rate", "Negative disposition rate", "Refused plus dispute dispositions per connected call", "pct", "disp_refused + disp_dispute", "contacts_connected", "down", False, _C, "dispositions"),
    ("ptp_share_of_dispositions", "PTP share of dispositions", "PTP dispositions as a share of all dispositions", "pct", "disp_ptp", "disp_total", "neutral", False, _C, "dispositions"),
    ("rpc_share_of_dispositions", "RPC share of dispositions", "RPC dispositions as a share of all dispositions", "pct", "disp_rpc", "disp_total", "neutral", False, _C, "dispositions"),
    ("refused_share_of_dispositions", "Refused share of dispositions", "Refused dispositions as a share of all dispositions", "pct", "disp_refused", "disp_total", "down", False, _C, "dispositions"),
    ("dispute_share_of_dispositions", "Dispute share of dispositions", "Dispute dispositions as a share of all dispositions", "pct", "disp_dispute", "disp_total", "down", False, _C, "dispositions"),
    ("other_share_of_dispositions", "Other share of dispositions", "Wrong-number or call-back dispositions as a share of all dispositions", "pct", "disp_other", "disp_total", "neutral", False, _C, "dispositions"),
    ("ptp_kept_rate", "PTP kept rate", "Kept promises as a share of resolved promises", "pct", "ptp_kept", "ptp_kept + ptp_broken", "up", False, _C, "ptp"),
    ("ptp_broken_rate", "PTP broken rate", "Broken promises as a share of resolved promises", "pct", "ptp_broken", "ptp_kept + ptp_broken", "down", False, _C, "ptp"),
    ("ptp_creation_rate", "PTP creation rate", "New promises per connected call", "pct", "ptp_created", "contacts_connected", "up", False, _C, "ptp"),
    ("ptp_per_attempt_rate", "PTPs per attempt", "New promises per contact attempt", "pct", "ptp_created", "contacts_attempted", "up", False, _C, "ptp"),
    ("ptp_realisation_ratio", "PTP realisation", "Kept promises per new promise in the same period (lagged, trend only)", "pct", "ptp_kept", "ptp_created", "up", False, _C, "ptp"),
    ("pay_success_rate", "Payment success rate", "Share of payment attempts that succeeded", "pct", "pay_success", "pay_attempts", "up", True, _P, "payments"),
    ("pay_failure_rate", "Payment failure rate", "Share of payment attempts that failed", "pct", "pay_failed", "pay_attempts", "down", False, _P, "payments"),
    ("payments_per_connected_call", "Payment attempts per connected call", "Payment attempts per connected call", "ratio", "pay_attempts", "contacts_connected", "up", False, _C, "payments"),
    ("payments_per_attempt", "Payment attempts per contact attempt", "Payment attempts per contact attempt", "ratio", "pay_attempts", "contacts_attempted", "up", False, _C, "payments"),
    # ---------------------------------------------------------------- money (8)
    ("collected_amount", "Amount collected", "Rupees collected through successful payments", "rs", "pay_amount", None, "up", True, _M, "money"),
    ("promised_amount", "Amount promised", "Rupees promised in new PTPs", "rs", "ptp_amount", None, "up", False, _M, "money"),
    ("avg_payment_amount", "Average payment", "Rupees per successful payment", "rs", "pay_amount", "pay_success", "neutral", False, _M, "money"),
    ("avg_promised_amount", "Average promise", "Rupees per new PTP", "rs", "ptp_amount", "ptp_created", "neutral", False, _M, "money"),
    ("collection_to_promise_ratio", "Collected vs promised", "Rupees collected per rupee promised in the same period", "pct", "pay_amount", "ptp_amount", "up", False, _M, "money"),
    ("collected_per_connected_call", "Collected per connected call", "Rupees collected per connected call", "rs", "pay_amount", "contacts_connected", "up", False, _M, "money"),
    ("promised_per_connected_call", "Promised per connected call", "Rupees promised per connected call", "rs", "ptp_amount", "contacts_connected", "up", False, _M, "money"),
    ("collected_per_payment_attempt", "Collected per payment attempt", "Rupees collected per payment attempt", "rs", "pay_amount", "pay_attempts", "up", False, _M, "money"),
    # ---------------------------------------------------------------- compliance (4): legal communications and the 08:00-20:00 window
    ("legal_notices_sent", "Legal notices sent", "Legal communications sent to customers", "count", "legal_sent", None, "neutral", False, _K, "compliance"),
    ("legal_notices_out_of_window", "Legal notices outside the permitted window",
     "Legal communications sent before 08:00 or from 20:00, judged on the send time (zero is the only acceptable number)", "count", "legal_out_of_window", None, "down", True, _K, "compliance"),
    ("legal_notices_in_window", "Legal notices inside the permitted window", "Legal communications sent between 08:00 and 20:00", "count", "legal_sent - legal_out_of_window", None, "up", False, _K, "compliance"),
    ("legal_out_of_window_rate", "Legal notices outside the window, share", "Share of legal communications sent outside 08:00-20:00", "pct", "legal_out_of_window", "legal_sent", "down", False, _K, "compliance"),
    # ---------------------------------------------------------------- feed health (7)
    ("events_per_contact_attempt", "Events per contact attempt", "All events per contact attempt (feed mix)", "ratio", "ev_total", "contacts_attempted", "neutral", False, _O, "feed"),
    ("contact_share_of_events", "Contact share of events", "Contact events as a share of all events", "pct", "contacts_attempted", "ev_total", "neutral", False, _O, "feed"),
    ("disposition_share_of_events", "Disposition share of events", "Disposition events as a share of all events", "pct", "disp_total", "ev_total", "neutral", False, _O, "feed"),
    ("payment_share_of_events", "Payment share of events", "Payment events as a share of all events", "pct", "pay_attempts", "ev_total", "neutral", False, _O, "feed"),
    ("ptp_share_of_events", "PTP share of events", "PTP events as a share of all events", "pct", "ptp_created + ptp_kept + ptp_broken", "ev_total", "neutral", False, _O, "feed"),
    ("failed_payment_share_of_events", "Failed-payment share of events", "Failed payments as a share of all events", "pct", "pay_failed", "ev_total", "down", False, _O, "feed"),
    ("connected_share_of_events", "Connected share of events", "Connected calls as a share of all events", "pct", "contacts_connected", "ev_total", "neutral", False, _O, "feed"),
]

METRICS = {}
for _row in _DEFS:
    _m = Metric(*_row)
    if _m.metric_id in METRICS:
        raise MetricError(f"duplicate metric id {_m.metric_id}")
    if _m.unit not in UNITS:
        raise MetricError(f"bad unit for {_m.metric_id}")
    _m.num_terms, _m.den_terms, _m.allowed_dims          # parse + validate at import time
    if not _m.allowed_dims:
        raise MetricError(f"{_m.metric_id} has no sliceable dimension")
    METRICS[_m.metric_id] = _m


def get(metric_id: str) -> Metric:
    if metric_id not in METRICS:
        raise MetricError(f"{metric_id!r} is not a governed metric")
    return METRICS[metric_id]


def all_metrics():
    return list(METRICS.values())


def key_metrics():
    return [m for m in METRICS.values() if m.is_key]


def catalog():
    """JSON-friendly catalog for the API / UI / LC-01."""
    return [dict(metric_id=m.metric_id, name=m.name, description=m.description, unit=m.unit, formula=m.formula,
                 good_direction=m.good_direction, allowed_dims=m.allowed_dims, is_key=m.is_key, owner=m.owner,
                 family=m.family, version=m.version) for m in METRICS.values()]


def sync_to_db(database: str = "lc04") -> int:
    """Mirror the registry into metric_defs (tenant 'GLOBAL'). The code registry is the source of truth."""
    db.execute("GLOBAL", "DELETE FROM metric_defs WHERE tenant_id=:tenant_id", database=database)
    rows = [dict(metric_id=m.metric_id, name=m.name, description=m.description, unit=m.unit, numerator=m.numerator,
                 denominator=m.denominator, good_direction=m.good_direction, allowed_dims=json.dumps(m.allowed_dims),
                 is_key=int(m.is_key), owner=m.owner, version=m.version) for m in METRICS.values()]
    return db.insert_many("GLOBAL", "metric_defs", rows, database=database)


if __name__ == "__main__":
    db.init_db("lc04")
    print(f"{len(METRICS)} governed metrics ({len(key_metrics())} key); {sync_to_db()} rows written to metric_defs")
