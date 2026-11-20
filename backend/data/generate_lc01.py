"""
LC-01 synthetic warehouse generator (seed 7). Everything here is OURS: the organisers give no data, so the numbers prove
the method, not real-world accuracy.

    python backend/data/generate_lc01.py          -> backend/data/csv_lc01/*.csv  (git-ignored)  +  lc01_world_truth.json

World: 3 lenders (L1..L3 = tenants), 6,800 loans, 10 months of performance (2026-01 .. 2026-10; eight warm-up months are
simulated and thrown away so the opening state is realistic). Loans follow a small DPD state machine; calls, promises to pay
and payments are produced by the same simulation, so the numbers hang together (a kept promise cures a loan, a NACH bounce
makes a miss more likely, a weak agency cures fewer loans).

Planted stories (the root-cause answer keys; written to lc01_world_truth.json, never read by the agent):
  S1  L1  from 2026-08  agency "Bluewave Collections": contact rate collapses, more loans slip, fewer cure -> collection efficiency drops
  S2  L2  2026-09 / 10  two-wheeler loans in the East region: NACH bounces spike -> new delinquency jumps
  S3  L3  Chennai, creeping up all year: delinquency rises month after month
  S4  L2  agency "Fortis Collections": keeps far fewer promises to pay, all year
  S5  all, October: a mild festive dip in new slips (x0.85). It is within noise, so NO question key depends on it
  S6  L3  2026-07 gold loans: dispute spike, mostly "fraud"
Lender quirks (lender_config): L2 starts counting DPD after a 5-day grace; L3 treats gold loans as NPA after 120 days.
"""
import json
import math
import random
from pathlib import Path

import pandas as pd

SEED = 7
HERE = Path(__file__).resolve().parent
OUT = HERE / "csv_lc01"
SIM_MONTHS = [f"2025-{m:02d}" for m in range(5, 13)] + [f"2026-{m:02d}" for m in range(1, 11)]
KEEP_FROM = "2026-01"

REGION = {"Delhi": "North", "Jaipur": "North", "Lucknow": "North", "Mumbai": "West", "Pune": "West", "Ahmedabad": "West",
          "Bengaluru": "South", "Chennai": "South", "Hyderabad": "South", "Kolkata": "East", "Patna": "East"}
PRODUCT_AMT = {"personal_loan": (80_000, 600_000), "two_wheeler": (50_000, 150_000), "business_loan": (300_000, 2_500_000),
               "consumer_durable": (15_000, 80_000), "gold_loan": (50_000, 400_000)}
PRODUCT_RISK = {"personal_loan": 1.3, "business_loan": 1.2, "two_wheeler": 1.0, "consumer_durable": 1.1, "gold_loan": 0.6}

TENANTS = {
    "L1": dict(name="Kaveri Finance", n=3000, grace=0, npa=90,
               note="Counts DPD from the due date (no grace period).",
               cities=["Delhi", "Jaipur", "Lucknow", "Mumbai", "Pune", "Ahmedabad", "Bengaluru", "Chennai", "Hyderabad", "Kolkata", "Patna"],
               city_w=[10, 5, 4, 16, 9, 6, 12, 7, 8, 6, 4],
               products=["personal_loan", "two_wheeler", "business_loan", "consumer_durable"], prod_w=[35, 25, 15, 25],
               agencies=[("Apex Recovery", "tele", .28), ("Bluewave Collections", "hybrid", .30),
                         ("Crestline Services", "tele", .22), ("Delta Field Force", "field", .20)]),
    "L2": dict(name="Lakshya Capital", n=2200, grace=5, npa=90,
               note="Starts counting DPD only after a 5-day grace period, so its early-bucket numbers look better than a no-grace lender's.",
               cities=["Mumbai", "Pune", "Ahmedabad", "Kolkata", "Patna", "Delhi", "Jaipur"],
               city_w=[14, 8, 8, 22, 14, 8, 6],
               products=["personal_loan", "two_wheeler", "business_loan"], prod_w=[32, 42, 26],
               agencies=[("Eastgate Recovery", "hybrid", .25), ("Fortis Collections", "tele", .20), ("Granite Services", "tele", .20),
                         ("Harbor Recovery", "field", .20), ("Ionic Field Force", "field", .15)]),
    "L3": dict(name="Meridian Credit", n=1600, grace=0, npa=90,
               note="Treats gold loans as NPA only after 120 days; the shared NPA metric still uses 90 days so lenders stay comparable.",
               cities=["Bengaluru", "Chennai", "Hyderabad", "Mumbai", "Pune"], city_w=[26, 24, 22, 16, 12],
               products=["gold_loan", "two_wheeler", "consumer_durable"], prod_w=[45, 30, 25],
               agencies=[("Jade Collections", "tele", .40), ("Kestrel Recovery", "hybrid", .35), ("Lumen Services", "field", .25)]),
}
CALLS_BY_BUCKET = {"1-30": 3.0, "31-60": 4.0, "61-90": 5.0, "90+": 3.0}
CURE_P = {"1-30": 0.50, "31-60": 0.34, "61-90": 0.20, "90+": 0.10}
KEPT_B = {"1-30": 1.0, "31-60": 0.9, "61-90": 0.8, "90+": 0.6}
DISPUTE_REASONS = ["wrong_amount", "paid_already", "harassment", "fraud", "other"]


def bucket(d: int) -> str:
    return "current" if d <= 0 else "1-30" if d <= 30 else "31-60" if d <= 60 else "61-90" if d <= 90 else "90+"


def month_idx(m: str) -> int:
    return (int(m[:4]) - 2026) * 12 + int(m[5:7]) - 1


def effects(t, m, L, ag):
    e = dict(p_new=1.0, connect=1.0, cure=1.0, kept=1.0, bounce=0.04, dispute=1.0)
    k = month_idx(m)
    if m == "2026-10":
        e["p_new"] *= 0.85                                                       # S5 festive month
    if m == "2026-01":
        e["p_new"] *= 1.10                                                       # post-festive pinch
    if t == "L1" and ag == "Bluewave Collections" and m >= "2026-08":            # S1
        e["p_new"] *= 2.2; e["connect"] *= 0.45; e["cure"] *= 0.5
    if t == "L2" and L["product"] == "two_wheeler" and L["region"] == "East":    # S2
        if m == "2026-09":
            e["bounce"] = 0.45
        elif m == "2026-10":
            e["bounce"] = 0.20
    if t == "L3" and L["city"] == "Chennai" and k >= 1:                          # S3
        e["p_new"] *= 1 + 3.5 * k / 9
    if t == "L2" and ag == "Fortis Collections":                                 # S4
        e["kept"] *= 0.5
    if t == "L3" and L["product"] == "gold_loan" and m == "2026-07":             # S6
        e["dispute"] = 20.0
    return e


def emi_of(p, n, rate=0.16):
    r = rate / 12
    return p * r * (1 + r) ** n / ((1 + r) ** n - 1)


def make_world(rng):
    loans, agencies = {}, {}
    for t, c in TENANTS.items():
        agencies[t] = {}
        for name, typ, _w in c["agencies"]:
            base = {"tele": 0.60, "field": 0.50, "hybrid": 0.55}[typ] + rng.uniform(-0.04, 0.04)
            agencies[t][name] = dict(type=typ, connect=base, kept=0.66 + rng.uniform(-0.04, 0.04))
        names, w = [a[0] for a in c["agencies"]], [a[2] for a in c["agencies"]]
        L = []
        for i in range(c["n"]):
            city = rng.choices(c["cities"], c["city_w"])[0]
            product = rng.choices(c["products"], c["prod_w"])[0]
            lo, hi = PRODUCT_AMT[product]
            amount = round(rng.uniform(lo, hi), -3)
            idx = rng.randint(1, 21)                                             # months before 2025-10
            y, mo = divmod(2025 * 12 + 9 - idx, 12)                              # disbursal month (0-based mo)
            tenure = idx + rng.randint(15, 36)                                   # still active in 2026-10
            risk = min(0.25, max(0.005, rng.lognormvariate(math.log(0.028), 0.6))) * PRODUCT_RISK[product]
            u = rng.random()
            dpd = 0 if u < .91 else rng.randint(3, 30) if u < .96 else rng.randint(31, 60) if u < .98 else rng.randint(61, 90) if u < .99 else rng.randint(91, 150)
            L.append(dict(loan_id=f"{t}-{i + 1:05d}", product=product, city=city, region=REGION[city],
                          agency=rng.choices(names, w)[0], vintage=f"{y}Q{mo // 3 + 1}", disbursed=amount,
                          emi=round(emi_of(amount, tenure), 0), tenure=tenure, idx=idx, p_new=risk, dpd=dpd,
                          channel=rng.choices(["nach", "upi", "app", "cash", "cheque"], [50, 20, 12, 12, 6])[0],
                          bad_phone=rng.random() < 0.06, score0=min(840, max(460, 770 - 700 * risk + rng.gauss(0, 35)))))
        loans[t] = L
    return loans, agencies


def simulate(rng, loans, agencies):
    perf, calls, ptps, pays, disputes = [], [], [], [], []
    cid = pid = payid = did = 0
    for m in SIM_MONTHS:
        keep = m >= KEEP_FROM
        steps = month_idx(m) + 4                                                 # months since 2025-10, 1-based
        for t, Ls in loans.items():
            grace = TENANTS[t]["grace"]
            for L in Ls:
                emi, d0 = L["emi"], L["dpd"]
                bs = bucket(d0)
                ag = L["agency"]
                A = agencies[t][ag]
                e = effects(t, m, L, ag)
                arrears = emi * math.ceil(d0 / 30) if d0 > 0 else 0.0
                base = dict(tenant_id=t, month=m, loan_id=L["loan_id"], product=L["product"], city=L["city"], region=L["region"], agency=ag)

                # ---- calls ----
                n_calls = (1 if rng.random() < 0.08 else 0) if d0 == 0 else max(1, int(round(rng.gauss(CALLS_BY_BUCKET[bs], 1))))
                pc = min(0.95, A["connect"] * e["connect"] * (0.92 if bs == "90+" else 1.0))
                made_ptp, call_disp = False, False
                for _ in range(n_calls):
                    if L["bad_phone"] and rng.random() < 0.7:
                        outcome = "wrong_number"
                    elif rng.random() < pc:
                        outcome = "connected"
                    else:
                        outcome = "no_answer" if rng.random() < 0.88 else "wrong_number"
                    disp, dur = "none", rng.randint(0, 25)
                    if outcome == "connected":
                        dur = 30 + int(rng.expovariate(1 / 140))
                        r, pp = rng.random(), (0.35 if d0 > 0 else 0.0)
                        if r < pp:
                            disp, made_ptp = "ptp", True
                        elif r < pp + 0.20:
                            disp = "callback"
                        elif r < pp + 0.30:
                            disp = "refuses"
                        elif r < pp + 0.34:
                            disp, call_disp = "dispute", True
                        elif r < pp + 0.39:
                            disp = "paid_already"
                    if keep:
                        cid += 1
                        calls.append(dict(tenant_id=t, call_id=f"{t}-C{cid:07d}", month=m, call_date=f"{m}-{rng.randint(1, 28):02d}",
                                          loan_id=L["loan_id"], product=L["product"], city=L["city"], region=L["region"], agency=ag,
                                          bucket=bs, outcome=outcome, disposition=disp, duration_s=dur))

                # ---- month outcome ----
                paid_emi = recovered = 0.0
                d_end = d0
                pay_rows = []                                                    # (channel, amount, status)
                ptp_row = None
                if d0 == 0:
                    miss = rng.random() < L["p_new"] * e["p_new"]
                    if not miss:
                        ch = L["channel"]
                        if ch == "nach" and rng.random() < e["bounce"]:
                            pay_rows.append(("nach", emi, "bounced"))
                            if rng.random() < 0.70:
                                miss = True
                            else:
                                pay_rows.append(("upi", emi, "success")); paid_emi = emi
                        else:
                            pay_rows.append((ch, emi, "success")); paid_emi = emi
                    if miss:
                        raw = rng.randint(3, 30)
                        if raw <= grace:                                         # paid inside the lender's grace period
                            pay_rows.append(("upi", emi, "success")); paid_emi = emi; d_end = 0
                        else:
                            d_end = raw
                            if rng.random() < 0.25:
                                pay_rows.append((L["channel"], emi, "failed"))
                else:
                    cure = False
                    if made_ptp:
                        promised = arrears + emi
                        kept_p = min(0.95, A["kept"] * e["kept"] * KEPT_B[bs])
                        kept = rng.random() < kept_p
                        part = 0.0 if kept else (promised * rng.uniform(0.1, 0.5) if rng.random() < 0.3 else 0.0)
                        paid_amt = promised if kept else part
                        ptp_row = (promised, paid_amt, 1 if kept else 0)
                        if kept:
                            cure = True
                        elif paid_amt > 0:
                            recovered = paid_amt; d_end = d0
                            pay_rows.append((rng.choice(["upi", "app", "cash"]), paid_amt, "success"))
                        else:
                            d_end = min(150, d0 + 30)
                    else:
                        u, cp = rng.random(), CURE_P[bs] * e["cure"]
                        if u < cp:
                            cure = True
                        elif u < cp + 0.15:
                            paid_emi = emi; d_end = d0
                            pay_rows.append((rng.choice(["upi", "app", "cash", "cheque"]), emi, "success"))
                        else:
                            d_end = min(150, d0 + 30)
                    if cure:
                        paid_emi, recovered, d_end = emi, arrears, 0
                        pay_rows.append((rng.choice(["upi", "app", "cash", "cheque"]), emi + arrears, "success"))

                L["dpd"] = d_end
                if not keep:
                    continue
                score = int(min(850, max(450, L["score0"] - 12 * (d_end > 0) - 25 * (d_end > 30) - 35 * (d_end > 60) + rng.randint(-6, 6))))
                elapsed = L["idx"] + steps
                perf.append({**base, "vintage": L["vintage"], "bucket": bs, "dpd_start": d0, "dpd_end": d_end, "emi_due": emi,
                             "arrears_start": round(arrears, 2), "paid_emi": round(paid_emi, 2), "recovered": round(recovered, 2),
                             "paid_full": 1 if paid_emi >= emi - 1e-6 else 0,
                             "outstanding_end": round(L["disbursed"] * max(0.05, 1 - elapsed / L["tenure"]), 2), "bureau_score": score})
                if ptp_row:
                    pid += 1
                    ptps.append(dict(tenant_id=t, ptp_id=f"{t}-P{pid:06d}", month=m, loan_id=L["loan_id"], product=L["product"],
                                     city=L["city"], region=L["region"], agency=ag, bucket=bs,
                                     promised_amount=round(ptp_row[0], 2), paid_amount=round(ptp_row[1], 2), kept=ptp_row[2]))
                for ch, amt, st in pay_rows:
                    payid += 1
                    pays.append(dict(tenant_id=t, payment_id=f"{t}-Y{payid:07d}", month=m, loan_id=L["loan_id"], product=L["product"],
                                     city=L["city"], region=L["region"], agency=ag, channel=ch, amount=round(amt, 2), status=st))
                if rng.random() < 0.003 * e["dispute"] or (call_disp and rng.random() < 0.5):
                    did += 1
                    w = [0.2, 0.2, 0.1, 0.45, 0.05] if e["dispute"] > 1 else [0.3, 0.25, 0.15, 0.08, 0.22]
                    res = rng.random() < (0.8 if m < "2026-10" else 0.5)
                    disputes.append(dict(tenant_id=t, dispute_id=f"{t}-D{did:05d}", month=m, loan_id=L["loan_id"], product=L["product"],
                                         city=L["city"], region=L["region"], agency=ag, reason=rng.choices(DISPUTE_REASONS, w)[0],
                                         resolved=1 if res else 0, days_to_resolve=rng.randint(3, 45) if res else None))
    return perf, calls, ptps, pays, disputes


def main():
    rng = random.Random(SEED)
    loans, agencies = make_world(rng)
    perf, calls, ptps, pays, disputes = simulate(rng, loans, agencies)
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(tenant_id=t, loan_id=L["loan_id"], product=L["product"], city=L["city"], region=L["region"], agency=L["agency"],
                       vintage=L["vintage"], disbursed_amount=L["disbursed"], emi=L["emi"], tenure_months=L["tenure"])
                  for t, Ls in loans.items() for L in Ls]).to_csv(OUT / "loans.csv", index=False)
    pd.DataFrame([dict(tenant_id=t, agency=a, agency_type=v["type"]) for t, d in agencies.items() for a, v in d.items()]).to_csv(OUT / "agencies.csv", index=False)
    pd.DataFrame([dict(tenant_id=t, display_name=c["name"], grace_days=c["grace"], npa_dpd=c["npa"], quirk_note=c["note"])
                  for t, c in TENANTS.items()]).to_csv(OUT / "lender_config.csv", index=False)
    for name, rows in (("fact_perf", perf), ("fact_calls", calls), ("fact_ptp", ptps), ("fact_payments", pays), ("fact_disputes", disputes)):
        pd.DataFrame(rows).to_csv(OUT / f"{name}.csv", index=False)
        print(f"{name:14s} {len(rows):>8,d} rows")
    truth = [
        dict(id="S1", lender="L1", months="2026-08..2026-10", driver={"agency": "Bluewave Collections"}, metrics=["total_collection_efficiency", "connect_rate", "delinquency_rate"]),
        dict(id="S2", lender="L2", months="2026-09..2026-10", driver={"product": "two_wheeler", "region": "East"}, metrics=["nach_bounce_rate", "new_delinquency_rate", "roll_rate"]),
        dict(id="S3", lender="L3", months="2026-02..2026-10", driver={"city": "Chennai"}, metrics=["delinquency_rate", "par30"]),
        dict(id="S4", lender="L2", months="2026-01..2026-10", driver={"agency": "Fortis Collections"}, metrics=["ptp_kept_rate", "ptp_kept_rate_amount"]),
        dict(id="S5", lender="all", months="2026-10", driver={}, metrics=["new_delinquency_rate"], note="seasonal festive swing, not a problem"),
        dict(id="S6", lender="L3", months="2026-07", driver={"product": "gold_loan", "reason": "fraud"}, metrics=["disputes_raised"]),
    ]
    (HERE / "lc01_world_truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    print("csvs ->", OUT)


if __name__ == "__main__":
    main()
