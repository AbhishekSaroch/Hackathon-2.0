# LC-04 handoff: start here in the new chat

Written Nov 19, 2026 at the end of the BI-02 / BI-03 chat. Read this file, then `context.md` (how the project is built) and `DEMO.md` (how BI-02 / BI-03 are demoed). Do not read them all line by line: skim the tables.

**Suggested first message to the new chat:** "Read LC04_CONTEXT.md and context.md. Do not write code yet. Propose a step plan for LC-04 like we did for BI-03 (small steps, each with a 'done when'), then wait for me to say 'Haan, Step N banao'."

---

## 1. Where we are

- **BI-02** (why a late customer is not paying): built end to end, demo-ready, not fully validated (details in `context.md` section 9 and 10).
- **BI-03** (pre-delinquency early warning): done, all tests green, demo pack in `DEMO.md`.
- **LC-04: not started.** LC-01 not started either (it shares the metric definitions layer with LC-04).
- **Clock:** the build clock started Thu 19 Nov 9:35 am, presentations start Fri 20 Nov 5:30 pm, 7 minutes to present plus 3 minutes of Q&A. Ask the user how much time is left before planning.
- Nothing is committed. The user pushes everything once at the end. Never commit or push unless asked.

## 2. How the user works (important)

- Hinglish (English words in Roman letters). New to hackathons. Wants **short, precise answers, no overengineering, no long explanations**.
- Work in **small numbered steps**; propose the plan first, then wait for "Haan, Step N banao". After a step: what was built, what was tested, what is next. "Fast mode" (agreed for BI-03 steps 10 to 12): run only the test suites a change affects, one full run at the end.
- The user runs backend and frontend from **their own PowerShell terminals**. Do not rely on Claude's preview servers for their demo (for your own checks, start a second copy on other ports: `API_PORT=8010 WEB_PORT=5174`, stop only those ports afterwards).
- **Never read, print or use `backend/.env`.** It holds the Groq key (and unrelated entries).
- Customer-facing text: Hinglish in Roman letters. Manager-facing UI text: English. No emoji in the product.
- Be honest in numbers and caveats (the whole project is synthetic data). That honesty was a deliberate selling point.

## 3. The LC-04 brief (verbatim)

**LC-04, Lender / Client: Self-building operations command dashboard**

*Business context.* CN leadership, operations heads and lender relationship managers work from more than 20 static dashboards. Definitions differ between them, views go stale, and nobody notices an intraday problem (a dialer outage in one city, an agency mis-dispositioning calls, a payment gateway failing) until it shows up in month-end numbers. A new view takes the BI team about a week.

*Objective.* Give each role a live view that assembles itself around what that person needs to act on, and flag and explain problems within the hour.

*Users.* CN leadership; operations and delivery heads; lender relationship managers; lender users (their own data only).

*Scope.* In: generating dashboard views from a role and a plain-language request, governed metrics only, intraday refresh, anomaly detection, drill-down to the driving segment, annotations and alerts, role-based access. Out: answering one-off questions in chat, which LC-01 covers.

*Why agentic.* A dashboard shows what someone configured in advance. The agent decides what deserves attention today, investigates across dimensions to find what is driving it, and rebuilds the view around the finding.

*What makes it hard.* Every number must reconcile to one governed definition across all views. Seasonality (day of week, salary cycles, festivals) produces swings that are not anomalies, so alert fatigue is the default failure. Drill-down must find the driving segment, not just restate the symptom.

*Functional requirements.*
1. Generate a dashboard specification (metrics, charts, filters) from a role and a request, using only governed metric definitions.
2. Render it with a refresh at least every 15 minutes.
3. Monitor key metrics intraday and detect anomalies after accounting for seasonality.
4. Drill down to the segment driving each anomaly and annotate the dashboard with the finding.
5. Enforce role-based views: a lender user sees only that lender's data.
6. Send an alert with a two-line explanation and a link to the annotated view.

*Non-functional.* Dashboard generated in under 2 minutes. Refresh every 15 minutes over 100,000 accounts. Tenant isolation enforced at the data layer.

*Data provided.*

| Dataset | Volume | Notes |
|---|---|---|
| Intraday event stream (LC-04 only) | 30 days public, 10 days hidden | contacts, PTPs, payments, dispositions at event level |
| Metric definitions layer (shared with LC-01) | about 60 metrics | the only allowed definitions |
| D1, D13 | full | dimensions for drill-down |
| Role definitions | 5 roles | including lender users scoped to one tenant |
| Dashboard request set | 10 public, 10 hidden | plain-language requests by role |
| Seeded anomalies | hidden | 6, including a city dialer outage, agency mis-dispositioning and a gateway failure |

*Tools the agent must call.* Intraday event stream, data warehouse SQL, metric definitions layer, any charting or front-end library, alerting.

*Guardrails.* Governed metrics only; no ad hoc definitions on a published view. Zero cross-tenant data.

*Acceptance criteria (hidden 10 days).* At least **5 of 6** seeded anomalies detected **within an hour** with the **correct driving segment**, **no more than 3 false alerts per day**, **every number reconciles** to its governed definition, **zero cross-tenant data**.

## 4. Judging (same for every team)

Five questions, 7 min present + 3 min Q&A: (1) does it actually work (working prototype), (2) how deep did you go (technical depth), (3) can you explain it simply, (4) would we use it on Monday (business impact), (5) have we seen this before (creativity). Special awards: Best Demo, Most Creative Solution, Best Use of Data.

## 5. Organizers give no test data: we generate everything

Same as BI-02 / BI-03: the intraday stream, the 60 metric definitions, the 5 roles, the request sets and the 6 seeded anomalies are **ours to create**. Consequences to plan for:
- The evaluation is self-graded. Keep the discipline from BI-03: seed the 6 hidden anomalies with a **separate seed / script that the detector never sees**, tune only on the 30 public days, run the hidden 10 days **once** and log it (see `backend/data/bi03_hidden_eval_ledger.json` and `outcomes.py` guards for the pattern).
- Say out loud in the demo that the data is synthetic and the anomalies were seeded by us.
- The brief mentions **D13** as a dimension table. We do not have its definition (D1 is the account table). Ask the user what D13 contains (likely agency / city / dialer type) or define it ourselves and say so.
- Keep the generator's difficulty honest (a version that is too easy proves nothing, and BI-03's first version was too hard: 0.7x lift).

## 6. What to reuse from this repo

| Need | Reuse |
|---|---|
| Tenant-safe data access | `backend/db/db.py`: every query must contain `:tenant_id`, tenant is bound by the module. A new database is added by extending `_paths()` (e.g. `"lc04"`, file `lc04.db`, schema `schema_lc04.sql`) the same way `bi03` was. Role-based access (lender sees only their tenant) = pass the tenant of the logged-in role, never from the prompt |
| Data generator + loader pattern | `backend/data/generate_bi03.py` (seeded, knobs at the top), `load_bi03.py` (`--reset`, integrity checks). CSVs are git-ignored |
| API pattern | `backend/routers/bi03.py`: `X-Tenant-Id` header (demo only), lazy imports inside endpoints, and `main.py` includes the router inside try/except so a new module can never break BI-02 / BI-03 |
| Frontend | `frontend/src`: `App.jsx` routes, `components/Layout.jsx` NAV (add a third group under a divider), `api.js`, `hooks.js` (`useFetch`), `components/TimelineChart.jsx` (plain SVG line chart with markers, reusable for metric time series), `pages.css`. No chart library is installed. `vite.config.js` proxies `/bi02` and `/bi03`: add `/lc04` |
| LLM | `backend/llm/groq_client.py` (rate limiter, cache, fail-fast, logging). Groq free tier is tight (8k tokens/min, 200k/day on the big model), so keep LLM calls few |
| Tests | `backend/tests/test_bi03_*.py` style: plain scripts, `ok(name, cond)`, exit code 1 on failure, a mini tenant for write tests and cleanup |

## 7. Do not break

- `python backend/tests/check_bi02.py` (31 checks, zero LLM tokens) must stay green. Run it after touching shared files (`main.py`, `db.py`, `Layout.jsx`, `vite.config.js`). BI-02 source files are hash-guarded.
- BI-03 suites: `backend/tests/test_bi03_*.py` and `test_compliance_gate.py`. Run them **one at a time**: SQLite has a single writer (`database is locked` otherwise).
- Do not run `python backtest.py hidden` (BI-03 held-out set is spent). Do not edit `portfolioiq.db` / `bi03.db` by hand.
- Backend restart is needed after pulling new routers (the user's port 8000 may still run old code).

## 8. Lessons from BI-02 / BI-03 that apply to LC-04

- **AI for judgement and language, Python for numbers and safety.** In BI-02 an LLM-written note flipped a fact twice, so manager-facing text is templated in code. For LC-04: metric values, baselines, anomaly statistics and the drill-down arithmetic must be code; the LLM may turn a request into a dashboard spec and may choose what to investigate, but its output is **validated in code** (spec may only contain metric ids from the governed catalog, like BI-02's citations that are dropped if they reference something that does not exist).
- **Governed metrics = one registry.** Every chart number should be computed through the same definition code, and a test should recompute each displayed number from its definition and compare ("every number reconciles" is an acceptance criterion).
- **Seasonality is the false-alert trap** (day of week, salary cycle, festivals). Plan a baseline per metric per segment and a hard budget of at most 3 false alerts a day, measured on the public days.
- **Drill-down must find the segment, not restate the symptom.** The three named anomalies are segment-shaped (a city, an agency, a gateway), so a systematic scan over the dimensions (D1 attributes, D13) ranking by share of the deviation is the likely core.
- **Performance:** 100,000 accounts refreshed every 15 minutes: pre-aggregate events into 15-minute buckets per dimension instead of scanning raw events (BI-03 learned this the hard way: add indexes on foreign-key columns, deleting without them took minutes).
- **Alerts** are mock (write to a table / show in UI), two lines plus a link to the annotated view. Nothing real is sent.
- A **replay control** (simulated clock, "advance 15 minutes") worked very well in the BI-03 demo, and makes the "within the hour" claim demonstrable.
- Node: the user's npm is broken (nvm v20.11.0). Run Vite with `& "$env:APPDATA\nvm\v22.4.0\node.exe" node_modules\vite\bin\vite.js`.

## 9. Open decisions for the first LC-04 session

1. How much time is left (this decides how deep the plan goes).
2. What is D13? (see section 5)
3. Which 6 anomalies to seed (three are named: city dialer outage, agency mis-dispositioning, gateway failure; invent three more) and whether to add a few "look-alike" seasonal swings that must **not** alert.
4. Where AI sits (suggested: request to dashboard spec, choosing which dimensions to investigate, writing the two-line explanation from computed facts), and what stays pure code.
5. How big the synthetic world is (BI-03 used 6,000 accounts for development and 60,000 only for the speed test; LC-04 needs a 100,000-account refresh test).
6. Whether LC-01's metric definitions layer is built here first (about 60 metrics, shared) or kept minimal for LC-04.

## 10. If you need the original text

The first message of the old chat (`C:\Users\abhis\.claude\projects\C--Users-abhis-OneDrive-Documents-Hackathon\8a10a620-20e1-4437-9fa4-e80af5adf670.jsonl`) holds all four pasted problem statements and the judging text. The product prototype page is `C:\Users\abhis\OneDrive\Documents\AI Engineering\portfolioiq_user_view.html`. Section 3 above already has everything LC-04 needs.
