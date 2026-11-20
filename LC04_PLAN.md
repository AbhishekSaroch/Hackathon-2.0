> **STATUS (Nov 20, 2026): all 13 steps done.** See `context.md` section 13 and `DEMO_LC04.md` for what was built and the honest hidden result. Differences from this plan: 7 practice anomalies (not 3); `lc_runner.py` (not `runner.py`, BI-03 has one); `Build a view` lives inside the Command center page; the AI is used only for the dashboard spec (the LLM-investigator idea was dropped: the drill-down is code); the weak signals (one gateway, PTP kept) are found later than an hour.

# LC-04 plan: Self-building operations command dashboard

Written Nov 19, 2026. Status: **plan only, no code yet.** Next message from user: "Haan, Step N banao".
Context: `context.md` (how the repo works), `LC04_CONTEXT.md` (brief, lessons). Product name inside PortfolioIQ: **Command Center**.

## 1. How it fits into PortfolioIQ

LC-04 is the **third module in the same repo**, built exactly like BI-03: its own database, own router, own sidebar group. It does not touch BI-02 / BI-03 tables or code.

| Layer | What we add | Existing thing we reuse |
|---|---|---|
| DB | `lc04.db` + `backend/db/schema_lc04.sql`; add `"lc04"` to `_paths()` in `db.py` | tenant-safe `query()` (refuses SQL without `:tenant_id`) |
| Data | `backend/data/generate_lc04.py` (public world), `inject_hidden_lc04.py` (hidden anomalies, own seed, detector never imports it), `load_lc04.py` | generator/loader pattern of BI-03 (seed, knobs, `--reset`, integrity checks) |
| Metrics | `backend/metrics/` : registry (~60 governed metrics) + one `compute()` function. Standalone package so **LC-01 can reuse it later** | none (new) |
| Agent | `backend/agents/lc04/`: `detect.py`, `drill.py`, `annotate.py`, `alert.py`, `spec.py` (request to dashboard), `runner.py` (replay clock) | `llm/groq_client.py` (cache, rate limit), BI-03 runner/replay idea |
| API | `backend/routers/lc04.py`, included in `main.py` inside try/except | `X-Tenant-Id` header pattern (replaced by a role header, see step 5) |
| UI | 5 pages under a new sidebar group (below the BI-03 divider): **Command center**, **Incidents**, **Build a view**, **Metric catalog**, **Evaluation** | `Layout.jsx` NAV, `api.js`, `hooks.js`, `TimelineChart.jsx`, `pages.css`; add `/lc04` to `vite.config.js` proxy |
| Tests | `backend/tests/test_lc04_*.py` plain scripts, exit 1 on failure | `check_bi02.py` must stay green (31 checks) |

Optional story link (stretch only): an incident's "affected accounts" list can deep-link to the BI-02 account page. Do not build data coupling for it.

## 2. Defaults I propose (change any of them, otherwise I assume these)

1. **D13** is not given. We define it ourselves and say so: a dimension table of **agency, city, dialer, payment gateway** (plus D1 attributes: lender, product, DPD bucket). 
2. **World size:** 10,000 accounts for development, **100,000 only for the refresh speed test**. 5 lenders (tenants). ~12 cities, 8 agencies, 3 dialers, 4 gateways.
3. **Stream:** 40 days of events (days 1-30 public, 31-40 hidden): contacts, PTPs, payments, dispositions.
4. **Pre-aggregate** events into a 15-minute cube per (lender, city, agency, dialer, gateway, product, bucket). Dashboards and detector read the cube, never raw events. Raw events are used only by the reconcile test.
5. **Metrics:** ~60 defined declaratively from ~12 base counters (rates, ratios, per-account, by channel). ~12 are "key metrics" that are monitored.
6. **AI sits in two places only:** (a) request + role to dashboard spec (validated, unknown metric ids dropped, deterministic fallback if the LLM fails); (b) optional: choose the order of dimensions to investigate. **Everything else is code:** baselines, anomaly maths, drill-down, verification, the two-line alert text (templated from computed facts, because LLM text flipped facts in BI-02).
7. **Hidden-set discipline (same as BI-03):** public days contain 3 *practice* anomalies for tuning. The 6 real hidden anomalies are injected by a separate script/seed into days 31-40. Tune on days 1-30 only, run days 31-40 **once**, write it to a ledger.

## 3. The 6 hidden anomalies and 4 decoys (to confirm)

| # | Anomaly (seeded in hidden days) | Shape the agent must find |
|---|---|---|
| 1 | **City dialer outage**: one dialer in one city stops, contacts drop | segment = city x dialer |
| 2 | **Agency mis-dispositioning**: one agency marks calls "PTP / RPC" that are not, PTP rate rises while payments stay flat | segment = agency, conversion gap (not a volume drop) |
| 3 | **Payment gateway failure**: success rate of one gateway falls | segment = gateway |
| 4 | **Lender feed stalls**: one lender's events arrive late or stop | segment = lender (staleness) |
| 5 | **PTP kept collapse** in one product / DPD bucket | segment = product x bucket |
| 6 | **Duplicate events** from one agency (double counted contacts) | segment = agency, volume spike |

**Decoys that must NOT alert:** Sunday dip, salary-day (1st-5th) payment spike, month-end push, one festival-day dip (public days have these too, so we tune against them).

Brief says 3 anomalies are named; #4 to #6 are our own invention. Say so in the demo.

## 4. Steps (each has a "done when"; vertical slice first, polish last)

| # | Step | Done when | Size |
|---|---|---|---|
| 0 | Safety: run `check_bi02.py`, confirm defaults above | 31 checks pass, user confirms section 2 and 3 | S |
| 1 | Schema `schema_lc04.sql` + `db.py` `"lc04"` path. Tables: `lc_accounts` (D1 + D13 columns, lender = tenant), `events`, `cube_15m` (indexed), `metric_defs`, `roles`, `anomalies`, `annotations`, `alerts`, `dashboards`, `replay_state`. Indexes on every FK column (BI-03 lesson) | `python db.py lc04` creates empty tables, BI-02 check passes | S |
| 2 | Generator `generate_lc04.py`: accounts, 40 days of events with real seasonality (time of day, day of week, salary cycle, festival) + 3 practice anomalies + decoys. Separate `inject_hidden_lc04.py` (own seed) writes the 6 hidden anomalies and an answer key the detector never reads | integrity checks pass; a **naive fixed-threshold detector** gives many false alerts per day and misses some (proves seasonality is the real problem and the data is not trivial) | M |
| 3 | Loader + cube builder `load_lc04.py`. Build 15-min cube from events | row counts match; cube totals equal raw event totals; **100k-account refresh of one 15-min bucket under a few seconds** | M |
| 4 | Governed metric registry + single `compute(metric, scope, group_by, window)` | ~60 metrics defined; **reconcile test** recomputes every displayed metric from raw events with independent SQL and matches exactly (acceptance: "every number reconciles") | M |
| 5 | Roles + scope enforced at data layer. 5 roles: CN leadership, Ops head, Delivery head, Lender RM (a set of lenders), Lender user (1 lender). Role decides allowed metrics, dimensions and lender scope; scope comes from the logged-in role, never from the prompt | tests: lender user cannot get another lender's rows by any endpoint, any group_by, or a prompt like "show me lender B"; unscoped query is refused | M |
| 6 | Detector: baseline per (metric, segment, 15-min slot) = same slot and weekday from previous weeks (median/MAD) with a salary-cycle adjustment; fire only on persistence (k consecutive buckets) plus a volume floor; one incident per root cause (not 10 alerts for one outage). Tuned on days 1-30 only | on public days: practice anomalies detected within 60 min, **at most 3 false alerts/day**, decoys silent | L |
| 7 | Drill-down + verification: scan every dimension, rank by share of the deviation (explanatory power), test pairs (city x dialer) so a parent segment does not hide the real one, then **verify by counterfactual** (remove the segment: does the metric return to baseline?). Writes an `annotation` with segment, size of effect, evidence numbers | correct segment on all practice anomalies, including a confounded case (agency concentrated in one city); wrong parent segment is rejected | L |
| 8 | Alerts + runner with replay clock: `runner.py advance --minutes 15`. Alert = two templated lines + link to the annotated view, stored in `alerts` (mock send) | replay public days: alert time minus anomaly start is under 60 min; advancing and resetting gives identical results | M |
| 9 | Dashboard spec generator `spec.py`: role + plain-language request to a spec (metrics, charts, filters). LLM output validated in code (metric ids must be in the role's allowed catalog, otherwise dropped); deterministic fallback template per role; "rebuild around finding" adds the incident's metric and segment filter to the spec | 10 public requests produce valid specs, each under 2 minutes (target: seconds, cached), zero ungoverned metrics, lender user spec never contains other lenders | M |
| 10 | API `routers/lc04.py`: `/lc04/me`, `/dashboards/generate`, `/dashboards/{id}/data` (refresh), `/metrics`, `/incidents`, `/incidents/{id}`, `/alerts`, `/replay/advance`, `/replay/reset`, `/evaluation`. Included in `main.py` in try/except | API test on a mini tenant: all endpoints, isolation, hidden days not visible past the replay pointer | M |
| 11 | UI: **Command center** (role switcher, spec renderer: KPI cards + `TimelineChart` series + segment table, auto-refresh every 15 min, replay panel "advance 15 min", annotations pinned on the charts), **Incidents** (list, detail with drill-down path and counterfactual), **Build a view** (type a request, see the spec and the governed metrics used), **Metric catalog** (definition, owner, reconcile status), **Evaluation** | clicked through on live data; BI-02 / BI-03 pages unchanged; sidebar gets a third group | L |
| 12 | Hidden run **once** (days 31-40) logged in `data/lc04_hidden_eval_ledger.json`; scorecard (detected x of 6, time to detect, correct segment, false alerts/day, reconcile, isolation); 100k speed test; `DEMO_LC04.md` (script, numbers, caveats, Q&A) | all four acceptance criteria measured and reported honestly, whatever the result | M |

**Fast mode** (as in BI-03 steps 10-12): run only the suites a change affects, one full run at the end. No commits per step.

## 5. If time runs short: what to cut, in order

1. Keep steps 1-8 + a minimal UI (11) always: that is "detect, find the segment, alert, show annotated view" and the whole acceptance test.
2. Cut next: LLM spec generation (9) falls back to deterministic role templates (still governed, still a valid spec).
3. Cut next: metrics shrink from 60 to ~30 (say so openly), Evaluation page becomes the JSON report.
4. Never cut: the reconcile test, tenant isolation tests, the decoy (seasonality) tests, the single hidden run. These are the credibility.

## 6. Demo story (7 minutes, draft)

1. Pick a role (Ops head), type "show me what needs my attention today": dashboard builds itself (spec shown, only governed metrics).
2. Press "Advance 15 min" until the Pune dialer outage starts: incident appears within the hour, drill-down shows city x dialer, counterfactual proves it, chart annotated, two-line alert with link.
3. Show a Sunday dip and a salary-day spike that did **not** alert (seasonality).
4. Switch to Lender user: same view, only their data; try to ask for another lender, refused at the data layer.
5. Scorecard: hidden 10 days, x of 6 found, false alerts per day, reconcile 100%. Say clearly: synthetic data, anomalies seeded by us.

## 7. Risks

- **Reaching 5 of 6 within an hour with no more than 3 false alerts/day**: the two pull against each other. Mitigation: persistence + volume floor + one incident per root cause; tune only on public days.
- **Our own generator decides the difficulty.** Too easy proves nothing (BI-03 first version was too hard, 0.7x). Keep decoys and one confounded segment.
- **Time:** unknown, see open question below.
- Groq free tier is tight: LLM only in step 9, cached; the rest costs zero tokens.

## 8. Open questions for the user

1. How many hours are left before Fri 20 Nov 5:30 pm? (decides whether we do all 12 steps or the cut list in section 5)
2. OK with the defaults in section 2 (D13 defined by us, 10k dev / 100k speed test, 5 lenders)?
3. OK with the 6 anomalies and 4 decoys in section 3, or do you want to swap any?
