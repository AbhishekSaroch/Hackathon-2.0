# PortfolioIQ: demo pack

Everything you need on demo day: the numbers (and what to say about them), the 7-minute script, the setup checklist, and honest answers to the likely questions. All data is synthetic, and the demo says so out loud.

**One line:** collections normally starts *after* a customer misses a payment. PortfolioIQ has two agents on one loan book: **BI-02** works out *why* a late customer is not paying, **BI-03** spots current customers who are *about to* miss, and proposes a safe, helpful nudge. A person approves everything. Nothing reaches a real customer.

---

## 1. The numbers

### BI-03 (early warning), held-out period (days 91 to 150), same 5% flag budget

| | Lift in top 5% flagged | Precision (base rate 2.6%) | Missers caught | Median warning |
|---|---|---|---|---|
| **Risk model (what the agent uses)** | **16.9x** (95% range 15.0 to 18.9) | 45% | 128 of 154 (83%) | **29 days** (range 24 to 32, 25th percentile 12) |
| Hand-written rule, no learning | 6.1x | 16% | 60 of 154 | 17 days |
| Random flagging | 1.1x | 3% | 9 of 154 | 12 days |

Acceptance targets from the brief: lift >= 3x (**met**), median lead >= 21 days (**met**), strictly point-in-time (**tested**, see section 5).

Other facts you can quote:
- Daily run on **60,000 accounts takes about 5 seconds** (the brief's budget is an hour).
- Borderline accounts get fresh data first (bureau refresh or bank statement, both mock). On public data: 33 flags withdrawn as false alarms, 116 accounts caught earlier than the plain score would have, 925 confirmed; about 33 accounts pulled per day. The headline moves only a little (17.6x to 17.8x) because few accounts are borderline.
- Live replay on our 6,000-account book (days 61 to 150): about 520 flags on about 370 accounts; 157 EMI date change offers, 135 auto-pay setups, 228 reminders. All waiting for approval, none sent.

### BI-02 (why is a late customer not paying)

- Unseen holdout of 30 accounts (6 per label): **29 of 30 correct (97%)**, 0 invalid citations. Ignore the 100% on the 25 accounts we tuned prompts on.
- The full 300-account evaluation was **not completed** (Groq free-tier limits), calibration and abstention on harder cases are not fully checked. Say it before they ask.

### Say these caveats yourself (they make you more credible, not less)

1. **The data is synthetic and easier than real life.** A hand-written rule already gets 6x. The numbers show the *method*, not real-world accuracy.
2. The held-out period was evaluated **once** (logged in a ledger), but earlier we loosened the data generator after quick diagnostics on that window (first version: 0.7x lift). Say "we shaped the data, so treat 16.9x as a method demo".
3. The held-out numbers were measured **without** the borderline double-check. That step was only validated on public data.
4. We measure **whether the right accounts get flagged**. We do **not** claim the nudge prevented a miss: that needs a control group in a real pilot.
5. About 12% of defaulters give no warning at all in this data. No method catches everyone.

---

## 2. Before the demo (checklist)

1. **Restart the backend and the frontend** (the `/bi03` routes are new):
   ```powershell
   cd backend; python -m uvicorn main:app --port 8000
   cd frontend; npm run dev        # if npm is broken: & "$env:APPDATA\nvm\v22.4.0\node.exe" node_modules\vite\bin\vite.js
   ```
2. Open `http://localhost:5173/early-warning`. You should see about 5,680 accounts watched and 248 open flags.
3. **Set the stage before the audience arrives.** Type 118 in "Go back to day" and press **Go back**. It takes **about a minute** (it deletes the later days), so never do it live. Then warm up: press **Advance 1 day** once (about 15 seconds the first time, it loads all events into memory) and **Go back** to day 118 again. In the demo one Advance then takes about 5 to 10 seconds. Do not restart the backend after this, or the warm-up is lost.
4. **BI-02:** do not run the agent live (Groq quota). Open accounts that are already assessed. Pick one whose "What the agent did" shows the highlighted *Asked for a fresh bank statement* step, one "Can't pay" and one "Not sure".
5. Leave the nudge queue untouched until the script says to approve one.
6. Browser at normal zoom, window wide enough for the two-column detail page.

**After the demo (back to the full state):** press **Advance 7 days** until the date reaches day 150 (about 6 seconds a day, so roughly 3 to 4 minutes from day 118). We rehearsed exactly this on the real data: reset to day 118, one live day, then replay to day 150 gave the identical 520 flags, 520 nudges and 5,683 accounts. If you approved nudges and want a clean queue: `cd backend\agents\bi03; python nudge.py process --reset`.

---

## 3. The 7-minute script

**0:00 to 0:40, hook (Accounts page).**
"Today collections starts after the miss: the customer is already late, the bureau already knows, the relationship is already strained. We built two agents on one loan book. One works out why a late customer is not paying. The other finds customers who are about to miss, while they are still current."

**0:40 to 2:40, BI-02 (2 min).**
- Open an assessed account with the fresh-statement step. "The agent reads the call transcripts, tests what the customer *says* against the bank statement, and asks for a fresh statement when the evidence is thin." Point at the reason, the evidence with references, and "What the agent did".
- "Python does the numbers and picks the channel, the AI only reads language and weighs the claim. If it is not sure, it says *Not sure* and sends a human." Show a "Not sure" account.
- Open **To approve**: "A person approves. Nothing is sent."
- Open **Evaluation**: "29 of 30 on an unseen holdout. We have not finished the full 300 because of free-tier limits, and we say so."

**2:40 to 3:00, bridge.** "That was after the miss. Now before it."

**3:00 to 6:20, BI-03 (3 min 20).**
1. **Early warning.** "Every day the agent reads the day's new events (salary credits, EMI payments, bureau updates), only what was available that day, scores every current account, and flags the riskiest." Point at the replay panel and the cards.
2. **Live moment** (the stage is already set at day 118, 26 Aug). Press **Advance 1 day**. "This is one real day of the daily loop: events in, scores, borderline accounts get fresh data, new flags." The result line says 4 new flags and 4 nudges drafted (27 Aug: **Mayur Mehta**, Rahul Iyer: late EMI payments; Pooja Iyer, Rohit Reddy: other loan bounced). Open **Mayur Mehta**: "His EMI was due on the 23rd and he paid on the 27th, 4 days late. That is inside the grace period, so he is *not* late, but he used to pay on the day. The score jumped from 0.08 to 0.42, over the threshold. The suggested help is an auto-pay setup." *(Fallback if it is slow: talk through the loop while it runs.)*
3. **A flag in detail.** Open **Arjun Iyer** (`/early-warning/993`, flagged 10 Aug): "His salary used to come on the 1st. Then the 6th, then the 8th. The risk line climbs *while the salary has not arrived*, no future data used. The flag says why: salary arriving late." Point at the chart lanes (green dots drift), the signal bars, and "Double-checked" (if shown).
4. **The nudge.** "The agent offers a new EMI date. The words come only from an approved template, no AI writes customer text, and it must pass a compliance gate of 11 checks." Expand the gate. "Mock send, test number only."
5. **Nudges, Test the gate.** Click **Try a pushy message** then **Check message**: "Blocked on five checks: not the approved wording, dues language, amount, urgency, and it reveals the signal." Then **Use an approved message**: passes.
6. Approve one nudge (optional): "Approving re-runs the gate at send time. A message that did not pass the gate can never be sent."

**6:20 to 6:50, Risk evaluation.** "On a held-out period: 16.9 times better than random in the top 5%, a median 29 days of warning, against a target of 3 times and 21 days. A hand-written rule gets 6 times. And this is synthetic data: it shows the method, not real-world accuracy." Point at the honesty box.

**6:50 to 7:00, close.** "One loan book, two agents, a person approves everything, and we tell you exactly what we have and have not proven."

---

## 4. Likely questions and honest answers

| Question | Answer |
|---|---|
| Is this real data? | No. Everything is generated by us (seed 42). The numbers show the method, not real accuracy. We also shaped the generator's difficulty once (first version gave 0.7x), and we say so. |
| Why should we trust 16.9x? | Point-in-time features (a row is visible only from its `available_from` date), tests that truncate or poison the future and must not change the answer, and five deliberately planted leaks that the tests all catch. The held-out period was evaluated once, in a ledger. |
| Does the nudge prevent defaults? | We have not proven that. We measure whether the right accounts get flagged and we record what happened to every flag (hit, no miss, not known yet). Proving the nudge works needs a control group in a real pilot. |
| Where is the LLM in BI-03? | Nowhere on purpose. BI-03 is statistics plus tool calls (bureau refresh, bank statement pull). Customer text comes only from approved templates. The LLM is in BI-02, where the job is reading messy Hinglish calls. Cheaper, faster, safer. |
| 60,000 accounts a day? | One daily run takes about 5 seconds in our test (the brief allows an hour). |
| Is it RBI compliant? | We do not claim that. It is designed to be compatible with RBI's framework (human approval, approved wording, consent-based data) **pending compliance sign-off**. |
| Opt-out / DND? | Not in the provided data, so the gate has no such check (team decision). A real product needs it and it is one more gate check. |
| Account Aggregator consent? | Not modelled (no consent data in the brief). The statement pull is a mock. A real deployment needs consent. |
| Privacy? | In BI-02 the LLM sees computed numbers and short transcripts, not the customer name or raw statement. Groq is a third-party cloud, so real data would need an approved or self-hosted model. BI-03 sends nothing to any LLM. No protected attributes are used. |
| What if a flag is wrong? Precision is 45%. | The nudge is harmless by design: a friendly message, no amounts, no pressure, at most one per 7 days, 30-day cooldown per flag, and a person approves. |
| Why not a simple rule? | We built one: 6.1x against the model's 16.9x, shown on the evaluation page. |
| How does it improve? | Outcomes are recorded for every flag. "Suggest a better threshold" replays other thresholds on known outcomes and saves a *candidate*. A person decides whether to activate it. Nothing changes by itself. |
| 5,000 accounts in an hour (BI-02)? | Not on the Groq free tier (about 65 accounts a day on the large model). It needs a paid tier or the organisers' key, plus a rules-first shortcut for clear cases. |
| What is not done? | BI-02's full 300-account run and calibration, a real pilot with a control group, opt-out and consent handling, LC-01 and LC-04. |

---

## 5. What is tested (so you can say "we tested it")

| Test | What it proves |
|---|---|
| `check_bi02.py` (31) | BI-02 unchanged: inputs, API contract, tenant isolation, pinned results, file hashes |
| `test_compliance_gate.py` (26) | Pushy, amount, urgency, wrong-hour messages are rejected |
| `test_bi03_features.py` (24) and `test_bi03_mutations.py` | No future data in features; 5 injected leaks all caught |
| `test_bi03_model.py` | Held-out period untouched until the one logged evaluation |
| `test_bi03_runner.py` (29) | Grace period, cooldown, speed on 60k accounts |
| `test_bi03_nudge.py` (31) | Only approved wording, nothing sends without the gate |
| `test_bi03_confirm.py` (29) | Fresh-data pulls cannot see the future (a deliberately leaky pull is caught) |
| `test_bi03_outcomes.py` (41) | "Not known yet" is never counted as a miss or a success |
| `test_bi03_api.py` (57) | Endpoints, tenant isolation, approve/skip rules, replay forward and back |

Run them (one at a time, SQLite has a single writer): `cd backend\tests; python <file>`. Each exits 1 on failure.

## 6. Do not

- Run `python backtest.py hidden` again (the held-out set is spent).
- Say "RBI compliant", "reduces defaults", or quote 16.9x without saying the data is synthetic.
- Run the BI-02 batch live (rate limits).
- Start several DB-writing scripts at once (`database is locked`).

## Optional: "Try new data" moment (about 1 minute)

Early warning page, box under the replay controls. Three steps, nothing else on screen.

1. **Download template**: a zip with three CSV files already filled with four hand-made accounts. Unzip it.
2. **Choose** the three CSV files, press **Load into database** ("Loaded 4 accounts, 415 events, 68 bureau snapshots").
3. **Run analysis** (about 12 s): the agent replays the days so far over the new accounts. Then press **Advance 7 days** above and the flags appear in the table and in the main list.

Backup without files: the link "Load the sample directly". Bulk: "Bulk sample (200 accounts)" is the same three files with 200 generated accounts (flags show up for about 7 of them). **Remove** puts everything back.

Starting state for the demo: replay at day 120 or earlier, demo accounts removed or just loaded.
Say out loud: these are hand-made demo accounts on synthetic data. The accounts flagged are the ones the model reads as a pattern: DEMO-003 (day 126, other loan bounced), DEMO-001 (day 130, salary delay), DEMO-002 (day 133, late EMI). DEMO-004 never flags (one late salary is not a pattern).
Rules for edited files: account ids start with `DEMO-`, dates `YYYY-MM-DD`, no date after 2026-09-27, about a year of history per account. A wrong file is refused with a line-by-line message and nothing is loaded.
