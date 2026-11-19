"""
BI-02 regression check. Run it after every BI-03 step: if BI-02 changed in any way, it says FAIL.

  python check_bi02.py            compare BI-02 with the recorded baseline (exit code 1 on any FAIL)
  python check_bi02.py --record   record a new baseline (only when a BI-02 change is intended)

What it checks (nothing here calls the LLM, so it costs no tokens):
  1. BI-02 INPUT tables are byte-for-byte the same (row counts + checksums)
  2. BI-02 OUTPUT tables never lost rows (they may grow when someone runs the agent)
  3. API contract: BI-02 endpoints still answer 200 with the same fields, accounts total unchanged
  4. Tenant isolation: another tenant still sees nothing
  5. Pinned accounts still have the same label + confidence
  6. BI-02 source files are unchanged (hash). If you changed one on purpose, re-record.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # starlette/httpx deprecation noise, not a problem
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path[:0] = [str(BACKEND), str(BACKEND / "db")]
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

BASELINE = HERE / "bi02_baseline.json"
TENANT = "abc_bank"
INPUT_TABLES = ["accounts", "repayments", "calls", "promises", "statement_txns", "gold_labels"]
OUTPUT_TABLES = ["assessments", "evidence", "agent_runs", "agent_steps", "recommendations"]
GUARDED_GLOBS = ["agents/bi02/*.py", "tools/account_tools.py", "routers/bi02.py"]


def table_fingerprint(conn: sqlite3.Connection, table: str) -> dict:
    h, n = hashlib.md5(), 0
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
        h.update(repr(tuple(row)).encode("utf-8"))
        n += 1
    return {"rows": n, "md5": h.hexdigest()}


def guarded_hashes() -> dict:
    out = {}
    for pattern in GUARDED_GLOBS:
        for p in sorted(BACKEND.glob(pattern)):
            out[p.relative_to(BACKEND).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def shape(resp) -> dict:
    body = resp.json()
    if isinstance(body, dict):
        return {"status": resp.status_code, "keys": sorted(body)}
    return {"status": resp.status_code, "item_keys": sorted(body[0]) if body else None}


def collect() -> dict:
    conn = sqlite3.connect(db.DB_PATH)
    snap = {"input": {t: table_fingerprint(conn, t) for t in INPUT_TABLES},
            "output_rows": {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in OUTPUT_TABLES}}

    pinned_ids = [r[0] for r in conn.execute("SELECT DISTINCT account_id FROM assessments ORDER BY account_id LIMIT 5")]
    snap["pinned"] = {}
    for a in pinned_ids:
        label, conf = conn.execute("SELECT label, confidence FROM assessments WHERE account_id=? "
                                   "ORDER BY assessment_id DESC LIMIT 1", (a,)).fetchone()
        snap["pinned"][a] = {"label": label, "confidence": conf}
    detail_id = pinned_ids[0] if pinned_ids else conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    conn.close()

    with TestClient(main.app) as client:
        urls = ["/health", "/bi02/accounts?page_size=5", "/bi02/accounts?label=unassessed&page_size=1",
                f"/bi02/accounts/{detail_id}", "/bi02/recommendations?status=pending", "/bi02/metrics"]
        snap["api"] = {u.split("?")[0] if "/accounts/" not in u else "/bi02/accounts/{id}": shape(client.get(u)) for u in urls}
        snap["api"]["/bi02/accounts?label=unassessed"] = shape(client.get("/bi02/accounts?label=unassessed&page_size=1"))
        snap["accounts_total"] = client.get("/bi02/accounts?page_size=1").json()["total"]
        snap["gold_total"] = client.get("/bi02/metrics").json()["n_gold"]
        other = client.get("/bi02/accounts", headers={"X-Tenant-Id": "other_bank"}).json()
        snap["other_tenant_total"] = other["total"]
        snap["other_tenant_detail_status"] = client.get(f"/bi02/accounts/{detail_id}", headers={"X-Tenant-Id": "other_bank"}).status_code
    snap["source_hashes"] = guarded_hashes()
    return snap


def compare(base: dict, now: dict) -> list[tuple[bool, str, str]]:
    r = []
    add = lambda ok, name, detail="": r.append((ok, name, detail))

    for t in INPUT_TABLES:
        b, n = base["input"][t], now["input"][t]
        add(b == n, f"input table {t} unchanged", f"baseline {b['rows']} rows, now {n['rows']} rows" if b != n else f"{n['rows']} rows")
    for t in OUTPUT_TABLES:
        b, n = base["output_rows"][t], now["output_rows"][t]
        add(n >= b, f"output table {t} lost no rows", f"baseline {b}, now {n}")

    for url, b in base["api"].items():
        n = now["api"].get(url, {})
        same_keys = (b.get("keys") == n.get("keys")) if "keys" in b else (b.get("item_keys") is None or n.get("item_keys") is None or b["item_keys"] == n.get("item_keys"))
        add(n.get("status") == b["status"] and same_keys, f"API {url} same status + fields", f"baseline {b}, now {n}" if not (n.get("status") == b["status"] and same_keys) else f"status {n['status']}")
    add(base["accounts_total"] == now["accounts_total"], "BI-02 accounts total unchanged (no BI-03 accounts leaked in)", f"baseline {base['accounts_total']}, now {now['accounts_total']}")
    add(base["gold_total"] == now["gold_total"], "gold labels total unchanged", f"baseline {base['gold_total']}, now {now['gold_total']}")
    add(now["other_tenant_total"] == 0 and now["other_tenant_detail_status"] == 404, "tenant isolation (other tenant sees nothing)",
        f"total {now['other_tenant_total']}, detail status {now['other_tenant_detail_status']}")

    for a, b in base["pinned"].items():
        n = now["pinned"].get(a)
        add(n == b, f"pinned account {a} same label + confidence", f"baseline {b}, now {n}" if n != b else f"{b['label']} {b['confidence']}")

    for f, h in base["source_hashes"].items():
        n = now["source_hashes"].get(f)
        add(n == h, f"BI-02 file unchanged: {f}", "CHANGED or missing (if intended, run --record)" if n != h else "")
    for f in now["source_hashes"]:
        if f not in base["source_hashes"]:
            add(False, f"new BI-02 file not in baseline: {f}", "run --record if intended")
    return r


def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="record a new baseline")
    ap.add_argument("--baseline", default=str(BASELINE))
    args = ap.parse_args()
    path = Path(args.baseline)

    now = collect()
    if args.record:
        path.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
        print(f"baseline recorded: {path}")
        print(f"  accounts {now['accounts_total']} | gold {now['gold_total']} | assessments {now['output_rows']['assessments']} "
              f"| pinned accounts {len(now['pinned'])} | guarded files {len(now['source_hashes'])}")
        return 0

    if not path.exists():
        print(f"no baseline at {path}. Run with --record first.")
        return 2
    results = compare(json.loads(path.read_text(encoding="utf-8")), now)
    for ok, name, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and (not ok or "unchanged" not in name) else ""))
    failed = sum(1 for ok, *_ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_cli())
