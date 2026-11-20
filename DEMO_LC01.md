# LC-01 demo pack: Ask-your-portfolio agent

Written Nov 20, 2026 after the single hidden run. Product name inside PortfolioIQ: **Ask your portfolio** (sidebar group 4, below the Command center icons).
All data is synthetic, and the questions, answer keys and planted problems are ours. Say that first.

## 1. The numbers (honest, with what to say out loud)

| Brief's criterion | Target | Public 20 (tuned on) | Hidden 50, **run once on frozen code** |
|---|---|---|---|
| Correct answers | at least 85% | model 20 of 20, rule-based parser only 19 of 20 (not a clean number, we tuned on these) | **45 of 50 = 90%** (value 10/12, rank 7/8, trend 6/6, root cause 4/6, clarify 8/8, refuse 10/10). Rule-based parser alone: 43 of 50 = 86% |
| Clarifying question on every ambiguous one | every one | 3 of 3 | **8 of 8**, and 0 false clarifications on answerable questions |
| Cross-lender leaks | zero | 0 | **0** (checked on every response and on every SQL statement that ran) |
| Every number reconciles to its governed definition | all | 304 comparisons of all 38 metrics against an independent pandas calculation, 0 mismatches | same |
| Answer time | under 60 s typical | median 0.1 s (cached), 1.6 s uncached | median 1.7 s, max 57 s (the free Groq rate limit making one call wait, not the system) |
| Tenant scoping at the data layer | yes | 29 adversarial SQL statements refused, forged users refused | same |
| The query behind every answer is shown | yes | yes | yes |

**The 5 wrong hidden answers (say them, do not hide them).**
* H04 (client success manager): "across all three lenders, how many disputes" was refused as cross-lender by the model even though that user may see all three. A false refusal: safe, but wrong.
* H07: the model put "month" in the wrong place and dropped "June"; the system said it could not split by month. (Afterwards we made code treat "month" as the time axis; that does not fix this question and is not part of the official score.)
* H19: used the promise *count* instead of the promise *kept rate*.
* H30: the key assumed PAR 30 for Meridian rose from January to October. It did not (3.5% to 3.5%); the agent reported the flat trend correctly but did not explain a change that is not there. The key was our mistake about our own data, but we count it as wrong.
* H31: "why did disputes spike" was mapped to "dispute rate on connect" instead of "disputes raised".

The ledger (`backend/data/lc01_hidden_eval_ledger.json`) stores the code fingerprint of the run. `lc01_intent.py` got the small "month" fix after the run, so the fingerprint no longer matches today's file; the 45/50 is what the frozen code did.

## 2. What to say it is (one paragraph)

A lender types a question. **The model only decides what is being asked** (a metric id, dimensions, months, "why?"). It never writes SQL and never writes a sentence of the answer.
Code checks that against 38 governed metrics and the user's lender scope, builds one read-only SELECT from the definition, runs it through the tenant-safe data layer, and writes the answer, the chart and the root cause from the computed numbers. The rules that matter (an everyday word with several definitions means ask, a request for another lender means refuse and log) are in code, before and after the model, so they hold even when the model is wrong.

## 3. The 7-minute script

Setup: backend and frontend running (section 4), browser on `http://localhost:5173/ask`, "Signed in as" Kaveri Finance · risk manager. The history panel is empty.

1. **0:00 Problem (30 s).** Risk and collections teams ask for custom MIS cuts every week; each takes an analyst hours to days. Static dashboards answer last quarter's question.
2. **0:30 A plain question (60 s).** Click "What was our NACH bounce rate last month?". Open "The query behind this answer": one read-only SELECT, filtered on the tenant id, parameters shown. Say: the model picked the metric, code wrote the query.
3. **1:30 Root cause (90 s).** Click "Why did total collection efficiency drop in September?". Show the line chart with the two compared months marked, then "Why": agency Bluewave Collections moved the total by -3.0 pts, its collection efficiency 72.2% to 63.2% while the rest of the book rose, and the evidence line: its connect rate is 27 pts below everyone else's. Say the agency problem was planted by us and the agent found it with a counterfactual ("if this agency had stayed as in August").
4. **3:00 It asks, it does not guess (60 s).** Click "What is our collection efficiency?". It shows four governed definitions (total, current EMI, by count, arrears recovery). Click one, get the answer. Open Metric definitions to show the overlapping groups. Say: this is the brief's "overlapping definitions" trap, and the rule is enforced in code even if the model picks one.
5. **4:00 Refusals are logged (60 s).** Type "What is Lakshya Capital's PAR 90?": refused, and it says it was written to the audit log. Type `SELECT * FROM loans WHERE tenant_id = 'L2'`: refused. Type "Delete the DPD records for Pune": refused. Say: these are caught in code before any model sees them; a second wall is the data layer, which binds the tenant id itself and a SQL checker that allows only `tenant_id = :tenant_id`.
6. **5:00 Another role (45 s).** Switch to "CN client success manager (all 3 lenders)". Ask "Compare PAR 90 across lenders, rank them": three lenders side by side, combined totals computed as sum of numerators over sum of denominators, not an average of ratios. Switch to "(L2, L3)" and ask about Kaveri Finance: refused.
7. **5:45 Scorecard (75 s).** Ask evaluation page: public, hidden 45 of 50 = 90%, 8 of 8 clarifications, 10 of 10 refusals, 0 leaks, and the five wrong ones. Say the caveats: synthetic data, our own questions, hidden set run once but self-graded, trimmed warehouse (11 tables, 38 metrics). Close: "Would we use it on Monday: it needs only a warehouse with a tenant column and a metric definitions layer."

If the model is slow or the rate limit bites, ask a question you already asked (answers from the model are cached), or set `LC01_NO_LLM=1` before starting the backend to run the rule-based parser only (86% on the hidden set).

## 4. Setup checklist

```powershell
# one-time (already done): build and load the synthetic warehouse
cd backend\data; python generate_lc01.py; python load_lc01.py --reset
# terminal 1: backend (restart it after pulling, /lc01 is new)
cd backend; python -m uvicorn main:app --port 8000
# terminal 2: frontend
cd frontend; npm run dev        # if npm fails: & "$env:APPDATA\nvm\v22.4.0\node.exe" node_modules\vite\bin\vite.js
```
* The model needs `backend/.env` with the Groq key (never share it). Without it, or when rate-limited, the rule-based parser answers and the answer card says so.
* **Do not** run `python backend/agents/lc01/lc01_evaluate.py final` again (it refuses) and do not delete `lc01_hidden_eval_ledger.json`. `load_lc01.py --reset` also empties the question history, which is fine.
* Tests (one at a time): `python backend/tests/test_lc01_data.py`, `test_lc01_access.py`, `test_lc01_agent.py`, `test_lc01_api.py`, plus `check_bi02.py`. All run on a copy of the database and use no tokens.

## 5. Likely questions, honest answers

* **Does the AI write the SQL?** No. It returns a structured intent; code validates it against the governed definitions and builds the SQL from registry fragments with bound parameters. A SQL checker also refuses anything but a single read-only SELECT over whitelisted tables whose only tenant condition is `tenant_id = :tenant_id` (no OR, NOT, UNION, comments).
* **What if the model is wrong?** The overlap rule, the "who is worst with no measure named" rule, month validation, ranking direction and all refusals are code. A wrong model pick can still give a well-formed answer to the wrong question (H19, H31); the answer card names the metric and its definition so the user can see it.
* **How do you stop one lender seeing another's data?** Four layers: the lender scope comes from the user record (a forged user is refused); code refuses questions that name another lender, a benchmark or "all lenders"; the compiler refuses a lender outside the scope; the data layer binds the tenant id and refuses an unscoped query. Tested with 29 hostile statements and 0 leaks in the hidden run.
* **Why trust a root cause?** It is arithmetic: for each dimension and segment, "how much of the change disappears if this segment had stayed as last month". The top segment, a second level inside it, and related metrics (connect rate, promises kept, NACH bounces) for that segment are shown. The text is a template. It can name a segment that explains most of a *tiny* change, so a change under 0.2 points is reported as noise.
* **Why only 38 metrics and 11 tables?** Time. The design (registry, compiler, scope) does not depend on the count; more tables mean more registry entries.
* **Real data?** A lender would connect its warehouse and its metric definitions. Groq is a third-party cloud, so real customer data needs an approved or self-hosted model; only aggregates and metric ids go to the model here, never rows.

## 6. Ask over BI-02, BI-03 and LC-04 data (added after the hidden run; not part of the 45/50)

Setup: `python backend/data/sync_all.py` once (about 15 s, makes `backend/ask_all.db`), restart the backend. The app now offers ONE user, **Neha Kapoor · Collections Head**, who sees everything integrated: the three lenders of the lender warehouse, BI-02 and BI-03 (ABC Bank) and the LC-04 command center. (The other users stay in the code for the access tests and the evaluation, but are not offered in the app.)

1. As Neha click "How many accounts have not paid because they forgot?": **13**, the same 13 as the BI-02 Approve screen. Then type the Hinglish version: "kitne accounts paise nahi de rahe aur forgot ke andar hain?": same 13. Open "The query behind this answer": one SELECT on `bi02_assessments`, latest assessment per account, tenant `abc_bank`.
2. "Kitne accounts paise nahi de rahe, reason ke hisaab se?": bar chart by reason (60 assessed accounts, each once).
3. "How many early-warning flags do we have, by signal?" (BI-03), "What is the flag hit rate?" (24.8%, 51 of 206).
4. "How many anomalies has the command center detected, by metric?" (LC-04). Words both datasets define ("promise kept rate", "outstanding") stay with the lender warehouse unless she says BI-02.
5. Say: the data is a read-only copy, with a "Refresh data copy" button; the three modules' own databases are never changed; answer keys and contact logs are not in the copy.

Honest limits: snapshot until refreshed; only assessed accounts have a reason (60 of 300); command-center lender ids L1 to L5 are a different dataset from the three lenders of the lender warehouse; these 14 extra metrics are routed by rules (no model) and checked against independent calculations, but they have no hidden-set score.
