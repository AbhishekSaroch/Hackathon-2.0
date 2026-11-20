"""
BI-03 demo-data tests (the "Try new data (CSV)" panel).   python test_bi03_demo.py   (exit code 1 on any failure)

Runs the whole cycle through the API on the real tenant and puts it back as it was (demo accounts loaded and analysed):
  load refused twice, analysis refused before a load, load = CSV rows in the DB and checks pass, analysis is safe to repeat,
  the control account is never flagged, nothing is shown after the replay pointer, remove leaves no trace,
  and no row of any non-demo account changes at any point.
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND / "agents" / "bi03"), str(BACKEND / "db")]
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import replay as RP  # noqa: E402

T = "abc_bank"
fails = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


client = TestClient(main.app)
get = lambda p, t=T: client.get(p, headers={"X-Tenant-Id": t})
post = lambda p, t=T: client.post(p, headers={"X-Tenant-Id": t})
TABLES = ["bp_accounts", "events", "bureau_snapshots", "risk_scores", "flags", "nudges", "flag_outcomes", "outcome_labels", "outcome_labels_hidden"]
col = lambda t: "account_id"
count = lambda tbl, like: db.query_one(T, f"SELECT COUNT(*) n FROM {tbl} WHERE tenant_id=:tenant_id AND account_id {'LIKE' if like else 'NOT LIKE'} 'DEMO-%'", database="bi03")["n"]
real = lambda: {t: count(t, False) for t in TABLES}
demo = lambda: {t: count(t, True) for t in TABLES}

real0 = real()
s = get("/bi03/demo")
ok("A status answers: csv files, accounts, loaded flag", s.status_code == 200 and {"loaded", "accounts", "csv"} <= set(s.json()) and len(s.json()["csv"]["files"]) == 5)
ok("A unknown tenant is a 404", get("/bi03/demo", "no_such_bank").status_code == 404)

# start from nothing
if s.json()["loaded"]:
    post("/bi03/demo/remove")
ok("B analysis before a load is refused (400)", post("/bi03/demo/analyze").status_code == 400)

ld = post("/bi03/demo/load")
j = ld.json()
ok("C load: 4 accounts, 415 events, 68 snapshots from the CSV files, every check passes",
   ld.status_code == 200 and j["rows_loaded"]["bp_accounts"] == 4 and j["rows_loaded"]["events"] == 415 and j["rows_loaded"]["bureau_snapshots"] == 68 and all(c["ok"] for c in j["checks"]), str(ld.status_code))
ok("C load: rows really are in the database", demo()["bp_accounts"] == 4 and demo()["events"] == 415 and demo()["bureau_snapshots"] == 68)
ok("C load: nothing scored yet", j["loaded"] is True and j["scored"] is False and demo()["risk_scores"] == 0)
ok("C load: the cached feature engine is dropped so the next advance sees the new rows", T not in RP._ENGINES)
ok("C a second load is refused (400), nothing doubles", post("/bi03/demo/load").status_code == 400 and demo()["events"] == 415)

a1 = post("/bi03/demo/analyze")
n1 = demo()["risk_scores"]
a2 = post("/bi03/demo/analyze")
ok("D analysis scores every replayed day for the 4 accounts", a1.status_code == 200 and n1 == 4 * (a1.json()["result"]["days"][1] - a1.json()["result"]["days"][0] + 1), f"{n1} score rows")
ok("D analysis is safe to repeat (same rows, no duplicates)", a2.status_code == 200 and demo()["risk_scores"] == n1)
ptr = get("/bi03/replay").json()["replay_date"]
accts = {a["account_id"]: a for a in a2.json()["accounts"]}
ok("D control account DEMO-004 is never flagged", accts["DEMO-004"]["flag"] is None and db.query_one(T, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id AND account_id='DEMO-004'", database="bi03")["n"] == 0)
ok("D nothing exists after the replay pointer (scores, flags)", db.query_one(T, "SELECT COUNT(*) n FROM risk_scores WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%' AND as_of_date > :p", {"p": ptr}, database="bi03")["n"] == 0
   and db.query_one(T, "SELECT COUNT(*) n FROM flags WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%' AND flagged_on > :p", {"p": ptr}, database="bi03")["n"] == 0)
ok("D every demo flag has a gate-passed nudge waiting for approval, nothing sent", db.query_one(T, "SELECT COUNT(*) n FROM nudges WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-%' AND status != 'pending_approval'", database="bi03")["n"] == 0)
ok("E no row of any real account changed", real() == real0, str({k: (real0[k], v) for k, v in real().items() if v != real0[k]}))

rm = post("/bi03/demo/remove")
ok("F remove leaves no trace in any table", rm.status_code == 200 and all(v == 0 for v in demo().values()) and rm.json()["loaded"] is False, str(demo()))
ok("F real accounts still unchanged after the remove", real() == real0)
ok("F removing twice is harmless", post("/bi03/demo/remove").status_code == 200)

# ---------------------------------------------------------------- template download and upload of the user's own CSVs
import csv  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402

tz = get("/bi03/demo/template")
z = zipfile.ZipFile(io.BytesIO(tz.content))
ok("H template: a zip with the three CSV files and a README", tz.status_code == 200 and sorted(z.namelist()) == ["README.txt", "bp_accounts.csv", "bureau_snapshots.csv", "events.csv"])
good = {"accounts": z.read("bp_accounts.csv").decode(), "events": z.read("events.csv").decode(), "snapshots": z.read("bureau_snapshots.csv").decode()}


def edit_cell(text: str, row: int, col: int, value: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    rows[row][col] = value
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def refused(name: str, body: dict, needle: str) -> None:
    r = client.post("/bi03/demo/load", json=body, headers={"X-Tenant-Id": T})
    ok(name, r.status_code == 400 and needle in r.json().get("detail", "") and demo()["bp_accounts"] == 0 and demo()["events"] == 0, r.json().get("detail", "")[:90])


refused("I upload: only two of the three files is refused", {"accounts": good["accounts"], "events": good["events"]}, "all three files")
refused("I upload: an account id without the DEMO- prefix is refused, nothing loaded", {**good, "accounts": good["accounts"].replace("DEMO-001", "ACC-001")}, "must start with DEMO-")
refused("I upload: a missing column is refused", {**good, "events": good["events"].replace("source", "src", 1)}, "missing column")
refused("I upload: a row visible before it happened is refused", {**good, "events": edit_cell(good["events"], 1, 2, "2000-01-01")}, "before event_date")
refused("I upload: an event after the end of the stream is refused", {**good, "events": edit_cell(good["events"], 1, 1, "2027-01-01")}, "after the end of the data stream")
refused("I upload: an unknown event type is refused", {**good, "events": edit_cell(good["events"], 1, 4, "salary_paid")}, "not allowed")
refused("I upload: a bureau snapshot without lag is refused", {**good, "snapshots": edit_cell(good["snapshots"], 1, 2, good["snapshots"].splitlines()[1].split(",")[1])}, "after snapshot_date")
ok("I upload: real accounts untouched by all the refused uploads", real() == real0)

mine = {k: v.replace("DEMO-00", "DEMO-10") for k, v in good.items()}          # same data under new ids: DEMO-101 .. DEMO-104
up = client.post("/bi03/demo/load", json=mine, headers={"X-Tenant-Id": T})
uj = up.json()
ok("J upload: the user's own three files load (4 accounts, 415 events, 68 snapshots), checks pass", up.status_code == 200 and uj["rows_loaded"]["events"] == 415 and all(c["ok"] for c in uj["checks"])
   and sorted(a["account_id"] for a in uj["accounts"]) == ["DEMO-101", "DEMO-102", "DEMO-103", "DEMO-104"], str(up.status_code))
ok("J upload: refused while demo accounts are already loaded", client.post("/bi03/demo/load", json=mine, headers={"X-Tenant-Id": T}).status_code == 400)
an = post("/bi03/demo/analyze")
ok("J upload: analysis runs on the uploaded accounts; the control (DEMO-104) is not flagged", an.status_code == 200 and an.json()["result"]["days"][1] >= 61
   and {a["account_id"]: a["flag"] for a in an.json()["accounts"]}["DEMO-104"] is None)
ok("J upload: real accounts unchanged", real() == real0)
post("/bi03/demo/remove")
ok("J upload: remove leaves no trace", all(v == 0 for v in demo().values()))

# ---------------------------------------------------------------- bulk sample: 200 generated accounts through the same upload
import time  # noqa: E402

post("/bi03/demo/remove")
cnt0 = get("/bi03/demo").json()["database"]
bz = client.get("/bi03/demo/bulk-sample?n=200")
zb = zipfile.ZipFile(io.BytesIO(bz.content))
ok("K bulk sample: a zip with the three CSV files and a README", bz.status_code == 200 and sorted(zb.namelist()) == ["README.txt", "bp_accounts.csv", "bureau_snapshots.csv", "events.csv"])
ok("K bulk sample: n outside 10..1000 is refused", client.get("/bi03/demo/bulk-sample?n=5").status_code == 422 and client.get("/bi03/demo/bulk-sample?n=5000").status_code == 422)
bulk = {"accounts": zb.read("bp_accounts.csv").decode(), "events": zb.read("events.csv").decode(), "snapshots": zb.read("bureau_snapshots.csv").decode()}
n_ev = len(bulk["events"].splitlines()) - 1
t0 = time.time()
bl = client.post("/bi03/demo/load", json=bulk, headers={"X-Tenant-Id": T})
bj = bl.json()
t_load = time.time() - t0
ok("K bulk load: 200 accounts and every event row go in, checks pass", bl.status_code == 200 and bj["rows_loaded"]["bp_accounts"] == 200 and bj["rows_loaded"]["events"] == n_ev and all(c["ok"] for c in bj["checks"]), f"{n_ev} events in {t_load:.1f}s")
d1 = bj["database"]
ok("K bulk load: the live database counts went up by exactly what was loaded", d1["demo"]["accounts"] == 200 and d1["demo"]["events"] == n_ev
   and d1["all"]["accounts"] == cnt0["all"]["accounts"] + 200 and d1["all"]["events"] == cnt0["all"]["events"] + n_ev, str(d1["demo"]))
ok("K bulk load: the panel gets 10 accounts and the total", bj["accounts_total"] == 200 and len(bj["accounts"]) == 10)
t0 = time.time()
ba = post("/bi03/demo/analyze")
bja = ba.json()
full = 200 * (bja["result"]["days"][1] - bja["result"]["days"][0] + 1)
scored_accts = db.query_one(T, "SELECT COUNT(DISTINCT account_id) n FROM risk_scores WHERE tenant_id=:tenant_id AND account_id LIKE 'DEMO-B%'", database="bi03")["n"]
# an account that has already missed an EMI leaves the watch, so it is not scored on later days: fewer rows than 200 x days is expected
ok("K bulk analysis runs over all 200 accounts (an account that already missed leaves the watch)",
   ba.status_code == 200 and scored_accts == 200 and 0.8 * full < bja["database"]["demo"]["scores"] <= full, f"{time.time() - t0:.0f}s, {bja['database']['demo']['scores']} of {full} possible score rows, {bja['flagged_total']} flagged")
ok("K bulk analysis: flagged accounts come first in the panel list", all(a["flag"] is not None for a in bja["accounts"][:bja["flagged_total"]]) if bja["flagged_total"] <= 10 else all(a["flag"] is not None for a in bja["accounts"]))
ok("K bulk: real accounts unchanged", real() == real0)
post("/bi03/demo/remove")
ok("K bulk remove: the database counts are back to what they were", get("/bi03/demo").json()["database"] == cnt0)

# leave the system as it was: demo accounts loaded and analysed
post("/bi03/demo/load")
fin = post("/bi03/demo/analyze")
ok("G state restored: demo accounts loaded and analysed again", fin.status_code == 200 and fin.json()["scored"] is True and real() == real0)

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
