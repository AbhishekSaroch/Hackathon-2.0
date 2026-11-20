# PortfolioIQ: the story and the 7-minute script (all four modules)

**Title: "Monday, 9 a.m."** One day of a collections head, four questions, one platform.
Time: 7 minutes. Say the lines in quotes, do the "Do" lines. All data is synthetic: say it in the first minute.

## The story in one breath (open with this, 30 s)

"It is Monday, 9 a.m. Neha runs collections at a lender. Four questions land on her desk before her first coffee.
**Who is about to miss an EMI? Why is the one who already missed not paying? Is something breaking on the ops floor right now?
And the CEO wants a number by lunch.**
Today each question belongs to a different team, a different tool and a different definition of the same word.
We built one platform, PortfolioIQ, with four agents, one per question. One rule runs through all of them:
**the AI reads and reasons, code does the counting, a person approves.** Everything you will see runs on synthetic data that we generated."

## The four questions (what each module solves)

| Neha's Monday question | Module | The problem today | What we built | Proof number (synthetic data) |
|---|---|---|---|---|
| 1. Who will miss next? | **BI-03 Early warning** | Collections starts after the miss: customer already late, bureau already hit | Daily agent scores current accounts, flags the risky ones with the reason, drafts a safe nudge; a person approves | 16.9x better than random in the top 5%, median 29 days of warning |
| 2. Why is she not paying? | **BI-02 Reason finder** | Everyone late gets the same call, though "can't pay" and "won't pay" need opposite handling | Reads the calls, tests what the customer says against the bank statement, picks the next action | 29 of 30 right on an unseen holdout |
| 3. Is something breaking now? | **LC-04 Command center** | 20+ static dashboards, different definitions, an outage is seen at month end | A view that builds itself from a request, watches intraday, finds the driving segment | Hidden days: 3 of 6 official, 6 of 6 after a bug fix (say both) |
| 4. The CEO's question | **LC-01 Ask your portfolio** | A custom MIS cut takes an analyst hours or days | Plain-language question, governed metric, one read-only query, root cause, lender sees only its own data | 45 of 50 = 90% on the hidden set, 0 leaks |

## Clock

| Time | Part | Page |
|---|---|---|
| 0:00 | Story | any |
| 0:30 | 1. Before the miss (BI-03) | Early warning |
| 2:45 | 2. After the miss (BI-02) | Accounts |
| 3:40 | 3. Right now (LC-04) | Command center |
| 5:15 | 4. By lunch (LC-01) | Ask your portfolio |
| 6:20 | Close | any |

## 1. Before the miss: BI-03 (2 min 15 s)

*Bridge:* "Question one: who is about to miss? Our bank's file has just arrived with three new accounts."

1. **Do:** Early warning page (replay already at day 127, nothing loaded). Click **Download template** (shows the three CSVs), then choose the three files from `demo_csv` and press **Load into database**. "Three accounts, 311 events, in the database." (15 s)
2. **Do:** **Run analysis** (about 12 s). "The agent replays their history day by day, seeing only what was known on that day." Result: *0 of 3 flagged*. "Nothing looks wrong yet." (25 s)
3. **Do:** Press **Advance 7 days** (the first one after a load takes about a minute: talk through it). "Each day: new events in, 31 signals, a risk score, borderline accounts get fresh data, flag, draft a nudge." (60 s)
4. **Do:** Search `DEMO-`. Three flags, three different reasons: **Kavita** salary late, **Arjun** EMI late, **Divya** card use rising. Open **Kavita Nair**.
   "Her salary used to land on the 5th. This month on the 7th it had not arrived, and the EMI was due that day. The risk line crossed the threshold before she missed anything. The agent offers to move her EMI date to the 17th." Point at the chart and the nudge. "Wording comes only from approved templates, no AI writes customer text, it passes 11 compliance checks, and a person approves." (35 s)
5. **Say:** "On held-out data this finds the right accounts 16.9 times better than random, with a median 29 days of warning. Synthetic data, so it shows the method, not real-world accuracy." (10 s)

## 2. After the miss: BI-02 (55 s)

*Bridge:* "Question two: for those who already missed. Two customers, both about 40 days late. From outside they look identical."

1. **Do:** Open `/accounts/00500003933` (Priya Gupta, **Can't pay**, DPD 41) then `/accounts/00500003929` (Neha Reddy, **Won't pay**, DPD 39).
   "Same lateness, opposite problems, so opposite action: Priya gets an easy payment plan, Neha gets a senior collector. Calling both the same way is how you lose Priya and never reach Neha."
2. **Say, pointing at the reason, the evidence and "What the agent did":** "The AI reads the calls and tests what the customer *says* against the bank statement. Python does the numbers and chooses the action. When the statement is thin it asks for a fresh one."
3. **Do:** Open `/accounts/00500003923` (Mohd Reddy, **Not sure**). "If it cannot tell, it says Not sure and hands over to a human. Nothing is sent without a person's approval."
4. **Say:** "29 of 30 on an unseen holdout. We did not finish the full 300 because of free-tier limits, and we say so."

## 3. Right now: LC-04 Command center (1 min 35 s)

*Bridge:* "Question three: is anything breaking right now?"

1. **Do:** Command center as **Asha (CN leadership)**, clock at 28 Sept 00:00. Type "Which payment gateway is failing and how much money is affected?" and press **Build view**. "The AI drafts the view; code checks every metric against the 59 governed ones and the viewer's role. It cannot invent a metric." (25 s)
2. **Do:** Click the jump chip **"Oct 15, 10:45: just before a dialer outage"**, press **Advance 15 min** four times. At 11:45 the alert appears: *Contact attempts fell 70% in Hyderabad / dialer D3.* Open **Annotated view**. "Not 'calls are down', but exactly which city and which dialer, and 100% of the drop explained." (45 s)
3. **Say:** "Sundays, Dussehra and salary days swing the numbers; the detector compares like with like, so none of those raised an alert." (10 s)
4. **Do:** Switch **Viewing as** to **L1 user**. "The same incident, with only this lender's numbers. Another lender's data is refused." (15 s)

## 4. By lunch: LC-01 Ask your portfolio (1 min 05 s)

*Bridge:* "Question four: the CEO's number."

1. **Do:** Ask your portfolio, signed in as **Kaveri Finance, risk manager**. Click **"Why did total collection efficiency drop in September?"** Show the chart, then **Why**: "One agency, Bluewave, fell 72.2% to 63.2% while the rest of the book rose, and its connect rate is 27 points below everyone else's." Open **The query behind this answer**: "One read-only SELECT. The model only picked the metric; code wrote the query." (45 s)
2. **Do:** Type "What is Lakshya Capital's PAR 90?" "Refused and logged: that is another lender's data." (20 s)

## Close (40 s)

"Four questions, one rule: **AI reads and reasons, code counts, a person approves.**
Our scorecard, all on synthetic data that we graded ourselves: BI-03 16.9x lift; BI-02 29 of 30; LC-04 found 3 of 6 on the hidden days, and 6 of 6 after we fixed a bug on data we had already seen, so the second number is not a clean hold-out; LC-01 45 of 50.
Would we use it on Monday? It needs only the lender's event stream, its metric definitions and a role map."

## Before you start (5-minute checklist)

1. Restart the backend (port 8000) and the frontend. `backend/.env` must have the Groq key (LC-04 view building and LC-01 need it; never show or share it).
2. **BI-03:** open Early warning. If demo accounts are loaded press **Remove**. The replay must stand at **day 127**: from day 120 press **Advance 7 days** once (about 2 minutes) before the audience arrives. (To go back later, "Go back to day" only works for days already processed.) Files to choose live: the three CSVs in `demo_csv/`. Backup with no files: "Load the sample directly".
3. **BI-02:** run nothing live. Open the three accounts once and read each reason, then adjust the sentences to what the screen says.
4. **LC-04:** Command center, "Go to time" `2026-09-28 00:00:00`, press **Prepare replay** if it asks.
5. **LC-01:** `/ask`, Kaveri Finance risk manager, history empty. If the model is slow, restart the backend with `LC01_NO_LLM=1` (rule parser, 86% on the hidden set).
6. Do one full dry run, with a stopwatch. Check that the three demo accounts flag on days 130, 133 and 133. If one flags on another day, say what the screen shows.

## If something breaks

- Advance is slow: keep talking about the daily loop; it finishes.
- BI-03 files will not load: press "Load the sample directly" (four accounts, same idea).
- LC-04 replay stuck: type a time in "Go to time" or click a jump chip; replay is reversible.
- LC-01 rate limit: ask a question you already asked (cached), or use the rule parser.

## Questions you will get (short, honest)

| Question | Answer |
|---|---|
| Is the data real? | No, we generated all of it. The numbers show the method, and the evaluations were graded by us. |
| LC-04 found only 3 of 6? | In the official run, yes: a drill-down bug hid three incidents although the detector had alerted on all six. We fixed it without touching a threshold and re-scored: 6 of 6, 5 within the hour. We say it is not a clean hold-out. |
| Does the nudge stop defaults? | We have not proven that. We prove the right accounts get flagged. Proving the nudge needs a control group in a real pilot. |
| Where is the AI? | Reading calls (BI-02), turning a request into a view (LC-04), picking the metric from a question (LC-01). Numbers, queries, alert text, access control and actions are code. BI-03 uses no LLM on purpose. |
| How is another lender's data kept out? | At the data layer: every query is forced to carry the tenant id, the lender comes from the user's record, not from the request, and hostile queries were tested (0 leaks). |
| What is not done? | BI-02's full 300-account run, a real pilot, opt-out and Account Aggregator consent handling, a real data feed. |

**Do not say:** "real data", "6 of 6 held-out" for LC-04, "the nudge prevents defaults", or that anything was sent to a customer (everything is mock and needs approval).
