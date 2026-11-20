"""
LC-04 dashboard spec generator: a role + a plain-language request -> a dashboard SPEC (metrics, charts, filters).

    gen = SpecGenerator()                      # llm = llm.groq_client.chat_json (or a stub in tests)
    out = gen.generate(principal, "Which gateway is failing and how much money is affected?")
    out["spec"]      the validated spec (JSON)           out["source"]  llm | fallback | rebuilt
    out["notes"]     what was dropped or refused and why  out["ms"]      generation time
    spec2 = gen.rebuild(principal, spec, incident)      # "rebuild the view around the finding"

Where the AI sits: the LLM turns the request into a draft spec. EVERYTHING it says is then validated in code against the
governed catalog and the user's role (metric ids, dimensions, filter values, lender scope, panel types, sizes); anything outside is
dropped and reported, ad hoc definitions (formula / SQL / "create a metric") are refused, and if nothing valid is left (or the LLM
is unavailable) a deterministic fallback spec is built from keywords and the role's defaults. So a published view can only ever
contain governed metrics the viewer may see, whatever the model returns.
The spec is DATA: how it is rendered (numbers) goes through access.scoped_compute, i.e. the governed definitions and the role's scope.
"""
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "metrics"))
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(HERE))
import access as A  # noqa: E402
import db  # noqa: E402
import drill as DR  # noqa: E402
import registry  # noqa: E402

PANEL_TYPES = ("kpi", "timeseries", "breakdown", "table")
GRAINS = ("bucket", "hour", "day")
WINDOWS = ("today", "last_24h", "last_7d")
MAX_PANELS = 12
REFRESH_SECONDS = 900
FORBIDDEN_KEYS = {"formula", "expression", "sql", "definition", "numerator", "denominator", "query"}
ADHOC = re.compile(r"\b(create|define|invent|new|custom|my own)\b.{0,40}\bmetric\b|\bformula\b|\bminus\b.{0,30}\bmetric\b", re.I)

ROLE_DEFAULTS = {                      # what each role sees when it asks for nothing in particular
    "cn_leadership": [("kpi", "collected_amount"), ("kpi", "pay_success_rate"), ("kpi", "connect_rate"), ("kpi", "legal_notices_out_of_window"),
                      ("timeseries", "collected_amount", [], "hour"), ("timeseries", "contacts_attempted", [], "bucket"),
                      ("breakdown", "collected_amount", ["lender"]), ("breakdown", "pay_success_rate", ["gateway"]),
                      ("breakdown", "legal_notices_out_of_window", ["agency"]), ("breakdown", "connect_rate", ["city"])],
    "ops_head": [("kpi", "connect_rate"), ("kpi", "contacts_attempted"), ("kpi", "ptp_capture_rate"), ("kpi", "legal_notices_out_of_window"),
                 ("timeseries", "contacts_attempted", [], "bucket"), ("timeseries", "connect_rate", [], "hour"),
                 ("breakdown", "connect_rate", ["city"]), ("breakdown", "ptp_capture_rate", ["agency"]),
                 ("breakdown", "legal_notices_out_of_window", ["agency"])],
    "delivery_head": [("kpi", "pay_success_rate"), ("kpi", "collected_amount"), ("kpi", "payments_failed"), ("kpi", "collection_to_promise_ratio"),
                      ("timeseries", "pay_success_rate", [], "hour"), ("timeseries", "collected_amount", [], "hour"),
                      ("breakdown", "pay_success_rate", ["gateway"]), ("breakdown", "collected_amount", ["lender"])],
    "lender_rm": [("kpi", "collected_amount"), ("kpi", "ptp_kept_rate"), ("kpi", "pay_success_rate"), ("kpi", "legal_notices_out_of_window"),
                  ("timeseries", "collected_amount", [], "hour"), ("breakdown", "collected_amount", ["lender"]),
                  ("breakdown", "legal_notices_out_of_window", ["lender"]), ("breakdown", "connect_rate", ["city"])],
    "lender_user": [("kpi", "collected_amount"), ("kpi", "ptp_kept_rate"), ("kpi", "pay_success_rate"), ("kpi", "legal_notices_out_of_window"),
                    ("timeseries", "collected_amount", [], "hour"), ("breakdown", "collected_amount", ["city"]),
                    ("breakdown", "legal_notices_out_of_window", ["agency"]), ("breakdown", "connect_rate", ["dpd_bucket"])],
}
# plain keywords -> metric families / dimensions, for the fallback and for hinting (code, not AI)
KEYWORDS = [
    (r"payment|gateway|paid|success rate|failing|fail", {"metrics": ["pay_success_rate", "payments_failed", "payments_attempted"], "label": "payment"}),
    (r"money|collect|rupee|amount|recover", {"metrics": ["collected_amount", "promised_amount", "collection_to_promise_ratio"], "label": "money / collections"}),
    (r"ptp|promise|kept", {"metrics": ["ptp_kept", "ptp_kept_rate", "ptp_capture_rate"], "label": "promise (PTP)"}),
    (r"call|dialer|connect|contact|dial", {"metrics": ["contacts_attempted", "connect_rate", "contacts_connected"], "label": "call and contact"}),
    (r"disposition|agent|agency", {"metrics": ["ptp_capture_rate", "disposition_coverage", "rpc_rate"], "label": "disposition"}),
    (r"feed health|feed|volume|events|stale|delay", {"metrics": ["events_total"], "label": "feed-health"}),
    (r"legal|compliance|notice|window|off.?hours", {"metrics": ["legal_notices_out_of_window", "legal_notices_sent", "legal_out_of_window_rate"], "label": "legal / compliance"}),
]
WINDOW_WORDS = [(r"this week|last 7|past week|weekly|last week|7 days", "last_7d"), (r"last 24|past day|24 hours|yesterday", "last_24h")]
DIM_WORDS = {"city": r"\bcit(y|ies)\b", "agency": r"\bagenc(y|ies)\b", "dialer": r"\bdialers?\b", "gateway": r"\bgateways?\b",
             "product": r"\bproducts?\b", "dpd_bucket": r"\bbuckets?\b|\bdpd\b", "lender": r"\blenders?\b"}


def _clean_text(s, n=80):
    return re.sub(r"[<>`{}\\]", "", str(s or "")).strip()[:n]


class SpecGenerator:
    def __init__(self, llm=None, model=None):
        self.llm, self.model = llm, model
        self._dim_values = None

    # ---------------------------------------------------------------- reference data
    def dim_values(self, database="lc04"):
        if self._dim_values is None:
            rows = db.query("GLOBAL", "SELECT dimension, value_id FROM lc_d13 WHERE tenant_id=:tenant_id", database=database)
            out = {}
            for r in rows:
                out.setdefault("dpd_bucket" if r["dimension"] == "bucket" else r["dimension"], []).append(r["value_id"])
            out["lender"] = ["L1", "L2", "L3", "L4", "L5"]
            self._dim_values = out
        return self._dim_values

    # ---------------------------------------------------------------- validation (the guardrail)
    def validate(self, p: A.Principal, raw) -> tuple[dict, list]:
        """Draft -> a spec that can only contain governed metrics the viewer may see. Returns (spec, notes)."""
        notes = []
        if not isinstance(raw, dict) or not isinstance(raw.get("panels"), list):
            return {"panels": []}, ["the draft had no panels"]
        if FORBIDDEN_KEYS & set(map(str.lower, map(str, raw))):
            notes.append("ad hoc definitions are not allowed on a published view: only governed metrics can be shown")
        values = self.dim_values()
        panels, seen = [], set()
        for i, pn in enumerate(raw["panels"][:40]):
            if not isinstance(pn, dict):
                continue
            if FORBIDDEN_KEYS & set(map(str.lower, map(str, pn))):
                notes.append(f"panel {i + 1}: ignored an ad hoc definition")
            mid, typ = pn.get("metric_id"), pn.get("type")
            if typ not in PANEL_TYPES:
                notes.append(f"panel {i + 1}: unknown panel type {typ!r}")
                continue
            if mid not in registry.METRICS:
                notes.append(f"panel {i + 1}: {str(mid)[:40]!r} is not a governed metric, dropped")
                continue
            if mid not in p.metrics:
                notes.append(f"panel {i + 1}: your role ({p.role_name}) cannot see {mid}, dropped")
                continue
            ok_dims = set(A.effective_dims(p, mid))
            gb = [g for g in (pn.get("group_by") or []) if isinstance(g, str)]
            bad = [g for g in gb if g not in ok_dims]
            if bad:
                notes.append(f"panel {i + 1}: {mid} cannot be sliced by {', '.join(bad)} for your role; those were dropped")
            gb = [g for g in dict.fromkeys(gb) if g in ok_dims][:2]
            flt = {}
            for d, v in (pn.get("filters") or {}).items():
                vs = [v] if isinstance(v, str) else [x for x in (v if isinstance(v, list) else []) if isinstance(x, str)]
                vs = [x.strip() for s_ in vs for x in s_.split(",") if x.strip()]          # a model may send "L1,L2" as one string
                if d not in ok_dims:
                    notes.append(f"panel {i + 1}: filter on {d!r} is not available to you, dropped")
                    continue
                if d == "lender":
                    outside = [x for x in vs if x not in p.lenders]
                    if outside:
                        notes.append(f"panel {i + 1}: lender {', '.join(outside)} is outside your scope, dropped")
                    vs = [x for x in vs if x in p.lenders]
                known = [x for x in vs if x in values.get(d, [])]
                if len(known) < len(vs):
                    notes.append(f"panel {i + 1}: unknown {d} value(s) dropped")
                if known:
                    flt[d] = known if len(known) > 1 else known[0]
            grain = pn.get("grain")
            if typ == "timeseries":
                grain = grain if grain in GRAINS else "bucket"
                gb = gb[:1]
            elif typ in ("breakdown", "table"):
                grain = None
                if not gb:
                    notes.append(f"panel {i + 1}: a {typ} needs a dimension to break down by, dropped")
                    continue
            else:                                                       # kpi
                grain, gb = None, []
            panel = dict(type=typ, metric_id=mid, group_by=gb, grain=grain, filters=flt,
                         title=_clean_text(pn.get("title")) or registry.get(mid).name, annotate=bool(pn.get("annotate", typ == "timeseries")))
            key = json.dumps(panel, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            panels.append(panel)
        if len(panels) > MAX_PANELS:
            notes.append(f"more than {MAX_PANELS} panels: the rest were dropped")
            panels = panels[:MAX_PANELS]
        win = raw.get("window") if raw.get("window") in WINDOWS else "today"
        spec = dict(title=_clean_text(raw.get("title"), 100) or "Operations view", role_id=p.role_id, window=win,
                    refresh_seconds=REFRESH_SECONDS, lenders=list(p.lenders), panels=panels)
        return spec, notes

    # ---------------------------------------------------------------- deterministic fallback
    def fallback(self, p: A.Principal, request: str | None = None) -> dict:
        """No AI: the role's default view, steered by plain keywords in the request."""
        panels = []
        text = (request or "").lower()
        wanted, dims = [], []
        for pat, cfg in KEYWORDS:
            if re.search(pat, text):
                wanted += cfg["metrics"]
        for d, pat in DIM_WORDS.items():
            if re.search(pat, text):
                dims.append(d)
        if wanted:
            vis = [m for m in dict.fromkeys(wanted) if m in p.metrics]
            for m in vis[:4]:
                panels.append(dict(type="kpi", metric_id=m))
            for m in vis[:2]:
                panels.append(dict(type="timeseries", metric_id=m, grain="hour"))
            for m in vis[:3]:
                ok = A.effective_dims(p, m)
                use = [d for d in dims if d in ok] or [d for d in ("gateway", "dialer", "agency", "city") if d in ok][:1]
                for d in use[:2]:
                    panels.append(dict(type="breakdown", metric_id=m, group_by=[d]))
        if not panels:
            for row in ROLE_DEFAULTS[p.role_id]:
                typ, mid = row[0], row[1]
                if mid in p.metrics:
                    panels.append(dict(type=typ, metric_id=mid, group_by=row[2] if len(row) > 2 else [], grain=row[3] if len(row) > 3 else None))
        win = next((w for pat, w in WINDOW_WORDS if re.search(pat, text)), "today")
        spec, _ = self.validate(p, dict(title=(request or p.role_name + " view")[:70], window=win, panels=panels))
        return spec

    def request_notes(self, p: A.Principal, request: str) -> list:
        """What in the request is outside the user's role or scope or the governed catalog (plain keyword checks, code)."""
        notes, text = [], request.lower()
        if ADHOC.search(request):
            notes.append("your request asks for a new or ad hoc metric; only governed metrics can be shown, so the closest governed "
                         "metrics are used instead")
        for pat, cfg in KEYWORDS:
            if re.search(pat, text) and not any(m in p.metrics for m in cfg["metrics"]):
                notes.append(f"{cfg['label']} metrics are not available to your role ({p.role_name})")
        for L in sorted(set(re.findall(r"\bL[1-5]\b", request, re.I))):
            if L.upper() not in p.lenders:
                notes.append(f"lender {L.upper()} is outside your scope; only {', '.join(p.lenders)} can be shown")
        for d, pat in DIM_WORDS.items():
            if re.search(pat, text) and d not in p.dims:
                notes.append(f"the dimension '{d}' is not available to your role")
        return notes

    # ---------------------------------------------------------------- the AI part
    def _prompt(self, p: A.Principal, request: str):
        cat = A.visible_catalog(p)
        lines = [f"{m['metric_id']}|{m['name']}|{m['unit']}|dims:{','.join(m['allowed_dims'])}" for m in cat]
        vals = {d: v for d, v in self.dim_values().items() if d in p.dims and d != "lender"}
        system = (
            "You design operations dashboards for a debt-collections platform. Turn the user's request into a dashboard spec. "
            "Use ONLY metric ids from the catalog and ONLY the listed dimensions and values. Never invent a metric or a formula: if the "
            "request asks for something not in the catalog, pick the closest catalog metrics and say so in notes. "
            f"Return JSON: {{\"title\": str, \"window\": \"today\"|\"last_24h\"|\"last_7d\", \"notes\": str, \"panels\": [{{\"type\": "
            f"\"kpi\"|\"timeseries\"|\"breakdown\"|\"table\", \"metric_id\": str, \"group_by\": [dimension], \"grain\": "
            f"\"bucket\"|\"hour\"|\"day\" (timeseries only), \"filters\": {{dimension: value}}, \"title\": str}}]}}. "
            f"At most {MAX_PANELS} panels; a breakdown/table needs one group_by; kpi has no group_by.")
        user = (f"Role: {p.role_name}. Lenders in scope: {', '.join(p.lenders)}.\nCatalog (id|name|unit|dims):\n" + "\n".join(lines)
                + f"\nDimension values: {json.dumps(vals)}\nRequest: {request}")
        return system, user

    def generate(self, p: A.Principal, request: str | None, use_llm: bool = True) -> dict:
        p = A._verify(p)
        t0 = time.time()
        request = (request or "").strip()
        notes, source, spec = (self.request_notes(p, request) if request else []), "fallback", None
        if use_llm and self.llm is not None and request:
            try:
                system, user = self._prompt(p, request)
                raw = self.llm(system, user, self.model) if self.model else self.llm(system, user)
                draft, vnotes = self.validate(p, raw)
                notes += vnotes
                if isinstance(raw, dict) and raw.get("notes"):
                    notes.append("model: " + _clean_text(raw["notes"], 160))
                if draft["panels"]:
                    spec, source = draft, "llm"
            except Exception as e:  # noqa: BLE001  (rate limit, no key, bad JSON ...)
                notes.append(f"the AI step was unavailable ({type(e).__name__}); the default view for your role was built instead")
        if spec is None:
            spec = self.fallback(p, request)
            source = "fallback"
        return dict(spec=spec, source=source, notes=notes, ms=int((time.time() - t0) * 1000))

    # ---------------------------------------------------------------- rebuild around a finding
    def rebuild(self, p: A.Principal, spec: dict, incident: dict) -> dict:
        """Put the finding at the top of the view: the metric in the driving segment (and its parent for contrast), the other
        metrics the incident touched, then the rest of the view. incident = dict(incident_id, metric, segment, parent, metrics)."""
        p = A._verify(p)
        seg, parent, mid = incident["segment"], incident.get("parent") or incident["segment"], incident["metric"]
        top = [dict(type="timeseries", metric_id=mid, group_by=[], grain="bucket", filters=seg, title=f"Driver: {DR.segment_text(seg)}", annotate=True)]
        if parent != seg:
            top.append(dict(type="timeseries", metric_id=mid, group_by=[], grain="bucket", filters=parent, title=f"Context: {DR.segment_text(parent)}", annotate=True))
        for m in [x for x in dict.fromkeys(incident.get("metrics", [])) if x != mid][:3]:
            top.append(dict(type="kpi", metric_id=m, filters=seg, title=f"{registry.get(m).name} in the driver"))
        top.append(dict(type="kpi", metric_id=mid, filters=seg, title=f"{registry.get(mid).name} in the driver"))
        draft = dict(title=f"{spec.get('title', 'View')}: incident {incident['incident_id']}", window=spec.get("window", "today"),
                     panels=top + spec.get("panels", []))
        out, notes = self.validate(p, draft)
        kept = []
        for pn in out["panels"]:
            t = pn["title"]
            if t.startswith(("Driver:", "Context:")):                  # the title must say what the panel REALLY filters on
                if not pn["filters"]:
                    notes.append("the driving segment is not visible to your role; the panel was left out")
                    continue
                pn["title"] = t.split(":")[0] + ": " + DR.segment_text(pn["filters"])
                if pn["filters"] != (seg if t.startswith("Driver") else parent):
                    notes.append("part of the driving segment is outside your role; the panel shows the part you may see")
            kept.append(pn)
        out["panels"] = kept
        out["focus"] = dict(incident_id=incident["incident_id"], segment=seg, metric=mid)
        return dict(spec=out, source="rebuilt", notes=list(dict.fromkeys(notes)))

    # ---------------------------------------------------------------- persistence
    def save(self, p: A.Principal, request, out, focus_anomaly_id=None, database="lc04"):
        tenant = p.lenders[0] if p.scope_type == "single_lender" else "GLOBAL"
        return db.insert(tenant, "dashboards", dict(user_id=p.user_id, role_id=p.role_id, request_text=request, spec=json.dumps(out["spec"]),
                                                    source=out["source"], focus_anomaly_id=focus_anomaly_id, generation_ms=out.get("ms")), database)


def default_llm():
    """The real model (Groq). Imported lazily so nothing breaks when there is no key or no network."""
    sys.path.insert(0, str(ROOT / "llm"))
    from groq_client import chat_json, MODEL_FAST  # noqa: E402
    return lambda system, user, model=None: chat_json(system, user, model or MODEL_FAST)
