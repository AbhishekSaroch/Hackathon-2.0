# LC-01 plan and status: Ask-your-portfolio agent

Built Nov 20, 2026 (steps 0 to 10, all done). Numbers and the 7-minute script are in `DEMO_LC01.md`; project overview in `context.md` section 14.
Nothing is committed or pushed. LC-01 is its own database (`backend/lc01.db`), its own folder (`backend/agents/lc01/`), its own router and its
own pages; the only shared files touched are listed in "What we touched outside LC-01" below.

## Goal (the brief)

A lender user asks a plain-language question about their own portfolio and gets a correct, explained answer in minutes: the answer, a chart,
the query behind it and, when asked, a root cause. Resolve metrics against the definitions layer and ask a clarifying question when ambiguous;
refuse and log any cross-tenant request. Acceptance: 85% correct on 50 hidden questions, a clarifying question on every ambiguous one, zero
cross-tenant leaks; under 60 s for typical questions; tenant scoping at the data layer; the query behind every answer is shown.

## Design in one paragraph

**The model never writes SQL and never writes a sentence of the answer.** It turns the question into a small structured intent (metric id,
dimensions, filters, months, "why?"). Code validates the intent against 38 governed metrics, the user's lender scope and the dimensions each
table has, compiles ONE governed SELECT from registry fragments (values are bound parameters), runs it once per lender in scope through the
tenant-safe data layer, and builds the answer text, chart and root cause from the computed numbers. The overlap rule (an everyday word that
fits several definitions = ask, never guess), the "who is worst / underperforming with no measure named = ask" rule and every refusal
(another lender, raw SQL, writes, prompt injection) are enforced in code, before and after the model, so they hold even when the model is wrong.

```
question -> login (scope from the user record) -> precheck (code: other lender, SQL, write, injection)
         -> intent (LLM, or rule-based parser if the model is down) -> finalize (code: overlap, no-measure, months, ranking direction)
         -> clarify | refuse (audited) | governed SELECT per lender -> sanity check -> answer + chart + root cause + SQL shown
```

## Steps

| # | Step | Status |
|---|---|---|
| 0 | Baseline: `check_bi02.py` green (31), shared files read, nothing committed | Done |
| 1 | Schema `backend/db/schema_lc01.sql` (11 tables, every one with `tenant_id`), `db.py` knows `"lc01"` (additive) | Done |
| 2 | Synthetic warehouse `data/generate_lc01.py` (seed 7; 3 lenders, 6,800 loans, 10 months, 27.9k calls, 3.1k promises, 64k payments, 525 disputes) + `load_lc01.py --reset` with 24 integrity checks. Six planted stories (S1 to S6) are the root-cause answer keys, `data/lc01_world_truth.json` | Done |
| 3 | Governed metric layer `agents/lc01/lc01_registry.py`: 38 metrics, 16 of them in 6 deliberately overlapping groups (collection, delinquency, roll, contact, PTP kept, bounce); lender quirks (L2 5-day grace, L3 gold NPA at 120 days) shown as a note | Done |
| 4 | Tenant-safe compiler `lc01_compiler.py` + SQL checker and scope in `lc01_access.py` (read-only single SELECT, whitelisted tables, `tenant_id = :tenant_id` as the only tenant condition, no OR / NOT / UNION) | Done |
| 5 | Language layer `lc01_intent.py`: code pre-checks, LLM intent (gpt-oss-20b, cached), rule-based fallback, code finalize | Done |
| 6 | Answer assembly `lc01_answer.py`: templated headline, chart spec, counterfactual root cause (2 levels + evidence metrics), lender notes, every SQL shown; orchestrator `lc01_agent.py` | Done |
| 7 | API `routers/lc01.py` (`X-User-Id`, lazy import, mounted in `main.py` inside try/except) | Done |
| 8 | UI: sidebar group 4 (Ask your portfolio, Metric definitions, Ask evaluation), "Signed in as" switcher, answer cards with chart, "Why", table, SQL and trace, clarification buttons | Done |
| 9 | Evaluation `lc01_evaluate.py`: 20 public questions (tuned on) and 50 hidden (run once, ledgered), keys from an independent pandas oracle `lc01_oracle.py` | Done |
| 10 | Tests (4 suites), `DEMO_LC01.md`, `context.md` section 14, final regression | Done |

## What we touched outside LC-01 (all additive)

`backend/db/db.py` (a fourth database name), `backend/main.py` (one more try/except router), `frontend/src/{api.js, App.jsx, components/Layout.jsx, pages.css}`,
`frontend/vite.config.js` (`/lc01` proxy), `.gitignore` (`csv_lc01/`), `.claude/launch.json` (two preview entries on ports 8010 / 5174).
BI-02 / BI-03 / LC-04 source files are unchanged; `check_bi02.py` still passes 31 of 31.

## Tests (plain scripts, run one at a time, each on a COPY of lc01.db, zero LLM tokens)

`python backend/tests/test_lc01_data.py` (registry, 304 metric reconciliations against pandas, planted stories present),
`test_lc01_access.py` (scope, forged principals, 29 adversarial SQL statements, isolation), `test_lc01_agent.py` (overlap / no-measure / refusal rules
with a stub model, audit rows, sanity check), `test_lc01_api.py` (HTTP behaviour, per-user history and audit, BI-02 still starts if LC-01 fails to import).
`LC01_NO_LLM=1` makes the agent use the rule-based parser only (used by the API test and handy for an offline demo).

## Not done / known limits

Trimmed warehouse (11 tables, 38 metrics instead of about 40 tables and 60). No real login: the user is picked from a demo switcher (header `X-User-Id`).
Charts are drawn by a small SVG component, not a chart library. No follow-up memory between questions (each question stands alone; a clarification is
answered by clicking an option). The Groq free tier limits throughput to roughly 3 to 4 model calls a minute, so a burst of questions can wait up to a minute.
