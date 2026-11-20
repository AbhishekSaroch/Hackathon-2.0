"""
LC-04 scorecard: the brief's acceptance criteria, measured, written to data/lc04_eval_report.json (shown on the Scorecard page).

  python lc_evaluate.py public      the public days only (the practice anomalies we tuned on). Safe to run any time.
  python lc_evaluate.py final       THE HIDDEN RUN, ONCE: loads the hidden days + their answer key + the hidden request set, runs the
                                    frozen detector / drill-down / alerts over days 31-40, scores them, writes the report and logs
                                    everything in data/lc04_hidden_eval_ledger.json. Refuses to run a second time.

Discipline (same as BI-03): everything was tuned on the public days only. Before the hidden days are touched the ledger records a
fingerprint of the detector, drill-down, alert code and tuned parameters; the final run records it again and the report says
honestly if they differ. The numbers are reported whatever they are.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import detect as D  # noqa: E402
import detect_eval as E  # noqa: E402
import lc_runner as RU  # noqa: E402
import request_set as RS  # noqa: E402
import spec as SP  # noqa: E402
import spec_eval as SE  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
db = D.db
REPORT = ROOT / "data" / "lc04_eval_report.json"
LEDGER = ROOT / "data" / "lc04_hidden_eval_ledger.json"
CODE = [HERE / f for f in ("detect.py", "drill.py", "alerts.py", "frame.py")] + [ROOT / "metrics" / "registry.py", ROOT / "metrics" / "compute.py",
                                                                              D.PARAMS_PATH]
T = pd.Timestamp


def fingerprint():
    h = hashlib.sha256()
    for f in CODE:
        h.update(f.name.encode() + (f.read_bytes() if f.exists() else b""))
    return h.hexdigest()[:16]


def ledger():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else []


def log(event, **kw):
    rows = ledger()
    rows.append({"event": event, "at": datetime.now().isoformat(timespec="seconds"), **kw})
    LEDGER.write_text(json.dumps(rows, indent=2))


# ---------------------------------------------------------------------------------------------- scoring a timeline
def score_timeline(tl, keys, first_day, last_day):
    """Per seeded anomaly: the first alert that went out for it, its driver, minutes after the start; plus false alerts per day."""
    rows, related_incs = [], set()
    for k in keys:
        s, e = T(k["start_ts"]), T(k["end_ts"])
        day = (s.normalize() - D.STREAM_START).days
        if not first_day <= day <= last_day:
            continue
        hits = []
        for inc in tl["incidents"]:
            for st in inc["stages"]:
                if s <= T(st["ts"]) <= e + pd.Timedelta(hours=3) and E.related(st["finding"].segment, k["segment"], max(day - 1, 0)):
                    hits.append((inc, st))
                    related_incs.add(inc["incident_id"])
        hits.sort(key=lambda h: h[1]["ts"])
        first = hits[0] if hits else None
        final = (first[0], first[0]["stages"][-1]) if first else None          # the same incident, after its last update
        rows.append(dict(
            id=k["anomaly_id"], kind=k["kind"], segment=k["segment"], start=k["start_ts"], detected=bool(first),
            alert_minutes=(T(first[1]["ts"]) - s).total_seconds() / 60 if first else None,
            within_hour=bool(first) and (T(first[1]["ts"]) - s).total_seconds() / 60 <= 60,
            driver=final[1]["finding"].segment if final else None,
            driver_first=first[1]["finding"].segment if first else None,
            driver_correct=bool(final) and final[1]["finding"].segment == k["segment"],
            driver_correct_at_first_alert=bool(first) and first[1]["finding"].segment == k["segment"],
            incident=first[0]["incident_id"] if first else None))
    fa = [inc for inc in tl["incidents"] if inc["stages"] and inc["incident_id"] not in related_incs
          and first_day <= inc["first_tick"] // D.SLOTS <= last_day]
    days = list(range(first_day, last_day + 1))
    per_day = pd.Series([inc["first_tick"] // D.SLOTS for inc in fa], dtype=int).value_counts().reindex(days, fill_value=0)
    return dict(anomalies=rows, false_alerts=dict(total=len(fa), days=len(days), per_day_max=int(per_day.max()), per_day_mean=round(float(per_day.mean()), 2),
                                                  incidents=[inc["incident_id"] for inc in fa]))


def isolation_check():
    """No lender-audience alert names another lender, and every alert row belongs to the lender whose numbers it shows."""
    conn = db.get_conn("lc04")
    rows = conn.execute("SELECT tenant_id, audience_role, line1, line2 FROM alerts WHERE audience_role IN ('lender_user','lender_rm')").fetchall()
    conn.close()
    bad = [r for r in rows if any(o in (r["line1"] + r["line2"]) for o in ("L1", "L2", "L3", "L4", "L5") if o != r["tenant_id"])]
    return dict(alert_rows_checked=len(rows), leaks=len(bad))


def run_test(name):
    r = subprocess.run([sys.executable, str(ROOT / "tests" / name)], capture_output=True, text=True, cwd=str(ROOT))
    lines = [x for x in r.stdout.splitlines() if x.startswith(("PASS", "FAIL"))]
    return dict(test=name, passed=sum(x.startswith("PASS") for x in lines), failed=sum(x.startswith("FAIL") for x in lines), ok=r.returncode == 0,
                detail=next((x for x in lines if "metric-views" in x), ""))


def spec_results(requests, live):
    gen = SP.SpecGenerator(SP.default_llm()) if live else SP.SpecGenerator()
    res = SE.evaluate(gen, requests, use_llm=live)
    return dict(n=len(res), good=sum(r["ok"] for r in res), governed=sum(r["governed"] for r in res), median_ms=sorted(r["ms"] for r in res)[len(res) // 2],
                slowest_ms=max(r["ms"] for r in res), sources=sorted({r["source"] for r in res}),
                failed=[dict(id=r["id"], why=[k for k, v in r.items() if v is False]) for r in res if not r["ok"]])


# ---------------------------------------------------------------------------------------------- the report
def summarize(s):
    if s is None:
        return None
    a = s["anomalies"]
    return dict(n=len(a), detected=sum(x["detected"] for x in a), within_hour=sum(x["within_hour"] for x in a),
                right_driver=sum(x["driver_correct"] for x in a), within_hour_and_right=sum(x["within_hour"] and x["driver_correct_at_first_alert"] for x in a),
                fa=s["false_alerts"])


def build_report(sets, tests, spec_pub, spec_hid, iso):
    pub, hid = sets["public"], sets.get("hidden")
    line = summarize
    p, h = line(pub), line(hid)
    t = {x["test"]: x for x in tests}
    recon = t.get("test_lc04_metrics.py", {})
    iso_t = t.get("test_lc04_access.py", {})
    acc = [
        dict(criterion="Seeded anomalies detected within an hour with the correct driving segment", target="at least 5 of 6",
             public=f"{p['within_hour_and_right']} of {p['n']} (all {p['detected']} detected, {p['right_driver']} right driver; slower ones are in the table)",
             hidden=None if h is None else f"{h['within_hour_and_right']} of {h['n']} within the hour with the right driver; {h['detected']} detected, {h['right_driver']} named the right driver",
             status=None if h is None else ("met" if h["within_hour_and_right"] >= 5 else "partly" if h["detected"] >= 5 else "not met")),
        dict(criterion="False alerts per day", target="at most 3 a day",
             public=f"worst day {p['fa']['per_day_max']}, mean {p['fa']['per_day_mean']} a day ({p['fa']['total']} in {p['fa']['days']} days)",
             hidden=None if h is None else f"worst day {h['fa']['per_day_max']}, mean {h['fa']['per_day_mean']} a day ({h['fa']['total']} in {h['fa']['days']} days)",
             status=None if h is None else ("met" if h["fa"]["per_day_max"] <= 3 else "not met")),
        dict(criterion="Every number reconciles to its governed definition", target="all numbers",
             public=f"{recon.get('detail', '')}; findings equal compute() (drill test)",
             hidden=None if h is None else f"re-run after the hidden days were loaded: {recon.get('passed', 0)} checks passed, {recon.get('failed', 0)} failed",
             status=None if h is None else ("met" if recon.get("ok") else "not met")),
        dict(criterion="Zero cross-tenant data", target="none",
             public=f"{iso_t.get('passed', 0)} access/attack checks pass (forged principal, injection, other-lender asks, alerts per lender)",
             hidden=None if h is None else f"{iso_t.get('passed', 0)} checks pass; {iso['alert_rows_checked']} lender alert rows checked, {iso['leaks']} leaks",
             status=None if h is None else ("met" if iso_t.get("ok") and iso["leaks"] == 0 else "not met")),
        dict(criterion="Dashboard generated in under 2 minutes", target="< 120 s",
             public=f"{spec_pub['good']} of {spec_pub['n']} public requests good, slowest {spec_pub['slowest_ms']} ms",
             hidden=None if spec_hid is None else f"{spec_hid['good']} of {spec_hid['n']} hidden requests good, slowest {spec_hid['slowest_ms']} ms (sources {', '.join(spec_hid['sources'])})",
             status=None if spec_hid is None else ("met" if spec_hid["good"] == spec_hid["n"] and spec_hid["slowest_ms"] < 120000 else "partly")),
        dict(criterion="Refresh every 15 minutes over 100,000 accounts", target="15 min",
             public="measured at step 3: busiest 15-minute bucket for all 5 lenders refreshed in 486 ms on 100,000 accounts; a day of one lender's city series reads in 81 ms",
             hidden="same code, not re-measured", status="met"),
    ]
    limits = [
        "All data is synthetic and the anomalies were seeded by us: the numbers show the method, not real-world accuracy.",
        "The hidden period is ours too: the generator's difficulty was set before the detector existed and the hidden anomalies were not changed afterwards, but we wrote both sides.",
        "A segment with few events per hour (one payment gateway, PTPs kept in one lender and product) cannot be told apart from noise in the first hour: those anomalies are found later, not sooner. That is a limit of the data, not of the threshold.",
        "'Since' times are an estimate of the first deviating 15-minute bucket, accurate to about one bucket.",
        "Two segments that cover almost the same events (an agency that works in one city only) cannot be separated by the data; the drill-down names the one with the strongest evidence.",
        "The AI only drafts dashboard specs; everything it returns is validated in code. Alerts, numbers and drill-down are code. The free Groq tier limits how fast the AI step can run.",
    ]
    return dict(generated_at=datetime.now().isoformat(timespec="seconds"), code_fingerprint=fingerprint(),
                data_note="Synthetic data, seeded anomalies. Public days (days 1-30) were used to build and tune. Hidden days (31-40) are run once.",
                public=dict(**pub, summary=p), hidden=None if hid is None else dict(**hid, summary=h), acceptance=acc, limits=limits,
                tests=tests, spec=dict(public=spec_pub, hidden=spec_hid), isolation=iso)


# ---------------------------------------------------------------------------------------------- commands
DIAGNOSIS = ("After the hidden run (3 of 6), a post-run look found a BUG in the drill-down, not a tuning problem: it left out of the history "
             "every day on which ANY series had an alert, and when fewer than 5 days were left it raised an error that the timeline "
             "swallowed, silently dropping the incident. The detector had raised alerts for the 3 missed anomalies (5, 4 and 9 alerts on their days), "
             "but the drill-down never confirmed them. Fix (no thresholds touched): a day is left out only for segments that overlap the alerted "
             "segment, if too few days remain they are put back instead of giving up, and dropped incidents are counted. The same hidden "
             "data was then re-scored. That number is NOT a clean hold-out: the fix was made after seeing the result.")


def annotate_after_fix(rep):
    """Words for the 'after the fix' column and the honest limits (idempotent: safe to re-apply to a report)."""
    sh, sp = rep["hidden_after_fix"]["summary"], rep["public_after_fix"]["summary"]
    for row in rep["acceptance"]:
        if row["criterion"].startswith("Seeded anomalies"):
            row["hidden_after_fix"] = (f"HIDDEN: {sh['within_hour_and_right']} of {sh['n']} within the hour with the right driver; {sh['detected']} alerted, "
                                       f"{sh['right_driver']} right driver. PUBLIC with the fixed code: {sp['detected']} of {sp['n']} alerted, "
                                       f"{sp['within_hour_and_right']} within the hour, {sp['right_driver']} right driver (the weak PTP-kept one stays 'watched'). Not a clean hold-out.")
        if row["criterion"].startswith("False alerts"):
            row["hidden_after_fix"] = (f"HIDDEN: worst day {sh['fa']['per_day_max']}, mean {sh['fa']['per_day_mean']} a day ({sh['fa']['total']} in {sh['fa']['days']} days). "
                                       f"PUBLIC: worst day {sp['fa']['per_day_max']}, mean {sp['fa']['per_day_mean']}.")
    marker = "AFTER THE FIX"
    rep["limits"] = [x for x in rep["limits"] if not x.startswith(("HIDDEN RESULT", marker))]
    rep["limits"].insert(0, "HIDDEN RESULT, TWO NUMBERS: the official run (frozen code) found 3 of 6. A drill-down bug was then found and fixed and the same data "
                            f"re-scored: {sh['detected']} of 6 alerted, {sh['within_hour_and_right']} of 6 within the hour with the right driver. The second number is not a clean hold-out.")
    rep["limits"].insert(1, f"{marker}: the weak PTP-kept practice anomaly (about 8 kept promises an hour) is detected and drilled to the right segment but sits just under "
                            "the confirmation line (z -3.98 against 4.0), so it stays 'watched' and sends no alert. We did not move the line.")
    return rep


def refresh_report():
    rep = annotate_after_fix(json.loads(REPORT.read_text()))
    REPORT.write_text(json.dumps(rep, indent=2, default=str))
    print("report refreshed")


def rescore(done, pub_keys):
    """Post-run: re-score the SAME hidden data with the bug fixed. The official hidden result stays as it was logged."""
    if not done:
        sys.exit("run `final` first")
    if any(x["event"] == "hidden_rescore_after_fix" for x in ledger()):
        sys.exit("already re-scored once; the report holds both numbers")
    log("post_run_diagnosis", note=DIAGNOSIS)
    tl = RU.prepare(force=True, verbose=True)
    hid_keys = E.load_key("anomaly_key_hidden")
    pub = score_timeline(tl, pub_keys, D.FIRST_DAY, 29)
    hid = score_timeline(tl, hid_keys, 30, 39)
    RU.materialize(RU.STREAM_START)
    rep = json.loads(REPORT.read_text())
    sp, sh = summarize(pub), summarize(hid)
    rep["hidden_after_fix"] = dict(**hid, summary=sh, note=DIAGNOSIS)
    rep["public_after_fix"] = dict(**pub, summary=sp)
    rep["post_run_fix"] = dict(code_fingerprint=fingerprint(), official_fingerprint=rep["code_fingerprint"], dropped_incidents_after_fix=len(tl.get("dropped", [])))
    annotate_after_fix(rep)
    REPORT.write_text(json.dumps(rep, indent=2, default=str))
    log("hidden_rescore_after_fix", code_fingerprint=fingerprint(), summary=sh, public_summary=sp,
        note="same hidden data re-scored after the drill-down fix; official numbers unchanged in the report")
    print(json.dumps(dict(official=rep["hidden"]["summary"], after_fix=sh), indent=1, default=str))
    for a in hid["anomalies"]:
        print(f"  {a['id']} {a['kind']:22} detected={a['detected']} alert_min={a['alert_minutes']} within_hour={a['within_hour']} driver={a['driver']} correct={a['driver_correct']}")


def timeline_for(n_days):
    return RU.prepare(verbose=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["public", "final", "rescore", "refresh"])
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    started = [x for x in ledger() if x["event"] == "hidden_evaluation_started"]
    done = [x for x in ledger() if x["event"] == "hidden_evaluation_run"]
    pub_keys = E.load_key()
    if a.cmd == "rescore":
        return rescore(done, pub_keys)
    if a.cmd == "refresh":
        return refresh_report()
    tests = [] if a.skip_tests else [run_test("test_lc04_metrics.py"), run_test("test_lc04_access.py")]
    if a.cmd == "public":
        if RU.meta("hidden_loaded") == "1":
            sys.exit("the hidden days are loaded: use the existing report (the hidden run happens once)")
        tl = RU.prepare(verbose=True)
        sets = dict(public=score_timeline(tl, pub_keys, D.FIRST_DAY, 29))
        RU.materialize(RU.STREAM_START)
        report = build_report(sets, tests or [dict(test="-", ok=True, passed=0, failed=0, detail="")], spec_results(RS.PUBLIC, False), None, isolation_check())
        REPORT.write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps(report["acceptance"], indent=1, default=str)[:3000])
        return
    # ------------------------------------------------------------- THE HIDDEN RUN
    if done:
        sys.exit(f"the hidden evaluation was already run on {done[-1]['at']}; it is run once. See {REPORT.name}.")
    fp = fingerprint()
    if not started:
        log("hidden_evaluation_started", code_fingerprint=fp, params=D.load_params(), note="detector, drill-down, alert code and tuned parameters fingerprinted BEFORE the hidden days are loaded")
        subprocess.run([sys.executable, str(ROOT / "data" / "load_lc04.py"), "--add-hidden", "--confirm"], check=True, cwd=str(ROOT / "data"))
        RS.seed("hidden")
        log("hidden_requests_loaded", requests=len(RS.HIDDEN))
    changed = started and started[-1]["code_fingerprint"] != fp
    print("hidden days loaded; running the frozen pipeline over days 1-40 ...", flush=True)
    tl = RU.prepare(force=True, verbose=True)
    hid_keys = E.load_key("anomaly_key_hidden")
    sets = dict(public=score_timeline(tl, pub_keys, D.FIRST_DAY, 29), hidden=score_timeline(tl, hid_keys, 30, 39))
    RU.materialize(RU.stream_end())                                    # write the alerts rows so the isolation check sees every day
    iso = isolation_check()
    RU.materialize(RU.STREAM_START)
    tests = [run_test("test_lc04_metrics.py"), run_test("test_lc04_access.py")]
    spec_h = spec_results(RS.HIDDEN, True)
    spec_p = spec_results(RS.PUBLIC, False)
    report = build_report(sets, tests, spec_p, spec_h, iso)
    if changed:
        report["limits"].insert(0, "WARNING: the detector / drill-down / parameters changed after the hidden days were first loaded (see the ledger).")
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    log("hidden_evaluation_run", code_fingerprint=fp, code_changed_since_start=bool(changed),
        summary=report["public"]["summary"] and report["hidden"]["summary"])
    print(json.dumps(report["acceptance"], indent=1, default=str))


if __name__ == "__main__":
    main()
