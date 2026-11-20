"""
LC-04 runner: the replay clock, incidents, annotations and (mock) alerts.

    python lc_runner.py prepare                 detect + drill down over the whole stream (a few minutes, cached)
    python lc_runner.py status
    python lc_runner.py advance --minutes 15    move the simulated clock and write what the agent would have written by then
    python lc_runner.py replay --to "2026-10-15 14:00:00"
    python lc_runner.py reset  --to "2026-10-08 00:00:00"      go back: everything produced later disappears

How an incident is handled (all in code; the numbers are governed):
  1. DETECT   the seasonal detector raises alerts (one per metric x segment) as buckets arrive.
  2. GROUP    alerts that cover the same part of the book and fire close together are ONE incident (one root cause).
  3. DRILL    at detection the drill-down names the driving segment; if its evidence is strong enough (confirmed: significant,
              carries the deviation, counterfactual holds) an ALERT goes out right away.
  4. REFINE   one hour later the drill-down runs again on the bigger sample, the annotation is updated, and an alert goes out
              then if there was none yet, or an "Update" if the driving segment changed. Incidents that are never confirmed
              raise no alert (they are kept as "watched", visible in the UI): this is how false alerts are held down.
Point-in-time: the replay shows only what had been produced by the simulated clock; detection and drill-down only use data up to
the moment they ran (tested by removing the future). The timeline is computed once and cached; advance() writes the rows.
Nothing is ever sent to anyone: alerts are rows.
"""
import argparse
import hashlib
import json
import pickle
import sys
import warnings
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
sys.path.insert(0, str(HERE.parents[1] / "db"))
import alerts as AL  # noqa: E402
import compliance as CP  # noqa: E402
import detect as D  # noqa: E402
import drill as DR  # noqa: E402
import db  # noqa: E402
from frame import CubeFrame, SLOTS, STREAM_START  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
DB = "lc04"
G = "GLOBAL"
REFINE_BUCKETS = 4                # one hour after detection
QUIET_MINUTES = 90                # an incident is closed when nothing new fired for this long
OUTPUT_TABLES = ["alerts", "annotations", "anomalies", "agent_steps", "agent_runs"]


# ---------------------------------------------------------------------------------------------- meta / clock
def meta(key, default=None):
    r = db.query_one(G, "SELECT value FROM lc_meta WHERE tenant_id=:tenant_id AND key=:k", {"k": key}, DB)
    return r["value"] if r else default


def set_meta(key, value):
    db.execute(G, "DELETE FROM lc_meta WHERE tenant_id=:tenant_id AND key=:k", {"k": key}, DB)
    db.insert(G, "lc_meta", {"key": key, "value": str(value)}, DB)


def last_day():
    return 39 if meta("hidden_loaded", "0") == "1" else 29


def stream_end():
    end = STREAM_START + pd.Timedelta(days=last_day() + 1)
    demo = meta("demo_legal_day")                      # the legal-communications demo scenario lets the clock run into its day
    return max(end, pd.Timestamp(demo) + pd.Timedelta(days=1)) if demo else end


def get_clock():
    return pd.Timestamp(meta("clock", str(STREAM_START)))


# ---------------------------------------------------------------------------------------------- the timeline
def _signature(n_days):
    conn = db.get_conn(DB)
    end = (STREAM_START + pd.Timedelta(days=n_days)).strftime("%Y-%m-%d %H:%M:%S")      # only the stream days: a demo day after them changes nothing
    cube_sig = tuple(conn.execute("SELECT COUNT(*), MAX(bucket_ts), SUM(ev_total), SUM(pay_failed) FROM cube_15m WHERE bucket_ts < ?", (end,)).fetchone())
    conn.close()
    h = hashlib.sha1()
    for f in (HERE / "detect.py", HERE / "drill.py", HERE / "alerts.py", Path(__file__), D.PARAMS_PATH):
        h.update(f.read_bytes() if f.exists() else b"")
    h.update(repr((cube_sig, n_days)).encode())
    return h.hexdigest()


def _related(inv, a, m):
    sa, sm = a["segment"], m["segment"]
    nested = all(sm.get(k) == v for k, v in sa.items()) or all(sa.get(k) == v for k, v in sm.items())
    return nested or inv.overlap(sa, sm, a["day"]) >= .5


def build_timeline(n_days):
    series, cal, params = D.build_series(n_days=n_days), D.load_calendar(), D.load_params()
    alerts = D.run(series, cal, params, last_day=n_days - 1)
    frame = CubeFrame(max_tick=n_days * SLOTS - 1)
    inv = DR.Investigator(frame, cal)
    live = sorted([a for a in alerts if not a["catchup"]], key=lambda a: a["first_tick"])
    clusters = []
    for a in live:
        for c in clusters:
            if a["first_tick"] - c["last_tick"] <= 8 and any(_related(inv, a, m) for m in c["members"]):
                c["members"].append(a)
                c["last_tick"] = max(c["last_tick"], a["last_tick"])
                break
        else:
            clusters.append(dict(members=[a], last_tick=a["last_tick"]))
    used, incidents, dropped = {}, [], []
    for c in clusters:
        members = c["members"]
        g0 = members[0]["first_tick"]
        day = g0 // SLOTS
        inv.alert_log = [(a["day"], dict(a["segment"])) for a in live if a["day"] < day]

        def findings(upto):
            out = []
            for m in members:
                if m["first_tick"] <= upto:
                    try:
                        out.append(inv.investigate(m, extend=upto - m["first_tick"]))
                    except ValueError:
                        pass
            return out

        f0 = findings(g0)
        g1 = g0 + REFINE_BUCKETS
        f1 = findings(g1) if g1 <= n_days * SLOTS - 1 else []
        if not f0 and not f1:
            dropped.append(dict(first_tick=g0, members=len(members)))     # never silent: the count is in the timeline
            continue
        p0 = inv.primary(f0) if f0 else None
        p1 = inv.primary(f1) if f1 else None
        t0 = D.bucket_end(g0)
        iid = f"INC-{t0:%m%d-%H%M}"
        used[iid] = used.get(iid, 0) + 1
        if used[iid] > 1:
            iid += f"-{used[iid]}"
        stages = []                                                    # what is sent, and when
        if p0 is not None and p0.confirmed:
            stages.append(dict(stage="initial", ts=str(t0), tick=g0, finding=p0))
        # a later alert only if it adds something: a first alert, or an UPDATE that names a different / MORE specific driver
        # (a refinement that falls back to a broader segment than the first alert is recorded in the annotation, not re-sent)
        broader = bool(stages) and p1 is not None and set(p1.segment.items()) < set(stages[0]["finding"].segment.items())
        if p1 is not None and p1.confirmed and (not stages or (p1.segment != stages[0]["finding"].segment and not broader)):
            stages.append(dict(stage="refined" if not stages else "update", ts=str(D.bucket_end(g1)), tick=g1, finding=p1))
        for s in stages:
            f = s["finding"]
            lenders = AL.affected_lenders(inv, f.segment, f.window[1] // SLOTS)
            rows = []
            for lender, role in AL.audience(f.metric, lenders):
                if role in ("lender_rm", "lender_user"):
                    nb = inv.measure(f.metric, {**f.segment, "lender": lender}, f.window[0], f.window[1]) if "lender" not in f.segment \
                        else None
                    l1, l2 = AL.two_lines(f, nb, who="lender")
                else:
                    l1, l2 = AL.two_lines(f, None, lenders, who="platform")
                if s["stage"] == "update":
                    l1 = "Update: " + l1
                rows.append(dict(tenant=lender, role=role, line1=l1, line2=l2))
            s["rows"], s["lenders"] = rows, lenders
        incidents.append(dict(incident_id=iid, members=members, first_tick=g0, t_detect=str(t0), t_refine=str(D.bucket_end(g1)),
                              last_tick=c["last_tick"], f0=f0, f1=f1, p0=p0, p1=p1, stages=stages))
    return dict(incidents=incidents, n_days=n_days, alerts_total=len(live), dropped=dropped)


_TL = {}                                   # in-memory copy of the timeline (the server keeps it between requests)


def is_prepared():
    """True when a timeline for the CURRENT data and code exists (cheap: compares a signature, loads nothing)."""
    sig_file = D.CACHE_DIR / "timeline.sig"
    return sig_file.exists() and (D.CACHE_DIR / "timeline.pkl").exists() and sig_file.read_text() == _signature(last_day() + 1)


def prepare(force=False, verbose=True):
    n_days = last_day() + 1
    sig = _signature(n_days)
    if not force and _TL.get("sig") == sig:
        return _TL["timeline"]
    cache = D.CACHE_DIR / "timeline.pkl"
    if not force and cache.exists():
        saved = pickle.loads(cache.read_bytes())
        if saved["sig"] == sig:
            _TL.update(sig=sig, timeline=saved["timeline"])
            return saved["timeline"]
    if verbose:
        print("preparing the timeline (detect + drill down over the whole stream)...", flush=True)
    tl = build_timeline(n_days)
    D.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps({"sig": sig, "timeline": tl}))
    (D.CACHE_DIR / "timeline.sig").write_text(sig)
    _TL.update(sig=sig, timeline=tl)
    return tl


# ---------------------------------------------------------------------------------------------- writing the outputs
def clear_outputs():
    with db.get_conn(DB) as conn:
        for t in OUTPUT_TABLES:
            conn.execute(f"DELETE FROM {t}")


def _key(a):
    return (a["metric"], json.dumps(a["segment"], sort_keys=True), a["first_tick"])


def materialize(clock):
    """Rewrite anomalies, annotations, alerts and agent traces for everything the agent would have produced by `clock`."""
    clock = pd.Timestamp(clock)
    tl = prepare(verbose=False)
    clear_outputs()
    counts = dict(incidents=0, anomalies=0, annotations=0, alerts=0)
    for inc in tl["incidents"]:
        if pd.Timestamp(inc["t_detect"]) > clock:
            continue
        counts["incidents"] += 1
        ids = {}
        last = pd.Timestamp(inc["t_detect"])
        for m in inc["members"]:
            det = pd.Timestamp(m["detected_at"])
            if det > clock:
                continue
            last = max(last, det)
            ids[_key(m)] = db.insert(m["tenant"], "anomalies", dict(
                incident_id=inc["incident_id"], metric_id=m["metric"], onset_ts=m["onset_ts"], detected_at=m["detected_at"],
                observed=m["observed"], expected=m["expected"], score=m["score"], buckets_persisted=m["window"], status="open",
                threshold_version=1), DB)
            counts["anomalies"] += 1
        status = "closed" if clock - last >= pd.Timedelta(minutes=QUIET_MINUTES) else "open"
        with db.get_conn(DB) as conn:
            conn.execute("UPDATE anomalies SET status=? WHERE incident_id=?", (status, inc["incident_id"]))
        run_id = f"{inc['incident_id']}-run"
        db.insert(G, "agent_runs", dict(run_id=run_id, agent_name="lc04_incident", entity_id=inc["incident_id"], status="done",
                                        started_at=inc["t_detect"], finished_at=inc["t_refine"]), DB)
        step = [0]

        def add(tool, inp, out):
            step[0] += 1
            db.insert(G, "agent_steps", dict(run_id=run_id, step_no=step[0], tool=tool, input=json.dumps(inp, default=str),
                                             output=json.dumps(out, default=str)), DB)

        first = inc["members"][0]
        add("detect", dict(metric=first["metric"], segment=first["segment"], window_buckets=first["window"]),
            dict(score=first["score"], direction=first["direction"], detected_at=first["detected_at"], alerts_in_incident=len(inc["members"])))
        for label, p, ts in (("initial", inc["p0"], inc["t_detect"]), ("refined", inc["p1"], inc["t_refine"])):
            if p is None or pd.Timestamp(ts) > clock:
                continue
            fj = p.to_json()
            add("drill_down", dict(stage=label, parent=p.parent), dict(driver=p.segment, explained=fj["explained"], z=fj["z"], top_candidates=fj["candidates"]))
            add("counterfactual", dict(segment=p.segment), p.counterfactual)
            anom_id = ids.get(_key(p.alert)) or next(iter(ids.values()), None)
            if anom_id is None:
                continue
            tenant = p.alert["tenant"]
            db.insert(tenant, "annotations", dict(
                anomaly_id=anom_id, created_at=ts, segment=json.dumps(p.segment), share_of_deviation=min(p.explained, 1.0),
                counterfactual_ok=int(p.counterfactual_ok), evidence=json.dumps({**fj, "stage": label, "confirmed": p.confirmed}, default=str),
                headline=p.headline), DB)
            counts["annotations"] += 1
            add("annotate", dict(stage=label), dict(headline=p.headline, confirmed=p.confirmed))
        for s in inc["stages"]:
            if pd.Timestamp(s["ts"]) > clock:
                continue
            f = s["finding"]
            anom_id = ids.get(_key(f.alert)) or next(iter(ids.values()), None)
            for r in s["rows"]:
                db.insert(r["tenant"], "alerts", dict(
                    incident_id=inc["incident_id"], anomaly_id=anom_id, annotation_id=None, created_at=s["ts"], audience_role=r["role"],
                    line1=r["line1"], line2=r["line2"], link=AL.link_for(inc["incident_id"]), status="sent_mock"), DB)
                counts["alerts"] += 1
            add("alert", dict(stage=s["stage"], lenders=s["lenders"]), dict(rows=len(s["rows"]), sent_at=s["ts"], line1=s["rows"][0]["line1"] if s["rows"] else ""))
    _write_compliance(clock, counts)
    set_meta("clock", clock)
    return counts


def _write_compliance(clock, counts):
    """The legal-window rule (compliance.py): incidents that have arrived by the clock, with their annotation, trace and alerts."""
    for ci in CP.incidents(clock):
        iid, t_detect = ci["incident_id"], ci["t_detect"]
        counts["incidents"] += 1
        status = "open" if clock - pd.Timestamp(ci["window_end"]) < pd.Timedelta(minutes=QUIET_MINUTES) else "closed"
        anom = db.insert(G, "anomalies", dict(incident_id=iid, metric_id=CP.METRIC, onset_ts=ci["window_start"], detected_at=t_detect,
                                              observed=float(ci["count"]), expected=0.0, score=float(ci["count"]), buckets_persisted=ci["buckets"],
                                              status=status), DB)
        counts["anomalies"] += 1
        driver_share = max([ci["shares"][k][v] for k, v in ci["segment"].items()] or [0.0])
        evidence = dict(metric=CP.METRIC, segment=ci["segment"], parent=ci["segment"], direction="up", explained=driver_share, z=None,
                        counterfactual_ok=True, counterfactual=dict(kind="rule", note="the rule allows zero legal notices outside the window"),
                        confirmed=True, stage="initial", rule=ci["rule"], shares=ci["shares"], per_lender=ci["per_lender"],
                        observed=dict(n=ci["count"], value=ci["count"]), expected=dict(n=0, value=0),
                        candidates=[dict(segment={d: v}, explained=s, z=None) for d, sh in ci["shares"].items() for v, s in list(sh.items())[:2]][:8],
                        window=[ci["window_start"], ci["window_end"]])
        db.insert(G, "annotations", dict(anomaly_id=anom, created_at=t_detect, segment=json.dumps(ci["segment"]), share_of_deviation=driver_share,
                                         counterfactual_ok=1, evidence=json.dumps(evidence, default=str), headline=ci["headline"]), DB)
        counts["annotations"] += 1
        run_id = f"{iid}-run"
        db.insert(G, "agent_runs", dict(run_id=run_id, agent_name="lc04_compliance", entity_id=iid, status="done", started_at=t_detect, finished_at=t_detect), DB)
        steps = [("detect", dict(rule=ci["rule"]["name"], metric=CP.METRIC),
                  dict(rule=True, direction="up", count=ci["count"], detected_at=t_detect, alerts_in_incident=1, window=CP.WINDOW)),
                 ("drill_down", dict(dimensions=CP.DIMS), dict(driver=ci["segment"], explained=driver_share, z=None, top_candidates=evidence["candidates"])),
                 ("counterfactual", dict(segment=ci["segment"]), evidence["counterfactual"]),
                 ("annotate", dict(stage="initial"), dict(headline=ci["headline"], confirmed=True)),
                 ("alert", dict(stage="initial", lenders=ci["lenders"]), dict(rows=len(ci["rows"]), sent_at=t_detect, line1=ci["line1"]))]
        for n, (tool, inp, out) in enumerate(steps, 1):
            db.insert(G, "agent_steps", dict(run_id=run_id, step_no=n, tool=tool, input=json.dumps(inp, default=str), output=json.dumps(out, default=str)), DB)
        for r in ci["rows"]:
            db.insert(r["tenant"], "alerts", dict(incident_id=iid, anomaly_id=anom, annotation_id=None, created_at=t_detect, audience_role=r["role"],
                                                  line1=r["line1"], line2=r["line2"], link=AL.link_for(iid), status="sent_mock"), DB)
            counts["alerts"] += 1


# ---------------------------------------------------------------------------------------------- clock control
def advance(minutes=15):
    old = get_clock()
    new = min(old + pd.Timedelta(minutes=int(minutes)), stream_end())
    counts = materialize(new)
    fresh = db.get_conn(DB).execute(
        "SELECT incident_id, created_at, line1, line2 FROM alerts WHERE created_at > ? AND created_at <= ? GROUP BY incident_id, created_at",
        (str(old), str(new))).fetchall()
    return dict(clock=str(new), previous=str(old), new_alerts=[dict(r) for r in fresh], **counts)


def reset(to):
    return materialize(pd.Timestamp(to))


def status():
    c = get_clock()
    conn = db.get_conn(DB)
    n = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("anomalies", "annotations", "alerts")}
    return dict(clock=str(c), end=str(stream_end()), hidden_loaded=meta("hidden_loaded") == "1", **n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "status", "advance", "replay", "reset"])
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--to")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "prepare":
        tl = prepare(force=a.force)
        print(f"{len(tl['incidents'])} incidents, {sum(len(i['stages']) for i in tl['incidents'])} alert stages, {tl['alerts_total']} detector alerts")
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "advance":
        print(advance(a.minutes))
    elif a.cmd in ("replay", "reset"):
        print(materialize(pd.Timestamp(a.to)))


if __name__ == "__main__":
    main()
