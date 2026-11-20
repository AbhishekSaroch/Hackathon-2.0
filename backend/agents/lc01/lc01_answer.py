"""
LC-01 answer assembly: run the governed query, build the (templated) answer text, the chart spec and the root-cause explanation.

Everything a manager reads here is produced by CODE from computed numbers. The LLM only decided WHAT to ask (an intent); it does
not write a single sentence of the answer, so it cannot flip a fact.

Root cause (when asked): compare the metric in two months, then for every dimension the metric can be split by, ask "if this
segment had performed in month A as it did in month A, how much of the change would be gone?" (a counterfactual, same idea as the
LC-04 drill-down). The dimension whose top segment explains most is the driver; a second level looks inside that segment; related
driver metrics (connect rate, promises kept, NACH bounces ...) are computed for the segment as evidence.
"""
import lc01_access as A
import lc01_compiler as C
import lc01_registry as R

MIN_BASE = 20             # a segment needs at least this many records in a month to be named as a driver
DOMINANT = 0.35           # a top segment must explain at least this share of the change to be called "the driver"
_MONEY_COLS = ("paid_emi", "recovered", "emi_due", "arrears_start", "outstanding_end", "amount", "paid_amount", "promised_amount")
_QUIRK_FAMILIES = {"L2": {"delinquency", "collections"}, "L3": None}   # L3's quirk is metric-specific, see _quirk_applies
_QUIRK_METRICS = {"L3": {"npa_rate", "par90"}}


def lender_label(lenders) -> str:
    n = [R.LENDER_NAMES[l] for l in lenders]
    return n[0] if len(n) == 1 else (", ".join(n[:-1]) + " and " + n[-1] + " combined")


def period_label(a, b) -> str:
    return a if a == b else f"{a} to {b}"


def _is_money(expr: str) -> bool:
    return any(c in expr for c in _MONEY_COLS)


def basis_text(m: R.Metric, row) -> str | None:
    """'1,234 of 5,678' for rate metrics (money for amount-based ones)."""
    if not m.den or row.get("den") is None or m.unit != "pct":
        return None
    nu, de = ("rs" if _is_money(m.num) else "count"), ("rs" if _is_money(m.den) else "count")
    return f"{R.fmt_value(row['num'], nu)} of {R.fmt_value(row['den'], de)}"


def label_of(row, dims) -> str:
    return " · ".join(str(R.LENDER_NAMES.get(row[d], row[d]) if d == "lender" else row[d]) for d in dims)


def _filters_label(q: C.Query) -> str:
    return "" if not q.filters else " (" + ", ".join(f"{d} = {' / '.join(v)}" for d, v in q.filters) + ")"


def lender_notes(p, q: C.Query, m: R.Metric) -> list:
    notes = []
    for l in q.lenders:
        applies = (m.family in (_QUIRK_FAMILIES.get(l) or set())) or (m.metric_id in _QUIRK_METRICS.get(l, set()))
        if applies:
            r = A.warehouse_query(p, "SELECT display_name, quirk_note FROM lender_config WHERE tenant_id = :tenant_id", None, (l,))
            if r:
                notes.append(f"{r[0]['display_name']}: {r[0]['quirk_note']}")
    return notes


class Runner:
    """Runs queries for one question and remembers every statement (the user is shown all of them)."""

    def __init__(self, p):
        self.p, self.sqls, self.steps = p, [], []

    def run(self, q: C.Query, label: str) -> C.Result:
        import time
        t0 = time.time()
        res = C.run(self.p, q)
        self.sqls.append(dict(label=label, sql=res.sql, params=res.params, lenders=list(q.lenders), rows=len(res.rows)))
        self.steps.append(dict(tool="warehouse_sql", detail=label, ms=int((time.time() - t0) * 1000), rows=len(res.rows)))
        return res


# ------------------------------------------------------------------------------------------------ chart generator
def make_chart(m: R.Metric, q: C.Query, rows: list, highlight=None) -> dict | None:
    if not q.group_by and not q.trend:
        return None
    dims = list(q.group_by)
    title = f"{m.name}" + (f" by {' and '.join(dims)}" if dims else "") + (" per month" if q.trend else "")
    if q.trend:
        series = {}
        for r in rows:
            series.setdefault(label_of(r, dims) if dims else m.name, []).append({"x": r["month"], "y": r["value"]})
        series = dict(sorted(series.items(), key=lambda kv: -(kv[1][-1]["y"] or 0))[:6])
        return dict(kind="line", title=title, unit=m.unit, x_label="month", series=[dict(name=k, points=v) for k, v in series.items()], highlight=highlight or [])
    pts = [{"x": label_of(r, dims), "y": r["value"]} for r in rows[:15] if r["value"] is not None]
    return dict(kind="bar", title=title, unit=m.unit, x_label=" · ".join(dims), series=[dict(name=m.name, points=pts)], highlight=[])


# ------------------------------------------------------------------------------------------------ root cause
def _val(num, den, m):
    return (num / den if den else None) if m.den else num


def _agg(res: C.Result):
    """Sum of numerators / denominators over all rows of an ungrouped or grouped result."""
    n = sum(r["num"] or 0 for r in res.rows)
    d = sum((r["den"] or 0) for r in res.rows) if res.query.metric_id and R.get(res.query.metric_id).den else 0
    return n, d


def _delta_word(m: R.Metric, a, b) -> str:
    if a is None or b is None or a == b:
        return "stayed flat"
    rose = b > a
    better = (rose and m.good == "up") or (not rose and m.good == "down")
    tag = "" if m.good == "neutral" else (" (better)" if better else " (worse)")
    return ("rose" if rose else "fell") + tag


def _is_flat(m, a, b, loose=False) -> bool:
    if a is None or b is None:
        return True
    if m.unit == "pct":
        return abs(b - a) < (0.02 if loose else 0.002)
    return abs(b - a) <= (0.05 if loose else 0.01) * max(abs(a), 1e-9)


def _scan(rn: Runner, m, base_filters, lenders, month_a, month_b, dims, vA, vB, nA, dA, nB, dB):
    """Counterfactual scan over `dims`; returns the best dimension with its ranked segments."""
    change = vB - vA
    best = None
    for dim in dims:
        try:
            qa = C.make_query(rn.p, m.metric_id, [dim], base_filters, month_a, month_a, lenders=lenders)
            qb = C.make_query(rn.p, m.metric_id, [dim], base_filters, month_b, month_b, lenders=lenders)
        except (C.UnknownValue, R.MetricError, A.AccessError):
            continue
        ra, rb = rn.run(qa, f"root cause: {m.name} by {dim}, {month_a}"), rn.run(qb, f"root cause: {m.name} by {dim}, {month_b}")
        sa = {r[dim]: r for r in ra.rows}
        sb = {r[dim]: r for r in rb.rows}
        segs = []
        for key in set(sa) | set(sb):
            a, b = sa.get(key, {}), sb.get(key, {})
            na, da, nb, db_ = (a.get("num") or 0), (a.get("den") or 0), (b.get("num") or 0), (b.get("den") or 0)
            if (da if m.den else na) < MIN_BASE * (1 if m.den else 0) and (db_ if m.den else nb) < MIN_BASE * (1 if m.den else 0):
                continue
            cf = _val(nB - nb + na, dB - db_ + da, m)
            if cf is None:
                continue
            expl = vB - cf
            rest_a, rest_b = _val(nA - na, dA - da, m), _val(nB - nb, dB - db_, m)
            segs.append(dict(dim=dim, label=key, effect=expl, value_a=_val(na, da, m), value_b=_val(nb, db_, m),
                             rest_a=rest_a, rest_b=rest_b, den_b=db_ if m.den else nb, size=((db_ / dB) if (m.den and dB) else (nb / nB if nB else 1))))
        sign = 1 if change > 0 else -1
        segs = [s for s in segs if s["effect"] * sign > 0]
        gross = sum(abs(s["effect"]) for s in segs)
        for s in segs:
            s["share"] = abs(s["effect"]) / gross if gross else 0
            s["gross"] = gross
        segs.sort(key=lambda s: -abs(s["effect"]))
        if segs and (best is None or abs(segs[0]["effect"]) > abs(best["segments"][0]["effect"]) + 1e-12):
            best = dict(dim=dim, segments=segs)
    return best


def _trivial_dims(m: R.Metric) -> set:
    """Dimensions that would 'explain' everything for a trivial reason, so they are never scanned as a root cause:
    the opening bucket (it is the starting state the roll / cure rates are defined on), a call's own outcome / disposition,
    and any dimension the metric's own formula already filters on (channel for the NACH bounce rate)."""
    out = set()
    if m.fact == "fact_perf":
        out.add("bucket")
    if m.fact == "fact_calls":
        out |= {"outcome", "disposition"}
    formula = m.num + " " + (m.den or "")
    out |= {d for d in m.dims if R.DIM_COL[d] in formula}
    return out


def explain(rn: Runner, m: R.Metric, q: C.Query, month_a: str, month_b: str) -> dict:
    p = rn.p
    base_filters = {d: list(v) for d, v in q.filters}
    lenders = q.lenders
    qa = C.make_query(p, m.metric_id, [], base_filters, month_a, month_a, lenders=lenders)
    qb = C.make_query(p, m.metric_id, [], base_filters, month_b, month_b, lenders=lenders)
    ra, rb = rn.run(qa, f"root cause: overall {m.name}, {month_a}"), rn.run(qb, f"root cause: overall {m.name}, {month_b}")
    nA, dA = _agg(ra)
    nB, dB = _agg(rb)
    vA, vB = _val(nA, dA, m), _val(nB, dB, m)
    change = (vB - vA) if (vA is not None and vB is not None) else 0
    out = dict(month_a=month_a, month_b=month_b, value_a=vA, value_b=vB, summary=[], segments=[], evidence=[], dimension=None, driver=None, flat=False)
    if vA is None or vB is None:
        out["summary"].append(f"There is no {m.name.lower()} to compare for {month_a} and {month_b} in this selection.")
        out["flat"] = True
        return out
    head = f"{m.name} {_delta_word(m, vA, vB)} from {R.fmt_value(vA, m.unit)} in {month_a} to {R.fmt_value(vB, m.unit)} in {month_b} ({R.fmt_delta(vA, vB, m.unit)})"
    if _is_flat(m, vA, vB):
        out["flat"] = True
        out["summary"].append(head + ". That is within normal month-to-month noise, so there is no problem to explain.")
        return out
    out["summary"].append(head + ".")
    used = {d for d, _ in q.filters} | set(q.group_by)
    dims = [d for d in m.dims if d not in used and d not in _trivial_dims(m)] + (["lender"] if p.multi and len(lenders) > 1 and "lender" not in used else [])
    best = _scan(rn, m, base_filters, lenders, month_a, month_b, dims, vA, vB, nA, dA, nB, dB)
    if not best:
        out["summary"].append("I split the change by every dimension available and no segment moved the number in the same direction, so there is no single driver.")
        return out
    top = best["segments"][0]
    out["dimension"], out["segments"] = best["dim"], best["segments"][:4]
    if top["share"] < DOMINANT:
        out["summary"].append(f"No single segment dominates. The largest, {best['dim']} {_name(top)}, accounts for {top['share']:.0%} of the movement; the rest is spread out.")
        return out
    out["driver"] = {best["dim"]: top["label"]}
    seg_name = _name(top)
    out["summary"].append(
        f"The main driver is {best['dim']} {seg_name}: on its own it moved the total by {R.fmt_delta(0, top['effect'], m.unit)} "
        f"(about {top['share']:.0%} of all the movement in that direction). Its {m.name.lower()} went from "
        f"{R.fmt_value(top['value_a'], m.unit)} to {R.fmt_value(top['value_b'], m.unit)}, while the rest of the book went from "
        f"{R.fmt_value(top['rest_a'], m.unit)} to {R.fmt_value(top['rest_b'], m.unit)}.")
    if top["gross"] > 1.3 * abs(change):
        out["summary"].append("Other segments moved the other way, so the net change is smaller than this driver's effect alone.")
    # second level: look inside the driving segment
    seg_filters = {**base_filters, best["dim"]: [top["label"]]} if best["dim"] != "lender" else base_filters
    seg_lenders = (top["label"],) if best["dim"] == "lender" else lenders
    q1a = C.make_query(p, m.metric_id, [], seg_filters, month_a, month_a, lenders=seg_lenders)
    q1b = C.make_query(p, m.metric_id, [], seg_filters, month_b, month_b, lenders=seg_lenders)
    sa1, sb1 = rn.run(q1a, f"drill: {seg_name}, {month_a}"), rn.run(q1b, f"drill: {seg_name}, {month_b}")
    n1a, d1a = _agg(sa1)
    n1b, d1b = _agg(sb1)
    v1a, v1b = _val(n1a, d1a, m), _val(n1b, d1b, m)
    if v1a is not None and v1b is not None and not _is_flat(m, v1a, v1b):
        dims2 = [d for d in dims if d != best["dim"] and d in m.dims]
        inner = _scan(rn, m, seg_filters, seg_lenders, month_a, month_b, dims2, v1a, v1b, n1a, d1a, n1b, d1b)
        if inner and inner["segments"][0]["share"] >= 0.6 and inner["segments"][0]["share"] >= 1.5 * inner["segments"][0].get("size", 1):
            s2 = inner["segments"][0]
            out["drill"] = {inner["dim"]: s2["label"]}
            out["summary"].append(f"Inside {seg_name}, {inner['dim']} {_name(s2)} accounts for about {s2['share']:.0%} of that segment's movement.")
    # evidence: related driver metrics for the driving segment vs the rest of the book
    for did in m.drivers:
        dm = R.get(did)
        if best["dim"] != "lender" and best["dim"] not in dm.dims:
            continue
        try:
            e_args = dict(lenders=seg_lenders)
            sa = C.make_query(p, did, [], seg_filters, month_a, month_a, **e_args)
            sb = C.make_query(p, did, [], seg_filters, month_b, month_b, **e_args)
            wa = C.make_query(p, did, [], base_filters, month_a, month_a, lenders=lenders)
            wb = C.make_query(p, did, [], base_filters, month_b, month_b, lenders=lenders)
        except (C.UnknownValue, R.MetricError, A.AccessError):
            continue
        xa, xb = rn.run(sa, f"evidence: {dm.name} for {seg_name}, {month_a}"), rn.run(sb, f"evidence: {dm.name} for {seg_name}, {month_b}")
        ya, yb = rn.run(wa, f"evidence: {dm.name} overall, {month_a}"), rn.run(wb, f"evidence: {dm.name} overall, {month_b}")
        (na_, da_), (nb_, db2_), (wna, wda), (wnb, wdb) = _agg(xa), _agg(xb), _agg(ya), _agg(yb)
        sa_v, sb_v = _val(na_, da_, dm), _val(nb_, db2_, dm)
        ra_v, rb_v = _val(wna - na_, wda - da_, dm), _val(wnb - nb_, wdb - db2_, dm)
        if sa_v is None or sb_v is None:
            continue
        if dm.den and (da_ < MIN_BASE or db2_ < MIN_BASE):
            continue
        worse = (sb_v < sa_v and dm.good == "up") or (sb_v > sa_v and dm.good == "down") or dm.good == "neutral"
        moved = (not _is_flat(dm, sa_v, sb_v, loose=True)) and worse
        gap = None if rb_v is None else sb_v - rb_v
        gap_bad = gap is not None and ((gap < 0 and dm.good == "up") or (gap > 0 and dm.good == "down"))
        gap_big = gap_bad and (abs(gap) >= 0.08 if dm.unit == "pct" else abs(gap) >= 0.2 * max(abs(rb_v), 1e-9))
        if not (moved or gap_big):
            continue
        rest = "" if ra_v is None or rb_v is None else f"; rest of the book {R.fmt_value(ra_v, dm.unit)} to {R.fmt_value(rb_v, dm.unit)}"
        tail = "" if moved else f" It had already moved away from the rest of the book (gap {R.fmt_delta(rb_v, sb_v, dm.unit)})."
        out["evidence"].append(dict(metric_id=did, name=dm.name, a=sa_v, b=sb_v, rest_a=ra_v, rest_b=rb_v, moved=moved,
                                    text=f"{dm.name} for {seg_name}: {R.fmt_value(sa_v, dm.unit)} to {R.fmt_value(sb_v, dm.unit)} ({R.fmt_delta(sa_v, sb_v, dm.unit)}){rest}.{tail}"))
    out["evidence"] = out["evidence"][:3]
    return out


def _name(seg) -> str:
    return str(R.LENDER_NAMES.get(seg["label"], seg["label"]))


# ------------------------------------------------------------------------------------------------ the answer
def answer(p: A.Principal, intent: dict) -> dict:
    """intent: validated dict from the intent stage -> full response (answered / no_data). Raises AccessError / MetricError / SanityError."""
    m = R.get(intent["metric"])
    q = C.make_query(p, m.metric_id, intent.get("group_by"), intent.get("filters"), intent.get("m_from"), intent.get("m_to"),
                     trend=intent.get("trend", False), lenders=intent.get("lenders"), order=intent.get("order", "desc"), top_n=intent.get("top_n"))
    rn = Runner(p)
    res = rn.run(q, f"{m.name}" + (f" by {', '.join(q.group_by)}" if q.group_by else "") + (" per month" if q.trend else ""))
    notes = list(res.warnings)
    if intent.get("period_assumed"):
        notes.insert(0, f"You did not give a period, so I used the latest month with data ({q.m_to}).")
    notes += intent.get("assumptions", [])
    notes += lender_notes(p, q, m)
    scope = lender_label(q.lenders)
    when = period_label(q.m_from, q.m_to)
    r = dict(status="answered", metric=dict(id=m.metric_id, name=m.name, unit=m.unit, definition=m.definition, family=m.family, overlap=m.overlap),
             period=dict(m_from=q.m_from, m_to=q.m_to, assumed=bool(intent.get("period_assumed"))),
             scope=dict(lenders=list(q.lenders), label=scope, filters={d: list(v) for d, v in q.filters}, group_by=list(q.group_by), trend=q.trend))
    if not res.rows or all(x["value"] is None for x in res.rows):
        r.update(status="no_data", headline=f"There is no {m.name.lower()} data for {scope}{_filters_label(q)} in {when}.", table=None, chart=None, explanation=None)
    else:
        r["headline"], r["table"] = _narrate(m, q, res.rows, scope, when)
        r["data"] = res.rows
        r["chart"] = make_chart(m, q, res.rows)
        r["explanation"] = None
        rn.steps.append(dict(tool="chart_generator", detail=(r["chart"] or {}).get("kind", "none (single figure)"), ms=0))
    if intent.get("explain") and r["status"] == "answered":
        b = q.m_to
        a = q.m_from if q.m_from < q.m_to else R.prev_month(b)
        if a is None:
            notes.append("The data starts in the first month, so there is no earlier month to compare it with.")
        else:
            ex = explain(rn, m, q, a, b)
            r["explanation"] = ex
            if not q.trend and not q.group_by:                       # give the explanation a trend line for context
                tq = C.make_query(p, m.metric_id, [], {d: list(v) for d, v in q.filters}, R.MONTHS[0], R.LATEST_MONTH, trend=True, lenders=q.lenders)
                tres = rn.run(tq, f"{m.name} per month (context)")
                r["chart"] = make_chart(m, tq, tres.rows, highlight=[a, b])
                r["table"] = dict(columns=["month", m.name], rows=[[x["month"], R.fmt_value(x["value"], m.unit)] for x in tres.rows])
            if ex.get("flat") is False and ex.get("driver") is None and not ex["segments"]:
                notes.append("No segment explained the change on its own.")
    r["notes"], r["sql"], r["steps"] = notes, rn.sqls, rn.steps
    return r


def _narrate(m: R.Metric, q: C.Query, rows: list, scope: str, when: str):
    dims = list(q.group_by)
    fl = _filters_label(q)
    fv = lambda v: R.fmt_value(v, m.unit)
    if not dims and not q.trend:
        row = rows[0]
        b = basis_text(m, row)
        head = f"{m.name} for {scope}{fl} in {when} is {fv(row['value'])}" + (f" ({b})." if b else ".")
        return head, dict(columns=["measure", "value"] + (["basis"] if b else []), rows=[[m.name, fv(row["value"])] + ([b] if b else [])])
    if q.trend:
        pts = [r for r in rows if r["value"] is not None]
        cols = (["segment"] if dims else []) + ["month", m.name]
        table = dict(columns=cols, rows=[([label_of(r, dims)] if dims else []) + [r["month"], fv(r["value"])] for r in rows])
        if not dims:
            f, l = pts[0], pts[-1]
            hi, lo = max(pts, key=lambda r: r["value"]), min(pts, key=lambda r: r["value"])
            head = (f"{m.name} for {scope}{fl} moved from {fv(f['value'])} in {f['month']} to {fv(l['value'])} in {l['month']} ({R.fmt_delta(f['value'], l['value'], m.unit)}); "
                    f"high {fv(hi['value'])} in {hi['month']}, low {fv(lo['value'])} in {lo['month']}.")
        else:
            by = {}
            for r in pts:
                by.setdefault(label_of(r, dims), []).append(r)
            moves = sorted(((k, v[-1]["value"] - v[0]["value"]) for k, v in by.items() if len(v) > 1), key=lambda kv: -abs(kv[1]))
            head = f"Monthly {m.name.lower()} by {' and '.join(dims)} for {scope}{fl}, {when}."
            if moves:
                k, d = moves[0]
                head += f" Biggest mover: {k} ({R.fmt_delta(0, d, m.unit)} from {by[k][0]['month']} to {by[k][-1]['month']})."
        return head, table
    pts = [r for r in rows if r["value"] is not None]
    table = dict(columns=[" · ".join(dims), m.name] + (["basis"] if m.den and m.unit == "pct" else []),
                 rows=[[label_of(r, dims), fv(r["value"])] + ([basis_text(m, r) or ""] if m.den and m.unit == "pct" else []) for r in rows])
    if q.top_n:
        lst = "; ".join(f"{label_of(r, dims)} {fv(r['value'])}" for r in pts[: q.top_n])
        head = f"{'Highest' if q.order == 'desc' else 'Lowest'} {len(pts[: q.top_n])} by {' and '.join(dims)} for {m.name.lower()} in {scope}{fl}, {when}: {lst}."
    else:
        hi, lo = max(pts, key=lambda r: r["value"]), min(pts, key=lambda r: r["value"])
        head = (f"{m.name} by {' and '.join(dims)} for {scope}{fl} in {when}: highest {label_of(hi, dims)} at {fv(hi['value'])}, "
                f"lowest {label_of(lo, dims)} at {fv(lo['value'])} ({len(pts)} segments).")
    return head, table
