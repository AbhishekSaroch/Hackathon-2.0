"""
LC-04 spec generator evaluation on the 10 PUBLIC requests (the hidden set is for step 12).

  python spec_eval.py            the deterministic fallback only (no AI)
  python spec_eval.py --live     also the real model (Groq), a handful of tokens per request, cached

For every request: is every panel a governed metric the role may see (always, by validation), is the view relevant to the
request, did it respect scope and role, did a forbidden / ad hoc ask get explained, and how long did it take (limit: 2 minutes).
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "metrics"))
sys.path.insert(0, str(HERE.parents[1] / "db"))
import access as A  # noqa: E402
import registry  # noqa: E402
import request_set as RS  # noqa: E402
import spec as SP  # noqa: E402


def check(gen, req, use_llm):
    p = A.login(req["user"])
    out = gen.generate(p, req["text"], use_llm=use_llm)
    spec, notes, ex = out["spec"], out["notes"], req["expect"]
    mets = [pn["metric_id"] for pn in spec["panels"]]
    flat = json.dumps(spec)
    res = dict(
        id=req["id"], source=out["source"], ms=out["ms"], panels=len(spec["panels"]),
        governed=all(m in registry.METRICS and m in p.metrics for m in mets),
        relevant=any(m in mets for m in ex["any"]) or any(registry.get(m).family in ex.get("fam", []) for m in mets),
        role_ok=not any(m in mets for m in ex.get("never", [])),
        scope_ok=set(spec["lenders"]) <= set(p.lenders) and not any(f'"{L}"' in flat for L in ex.get("lenders", [])),
        refused_explained=(not ex.get("refuse")) or bool(notes),
        window_ok=("window" not in ex) or spec["window"] == ex["window"], nonempty=len(mets) > 0, under_2_min=out["ms"] < 120_000, notes=notes)
    res["ok"] = all(res[k] for k in ("governed", "relevant", "role_ok", "scope_ok", "refused_explained", "window_ok", "nonempty", "under_2_min"))
    return res


def evaluate(gen, requests, use_llm):
    return [check(gen, r, use_llm) for r in requests]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    for label, gen, llm in [("fallback (no AI)", SP.SpecGenerator(), False)] + ([("live model", SP.SpecGenerator(SP.default_llm()), True)] if a.live else []):
        res = evaluate(gen, RS.PUBLIC, llm)
        print(f"\n== {label}")
        for r in res:
            print(f"{r['id']} {'OK ' if r['ok'] else 'BAD'} source={r['source']:8} panels={r['panels']:2} {r['ms']:6} ms  "
                  + ("" if r["ok"] else str({k: v for k, v in r.items() if v is False})))
        print(f"{sum(r['ok'] for r in res)} of {len(res)} good; slowest {max(r['ms'] for r in res)} ms; sources {sorted({r['source'] for r in res})}")
        if llm:
            for r in res:
                if r["notes"]:
                    print("  notes", r["id"], r["notes"][:3])


if __name__ == "__main__":
    main()
