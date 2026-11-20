# LC-04 demo pack: Self-building operations command dashboard

Written Nov 20, 2026 after the single hidden run. Product name inside PortfolioIQ: **Command center** (sidebar group 3, below the BI-03 icons).
All data is synthetic and the anomalies were seeded by us. Say that first.

## 1. The numbers (honest, with what to say out loud)

| Brief's criterion | Target | Public days (tuned on) | Hidden days, **official run** (frozen code, run once) | Hidden days, **re-scored after a bug fix** |
|---|---|---|---|---|
| Seeded anomalies found within an hour with the correct driving segment | at least 5 of 6 | as tuned: 5 of 7 within the hour, all 7 alerted, all 7 right driver. With the fixed code: 6 of 7 alerted (the weak PTP-kept one is detected but stays "watched", z -3.98 against the line of 4.0), 5 within the hour, all right | **3 of 6** (3 found: Pune dialer 60 min, agency A3 45 min, gateway G2 30 min) | **5 of 6** within the hour; 6 of 6 alerted, 6 of 6 right driver (the PTP collapse took 315 min) |
| False alerts per day (alerts that went out for something that was not seeded) | at most 3 | worst day 2, mean 0.26 (6 in 23 days) | worst day 1, mean 0.1 (1 in 10 days) | worst day 1, mean 0.2 (2 in 10 days) |
| Every number reconciles to its governed definition | all | 59 metrics x 6 views = raw events, 0 wrong; every finding equals `compute()` | re-run: 0 failures | same |
| Zero cross-tenant data | none | 35 access / attack checks | 35 checks, 168 lender alert rows, 0 leaks | same |
| Dashboard generated in under 2 minutes | < 120 s | 10 of 10 requests good, milliseconds without AI | 7 of 10 good, slowest 60 s (the free Groq limit, not the system); every spec 100% governed | same |
| Refresh every 15 minutes over 100,000 accounts | 15 min | busiest bucket for all 5 lenders in 486 ms (measured at step 3) | not re-measured | |

**The hidden result in one honest paragraph (say this, do not hide it).** We ran the hidden 10 days once, with the code fingerprinted
before. It found 3 of 6: the drill-down had a bug (it left out of its history every day on which *any* series had an alert, and when fewer
than 5 days were left it raised an error that the timeline swallowed, silently dropping the incident). The detector itself had raised alerts for
the 3 missed anomalies. We fixed the bug (no threshold touched) and re-scored the same data: 6 of 6 found, 5 within the hour, all drivers right.
The second number is **not** a clean hold-out because the fix came after seeing the result. Both are in the Scorecard page and in the ledger
(`backend/data/lc04_hidden_eval_ledger.json`). The first number is what the frozen system did; the second is what the fixed system does.

**Slow ones are a data limit, not a threshold.** A segment with few events an hour (one payment gateway, PTPs kept in one lender and product)
cannot be told from noise in the first hour: gateway G4 (practice) 105 min, PTP collapse 285 min (practice) and 315 min (hidden). Lowering
thresholds did not help in the public grid (it only added false alerts).

## 2. The 7-minute script

Setup (before you start): backend and frontend running (section 3), browser on `http://localhost:5173/command` as "Asha (CN leadership)".
Clock at 28 Sept 00:00. The replay timeline is already prepared (button says "Advance 15 min", not "Prepare replay").

1. **0:00 The problem (30 s).** 20+ static dashboards, definitions differ, an outage shows up at month end. We built a view that builds itself and watches intraday.
2. **0:30 A view builds itself (60 s).** Command center shows the leadership default view. Type "Which payment gateway is failing and how much money is affected?" and press Build view. Point at the badge ("AI draft, checked"), the notes list and "built in ... ms". Say: the AI drafts, code validates every metric against the 59 governed ones and the role.
3. **1:30 Go live (90 s).** Click "Oct 15, 10:45: just before a dialer outage". Click "Advance 15 min" four times. At 11:45 a green line appears: *Contact attempts fell 70% in Hyderabad / dialer D3 since 10:45 (24 against 80 expected).* Open "Annotated view": driving segment chips, 100% of the deviation explained, counterfactual "rest back to normal", the agent's step-by-step trace.
4. **3:00 It is not an alert on the symptom (60 s).** On the incident page show the candidate table: the city dropped 36%, the city x dialer pair 90%; the agent named the pair. Say: a parent dilutes the effect, a tiny slice is luck, the agent needs the difference to be decisive.
5. **4:00 The view rebuilds around the finding (45 s).** Click "Rebuild my view around this": the driver and its context come first, then the rest.
6. **4:45 Seasonality is not an anomaly (45 s).** Type `2026-10-20 08:00:00` in "Go to time" (Dussehra, and Kolkata's Durga Puja) and press "Advance 1 hour" a few times: no alert. Say: no alert was sent on any of 3 Sundays, Sunday 1 Nov (also a salary day), Dussehra, Dhanteras or the month-end Saturday: Sundays run at 35%, salary days double payments, Kolkata has a festival; the detector compares like with like and reads a calendar for regional festivals.
7. **5:30 A lender sees only their own (45 s).** Switch "Viewing as" to "L1 user": the same incident reads "9 against 29 expected" (their numbers, not the platform's), no clock controls, no other lender named. Ask for "lender L2's collections": refused with a note.
8. **6:15 The scorecard (45 s).** Scorecard page: public, official hidden (3 of 6) and after the fix (6 of 6). Say the honest paragraph above in two sentences. Close with: "Would we use it on Monday: it needs only an event stream, the metric definitions and the role map."

If something goes wrong live, the replay is reversible: type a time in "Go to time" or click a jump chip.

## 3. Setup checklist

```powershell
# terminal 1: backend (restart it after pulling, /lc04 is new)
cd backend; python -m uvicorn main:app --port 8000
# terminal 2: frontend
cd frontend; npm run dev        # if npm fails: & "$env:APPDATA\nvm\v22.4.0\node.exe" node_modules\vite\bin\vite.js
```
* First `/lc04` call after a backend start loads the replay timeline (a few seconds). If the page says "Prepare replay", press it once (about a minute).
* The replay clock lives in `lc04.db`. To start the demo from the beginning: Command center > "Go to time" > `2026-09-28 00:00:00`.
* Data is rebuilt (not needed unless the files are gone): `python backend/data/generate_lc04.py`, `python backend/data/inject_hidden_lc04.py`, `python backend/data/load_lc04.py --reset`, `python backend/metrics/roles.py`, `python backend/agents/lc04/request_set.py`, `python backend/agents/lc04/lc_runner.py prepare`. **Do not** run these after the hidden run unless you accept losing the hidden load: `load_lc04.py --reset` removes the hidden days and `lc_evaluate.py final` refuses to run a second time.
* The AI needs `backend/.env` with the Groq key (never share it). Without it every view is built by the deterministic fallback and says so.

## 4. Likely questions, honest answers

* **How do you avoid alert fatigue?** Seasonality is removed by comparing each segment's share (or rate) with the same clock window on previous days and the day's level across segments, with a calendar for regional festivals (and the PTP outcomes that lag them). Thresholds are per window (30 min to 4 h) and scaled by how many series a metric has. An alert needs the drill-down to confirm a driving segment and the rest to be normal; unconfirmed incidents are only "watched". On public days: worst day 2 false alerts, 0.26 a day.
* **Where is the AI?** Only in turning a plain request into a dashboard spec; its output is validated in code (ungoverned metrics, other lenders, illegal dimensions, ad hoc formulas, HTML, too many panels are all removed and explained). Detection, drill-down, numbers, alert text and access control are code, because a wrong fact in an alert is the worst outcome (in BI-02 an LLM-written note flipped a fact twice).
* **Can the agent invent a metric?** No. `compute()` takes a metric id from a registry of 59; there is no parameter for a formula; the spec validator refuses "create a metric ...". Tested with a hostile model.
* **How is zero cross-tenant data enforced?** At the data layer: every query goes through `db.py`, which refuses SQL without the tenant placeholder; a user's lenders come from their record, not from a request; a forged or edited principal is rejected on use (re-read from the database); four mutations of the access code were each caught by the tests.
* **Why did the hidden run find only 3 of 6?** A drill-down bug (section 1). We report both numbers. The detector raised alerts for all 6.
* **Why is the PTP collapse slow?** About 8 kept promises an hour in that lender and product: nothing is significant in the first hour.
* **100,000 accounts every 15 minutes?** The cube pre-aggregates events per 15 minutes and segment. On a 100k-account test world the busiest bucket refreshed in 486 ms for all lenders; a day of one lender's city series reads in 81 ms. The dev data is 10,000 accounts; the 100k run used 3 days of events.
* **Is "since 10:45" exact?** No: the onset is estimated to about one 15-minute bucket (the outage started at 11:00).
* **Real data?** The brief gives none, so everything is ours: generator, 59 metrics, 5 roles, the 6 hidden anomalies (own seed, separate script). Real data would need the real event stream and metric layer.

## 5. What is tested (all plain scripts, `python backend/tests/<name>.py`, exit code 1 on failure)

`test_lc04_schema` (9), `test_lc04_data` (data, seasonality, anomalies are really in the stream, a naive detector is not enough), `test_lc04_load` (DB = CSV, cube = independent recompute, point-in-time, idempotent), `test_lc04_speed` (100k accounts, ~5 min), `test_lc04_metrics` (59 metrics x 6 views = raw events; mutation-checked), `test_lc04_access` (isolation and attacks; four mutations caught), `test_lc04_detect` (recall, false alerts, decoys, point-in-time by poisoning the future), `test_lc04_drill` (driver and traps, counterfactual, numbers = `compute()`), `test_lc04_runner` (alerts, replay is reversible, lender isolation of alert text), `test_lc04_spec` (hostile model, fallback, rebuild, render), `test_lc04_api` (all endpoints, roles). Run them one at a time (SQLite has one writer). `check_bi02.py` must stay at 31 passed.

## 6. Do not

* Do not run `lc_evaluate.py final` again (it refuses) and do not edit `anomaly_key_hidden` or the hidden CSVs.
* Do not claim 6 of 6 as a held-out result. Say 3 of 6 official, 6 of 6 after a bug fix on data we had already seen.
* Do not call alerts "sent": they are rows; nothing leaves the system.

## 7. Bonus scenario: legal notices sent outside the 08:00-20:00 window (Nov 20, 06:17)

Added after the scorecard. It is a **demo scenario seeded by us, not part of any evaluation** (it is logged in the ledger as `demo_legal_seeded`).
The story: the simulated "now" is 2026-11-20 06:17. At about 03:00 one agency (A4) sent 38 legal notices to customers of lenders L2 and L3, outside the permitted
08:00-20:00 window. The sender's batch log reached the platform between 06:05 and 06:14, so the platform learns of it at 06:15, three hours late.

* **Why it is a rule and not the statistical detector.** At night the normal number of legal notices is zero, so there is no baseline to compare with: zero tolerance is the only
  sensible test. The rule reads the governed metric `legal_notices_out_of_window` (judged on the SEND time), groups buckets within an hour into one incident, finds the driving
  sender (a dimension value holding at least 80% of the notices) and writes two templated lines and a link, like every other alert.
* **Demo (60 s).** Command center > "Nov 20, 06:00: before the sender's log arrives": no alert. Press "Advance 15 min": the alert appears at 06:15, *38 legal notices were sent at 03:02-03:18,
  outside the permitted 08:00-20:00 window (logged 06:05-06:13). Driver: agency A4 (100% of them). Lenders affected: L2, L3.* Open the annotated view: the rule, who sent them, the trace.
  Switch "Viewing as" to the L2 user: *19 legal notices to your customers ...* (their own count). The Delivery head hears nothing (compliance metrics are not in that role); lenders L1, L4, L5 are not affected.
* **Numbers.** 135 normal notices on Nov 17-19 (all inside the window), 38 outside the window on Nov 20 (19 + 19), one incident, found at 06:15.
* **Remove / re-seed.** `python backend/data/seed_demo_legal.py --remove` (removes exactly the 173 legal events and their cube rows) and `python backend/data/seed_demo_legal.py` (the same scenario again); the schema change is in `backend/db/migrate_lc04_legal.py`.
* **Honest limits.** One scenario with one sender; the window (08:00-20:00) is a constant in `cube.py`; the platform finds out only when the log arrives (a real-time feed would find it at 03:xx); the rule alerts, it does not block sending.
