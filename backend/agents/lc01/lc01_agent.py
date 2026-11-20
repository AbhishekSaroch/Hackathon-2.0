"""
LC-01 agent: the one entry point. ask(user_id, question) -> response dict (also written to qa_log; refusals to audit_log).

Flow:  login (scope from the user record) -> precheck (code) -> intent (LLM, or rules if the model is down) -> finalize (code)
       -> clarify | refuse | governed query -> answer + chart + root cause + the SQL that ran.
Tools the agent calls (each is a step in the trace): metric_definitions, warehouse_sql, chart_generator.
"""
import os
import time

import lc01_access as A
import lc01_answer as W
import lc01_compiler as C
import lc01_intent as I
import lc01_registry as R
import lc01_sources as X

AUDITED = {"cross_tenant", "write_attempt", "raw_sql", "injection"}


def _base(question, p):
    return dict(question=question, user=A.principal_json(p), notes=[], sql=[], steps=[], chart=None, table=None, explanation=None,
                clarification=None, refusal=None, headline="", metric=None, period=None, scope=None, source=None)


def ask(user_id: str, question: str, hint_metric: str | None = None, llm=None, use_llm: bool = True) -> dict:
    t0 = time.time()
    use_llm = use_llm and os.getenv("LC01_NO_LLM") != "1"          # tests / offline demo: rule-based parser only, zero tokens
    p = A.login(user_id)                                   # AccessError -> caller maps to 401
    question = (question or "").strip()
    out = _base(question, p)
    if not question:
        out.update(status="error", headline="Please type a question.")
        return out
    if len(question) > 600:
        out.update(status="error", headline="That question is too long; please keep it under 600 characters.")
        return out
    # Questions about the other modules' data (BI-02 reasons, BI-03 early warning, LC-04 command center) are answered from ask_all.db by
    # lc01_sources; anything else, and every user of the lender warehouse, continues in the flow below exactly as before.
    xs = None
    try:
        xs = X.try_answer(p, question)
    except A.AccessError as e:
        xs = dict(status="refused", refusal=dict(category="cross_tenant", reason=str(e)), headline=f"Refused: {e}.", source="xsource")
        A.audit(p, "cross_tenant", str(e), question)
    except Exception as e:                                  # the extra sources must never break the lender flow
        out["notes"].append(f"The other-module data could not be read ({type(e).__name__}); answering from the lender warehouse only.")
        if not X.has_lender_warehouse(p):
            xs = dict(status="error", headline="The other-module data could not be read right now.", source="xsource")
    if xs is None and not X.has_lender_warehouse(p):               # a user whose data lives only in the other modules (no lender-warehouse rows)
        try:
            I.precheck(p, question)                                 # the same code-level walls run first: typed SQL, writes, injection, other lenders
            ex = "; ".join(e["text"] for e in X.examples(p)[:3])
            xs = dict(status="unsupported", source="xsource", headline="I could not match that to a BI-02, BI-03 or LC-04 measure for your user." + (f" Try: {ex}" if ex else ""))
        except I.Refusal as r:
            xs = dict(status="refused", source="precheck", refusal=dict(category=r.category, reason=r.reason), headline=r.reason)
            if r.category in AUDITED:
                A.audit(p, r.category, r.reason, question)
    if xs is not None:
        out["steps"] += xs.pop("steps", [])
        out.update(xs)
        return _finish(out, p, question, t0)
    try:
        try:
            decision, src = I.interpret(p, question, hint_metric, llm, use_llm)
        except I.Refusal as r:
            decision, src = dict(action="refuse", category=r.category, reason=r.reason), "precheck"
        out["source"] = src
        out["steps"].append(dict(tool="metric_definitions", detail=f"{len(R.METRICS)} governed metrics; question read by {'the language model' if src == 'llm' else 'code' if src == 'precheck' else 'the rule-based parser'}", ms=int((time.time() - t0) * 1000)))
        if decision["action"] == "refuse":
            out.update(status="refused", refusal=dict(category=decision["category"], reason=decision["reason"]), headline=decision["reason"])
            if decision["category"] in AUDITED:
                A.audit(p, decision["category"], decision["reason"], question)
        elif decision["action"] == "clarify":
            cl = decision["clarification"]
            out.update(status="clarify", clarification=cl, headline=cl["question"])
        else:
            intent = decision["intent"]
            try:
                resp = W.answer(p, intent)
                out["steps"] += resp.pop("steps")
                out.update(resp)
            except A.AccessError as e:                     # second wall: the compiler / data layer refused a lender outside the scope
                A.audit(p, "cross_tenant", str(e), question)
                out.update(status="refused", refusal=dict(category="cross_tenant", reason=str(e)), headline=f"Refused: {e}.")
            except C.UnknownValue as e:
                out.update(status="no_data", headline=f"There is no {e.dim} called \"{e.value}\" in your portfolio.",
                           notes=[f"{e.dim} values I can see for you: {', '.join(e.valid)}."], metric=dict(id=intent["metric"], name=R.METRICS[intent["metric"]].name))
            except R.MetricError as e:
                out.update(status="unsupported", headline=str(e))
            except C.SanityError as e:
                out.update(status="error", headline=f"I could not give a reliable number: {e}. Nothing was shown.")
    except A.AccessError as e:
        out.update(status="refused", refusal=dict(category="cross_tenant", reason=str(e)), headline=f"Refused: {e}.")
    return _finish(out, p, question, t0)


def _finish(out: dict, p, question: str, t0: float) -> dict:
    out["latency_ms"] = int((time.time() - t0) * 1000)
    sql_text = "\n\n".join(f"-- {s['label']}\n{s['sql']}\n-- params: {s['params']} lenders: {s['lenders']}" for s in out["sql"]) or None
    out["log_id"] = A.log_answer(p, question, out["status"], (out.get("metric") or {}).get("id"), sql_text, out["latency_ms"], {k: v for k, v in out.items() if k != "user"})
    return out
