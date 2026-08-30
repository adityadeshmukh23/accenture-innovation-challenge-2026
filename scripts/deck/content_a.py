# -*- coding: utf-8 -*-
"""Slides 5-9 of the R2 deck (architecture, adoption, results x2, rigor)."""
from gen import *

B, N, M = True, False, MUTED
def hdr(t, sz=1100): return run(t, sz=sz, b=True, color=PURPLE)

# ---------------------------------------------------------------- 5. ARCHITECTURE
def s_architecture():
    S = [title("Solution architecture: five stages, one decision per response")]
    S.append(lede([
        run("The ordering is the design. ", 1150, B),
        run("The prior runs before generation because it sets the latency posture. The cheap "
            "checks run before the verifier because they decide whether the verifier is worth "
            "paying for. The verifier runs under a deadline because a check that overruns its "
            "budget has failed, not succeeded.", 1150)]))
    stages = [
        ("1 · RISK PRIOR", "Before a token is generated. Request signals — endpoint, transaction "
         "value, user tier, data sensitivity, geo — set the stakes tier and the latency posture."),
        ("2 · EVIDENCE", "After generation, ordered by cost. Context is prepared once, then the "
         "cheap tier — cost telemetry, PII and policy, grounding proxies — runs on 100% of held "
         "traffic."),
        ("3 · ADAPTIVE GATE", "Does this response earn the verifier? T2 always does; so does any "
         "response whose cheap score clears the threshold; plus an exploration sample."),
        ("4 · DECISION", "Calibrated per-lane models produce p, widened into an uncertainty band "
         "by missing evidence. T0 fails open, T1/T2 fail closed. Thresholds derive from the "
         "policy's stated λ."),
        ("5 · ASYNC DEEP PASS", "After release, with no latency ceiling: full verifier plus bias. "
         "A streamed response now flagged is retracted; a held one is escalated post hoc."),
    ]
    w, gap = 2.379, 0.111
    for i, (h, b) in enumerate(stages):
        S.append(card(ML + i * (w + gap), 1.62, w, 1.74, h, [run(b, 900)],
                      tinted=(i % 2 == 0), hsz=1000))
    tiers = [
        ("T0 · STREAM", "Low stakes. Streams immediately at zero inline overhead, audited after "
         "the fact with one-click retraction."),
        ("T1 · HOLD", "Held inside a 300 ms budget. Fails closed — missing evidence widens the "
         "band upward, not downward."),
        ("T2 · HOLD + DEEP", "High stakes. Always earns the verifier, and always gets the "
         "asynchronous deep pass afterwards."),
    ]
    for x, (h, b) in zip(COL3, tiers):
        S.append(card(x, 3.50, COL3W, 0.95, h, [run(b, 900)], tinted=False, hsz=1000))
    outs = [("GREEN · deliver", GREEN, "response released unchanged"),
            ("YELLOW · auto-edit", AMBER, "redact, strip the contradicted claim, add a caveat"),
            ("RED · hold + reroute", RED, "safe template, human queue, full audit record")]
    for x, (h, c, b) in zip(COL3, outs):
        S.append(shape("out", x, 4.60, COL3W, 0.66,
                       [para([run(h, 950, B, c)], space_after=300),
                        para([run(b, 850, N, INK)], space_after=0)],
                       geom="roundRect", fill=WHITE, line=c, line_w=12700,
                       ins=(0.13, 0.08, 0.13, 0.06)))
    S.append(card(ML, 5.44, 6.0, 1.30, "POLICY LAYER — CONFIGURATION, NOT CODE",
                  [run("Per use case and per geography, as YAML with overlays. Four use-case "
                       "policies ship — support, fintech, clinical, batch — plus an EU/GDPR "
                       "overlay. Thresholds are derived from a stated cost-of-miss ratio, so a "
                       "changed risk appetite moves the cut points rather than the code.", 900)]))
    S.append(card(6.73, 5.44, 6.0, 1.30, "AUDIT AND FEEDBACK — THE LOOP THAT CLOSES",
                  [run("Every decision writes a hash-chained SQLite record. A standalone tool "
                       "re-derives the chain without importing the app, catching both alteration "
                       "and truncation. Human confirmations and overrides become labelled rows "
                       "the lane models are refitted on.", 900)]))
    return slide(S)

# ---------------------------------------------------------------- 6. USERS
def s_users():
    S = [title("Who runs it: the platform team owns the gateway, each use case owns its policy")]
    S.append(lede([
        run("AEGIS is a reverse proxy in front of existing LLM traffic — one base-URL change, no "
            "retraining, no model swap. That splits the buyer from the day-to-day operator, "
            "deliberately: the team that carries the latency budget is not the team that sets the "
            "risk appetite.", 1150)]))
    S.append(card(ML, 1.62, 6.0, 1.55, "THE BUYER — THE AI PLATFORM TEAM",
                  [run("Owns the gateway, the latency budget and the audit trail. Cares that one "
                       "deployment covers every model and every application behind it, that the "
                       "overhead is defensible, and that the evidence survives an auditor who "
                       "arrives months later asking why a specific response went out.", 900)]))
    S.append(card(6.73, 1.62, 6.0, 1.55, "THE OPERATORS — PER-USE-CASE POLICY OWNERS",
                  [run("A support lead, a compliance officer, a clinical safety reviewer. Each "
                       "owns one YAML policy: their own risk appetite, their own thresholds, "
                       "their own escalation rule — and their own review queue — without "
                       "touching the gateway or asking the platform team for a deploy.", 900)],
                  tinted=False))
    S.append(shape("polhdr", ML, 3.33, CW, 0.30,
                   [para([run("FOUR USE-CASE POLICIES SHIP, WITH DELIBERATELY DIFFERENT "
                              "RISK AND LATENCY PROFILES", 950, B, PURPLE)], space_after=0)]))
    pols = [("support_copilot", "T0 / T1 · low stakes · streams · 5% verified inline"),
            ("fintech_advisor", "T2 · high transaction value · always verified"),
            ("clinical_intake", "T2 + EU overlay · PHI · never streams"),
            ("batch_analytics", "T2 · 30 s budget · no latency ceiling")]
    w, gap = 2.994, 0.120
    for i, (n, d) in enumerate(pols):
        S.append(card(ML + i * (w + gap), 3.70, w, 0.82, n, [run(d, 850)], hsz=1000,
                      ins=(0.12, 0.09, 0.12, 0.06)))
    steps = [
        ("1 · POINT ONE APP AT IT", "Change base_url. Nothing else changes — the client still "
         "receives an ordinary OpenAI response, with the policy already applied to its text."),
        ("2 · RUN IN SHADOW", "Every decision is recorded and every metric computed while the "
         "actions stay off. The dashboard shows what would have been held, before anything is."),
        ("3 · ENFORCE, ONE POLICY AT A TIME", "Turn actions on for a single use case, starting "
         "where hold-and-release is already expected and the cost of a miss is already known."),
    ]
    for x, (h, b) in zip(COL3, steps):
        S.append(card(x, 4.70, COL3W, 1.16, h, [run(b, 900)], tinted=False, hsz=1000))
    S.append(shape("foot", ML, 6.04, CW, 0.62,
                   [para([run("Deployment reality: ", 950, B, PURPLE),
                          run("eight pinned Python packages. No ML framework, no database server, "
                              "no JavaScript build chain. One command runs the whole system "
                              "offline from a clean clone — no API key, no Docker, no network.",
                              950, N, INK)], space_after=0, line_pct="110000")]))
    return slide(S)

# ---------------------------------------------------------------- 7. QUALITY
def s_quality():
    S = [title("Prototype results: detection quality against a seeded, labelled scenario set")]
    S.append(lede([
        run("21 labelled evaluation scenarios and 65 calibration rows, disjoint by construction. "
            "Checks cannot read scenario_id or ground_truth — a test enforces that boundary, "
            "because without it every number on this slide would be worthless.", 1150)]))
    S.append(shape("stat", ML, 1.62, 2.85, 1.72,
                   [para([run("FINAL DECISION ACCURACY", 900, B, PURPLE)], space_after=300),
                    para([run("95.2%", 3200, B, PURPLE)], space_after=200),
                    para([run("20 of 21 scenarios, after the asynchronous deep pass",
                              850, N, INK)], space_after=0)],
                   geom="roundRect", fill=TINT, ins=(0.16, 0.13, 0.14, 0.08)))
    S.append(shape("tblbg", 3.50, 1.62, 9.23, 1.72, [para([run(" ", 800)], space_after=0)],
                   geom="roundRect", fill=WHITE, line=PURPLE, line_w=12700))
    cols = [("Lane", ["Performance", "Cost", "Responsibility"], 3.68, 2.05, "l"),
            ("TP", ["11", "1", "6"], 5.80, 0.55, "ctr"),
            ("FP", ["1", "0", "0"], 6.38, 0.55, "ctr"),
            ("FN", ["0", "0", "0"], 6.96, 0.55, "ctr"),
            ("TN", ["9", "20", "15"], 7.54, 0.55, "ctr"),
            ("Precision", ["0.917", "1.000", "1.000"], 8.30, 1.32, "ctr"),
            ("Recall", ["1.000", "1.000", "1.000"], 9.70, 1.32, "ctr"),
            ("F1", ["0.957", "1.000", "1.000"], 11.10, 1.40, "ctr")]
    # Row order is Performance, Cost, Responsibility. The Cost row is rendered
    # muted rather than in body ink: it rests on a single positive example, and
    # printing it at the same weight as a 12-example rate overstates it.
    ROW_INK = [INK, MUTED, INK]
    for head, vals, x, w, al in cols:
        ps = [para([run(head, 900, B, PURPLE)], align=None if al == "l" else al, space_after=280)]
        for v, ink in zip(vals, ROW_INK):
            ps.append(para([run(v, 1000, al != "l", ink)],
                           align=None if al == "l" else al, space_after=240))
        S.append(shape("col", x, 1.76, w, 1.50, ps, ins=(0.02, 0.02, 0.02, 0.02)))
    CW2 = (CW - 0.27) / 2
    S.append(card(ML, 3.46, CW2, 1.45, "THE SINGLE FALSE POSITIVE IS DELIBERATE",
                  [run("fin_overflag_01 is a substantively correct answer — “a shade over "
                       "four percent after charges” — expressed in words rather than "
                       "figures, so numeric grounding finds nothing to match. It exists so the "
                       "false-positive rate is non-zero and honest, and so the risk-appetite "
                       "slider has something real to trade against. A checker scoring 1.000 on "
                       "everything would mean the corpus was too easy, not that the checker was "
                       "good.", 950)]))
    S.append(card(ML + CW2 + 0.27, 3.46, CW2, 1.45,
                  "WHAT THESE RATES REST ON — 95% CI",
                  [run("Performance 0.917 over 12 predicted positives — CI [0.65, 0.99]. "
                       "Responsibility 1.000 over 6 — CI [0.61, 1.00]. ", 950),
                   run("Cost 1.000 over a single positive — CI [0.21, 1.00]: demonstrated on "
                       "one example, not statistically measured.", 950, B),
                   run(" Read that row as the mechanism firing on the case it was built for, "
                       "not as a detection rate. Wilson intervals, generated with the figures "
                       "they qualify.", 950)]))
    trio = [("CALIBRATION", "Brier 0.073, ECE 0.100 across 63 lane-decisions. The models are "
             "fitted by running the real checks over every corpus row — no hand-written vectors."),
            ("EVIDENCE BOUNDARY", "Checks receive only (question, context, response, telemetry). "
             "Scenario identity and ground truth are stripped before any check sees them."),
            ("TEST SUITE", "96 tests, including the no-leakage proof, the ledger tamper proof, "
             "and a guard that fails the build if any documented figure drifts from the run.")]
    for x, (h, b) in zip(COL3, trio):
        S.append(card(x, 5.03, COL3W, 1.12, h, [run(b, 900)], tinted=False, hsz=1000))
    S.append(shape("note", ML, 6.27, CW, 0.62,
                   [para([run("Reproducibility: ", 900, B, PURPLE),
                          run("every figure on this slide is deterministic — an independent "
                              "cold-clone run on different hardware reproduced all of them "
                              "exactly. Inline (pre-async) accuracy and wall-clock latency do "
                              "vary by machine, and are labelled as reference-machine "
                              "measurements wherever they appear.", 900, N, MUTED)],
                         space_after=0, line_pct="110000")]))
    return slide(S)

# ---------------------------------------------------------------- 8. EFFICIENCY
def s_efficiency():
    S = [title("Prototype results: what makes a 300 ms budget survivable at scale")]
    S.append(lede([
        run("Scrutiny is rationed, not uniform. ", 1150, B),
        run("The verifier is the expensive check — 96.4% of every token spent. Running it on "
            "everything is what makes governance too costly to leave switched on; running it on "
            "nothing is what makes it useless. The gate decides per response.", 1150)]))
    stats = [("32%", "of held requests earned the inline verifier — 17 of 53"),
             ("100% / 5%", "high-stakes policies versus low-stakes support traffic"),
             ("$0.00237", "estimated cost per request, all-in, verifier included"),
             ("96.4%", "of tokens are the verifier — which is precisely why gating it matters")]
    w, gap = 2.994, 0.120
    for i, (big, sub) in enumerate(stats):
        S.append(shape("stat", ML + i * (w + gap), 1.62, w, 1.42,
                       [para([run(big, 2400, B, PURPLE)], space_after=250),
                        para([run(sub, 875, N, INK)], space_after=0, line_pct="107000")],
                       geom="roundRect", fill=TINT if i % 2 == 0 else WHITE,
                       line=None if i % 2 == 0 else PURPLE, line_w=12700,
                       ins=(0.14, 0.12, 0.13, 0.08)))
    S.append(card(ML, 3.20, CW, 1.22, "HOW THE GATE DECIDES — AND WHY THE THIRD ROUTE MATTERS MOST",
                  [run("Three ways in: the tier (T2 always earns it), a cheap-signal score over "
                       "the threshold, or an exploration sample. The third is the one that is "
                       "easy to leave out and expensive to omit — verifying a fraction of the "
                       "traffic the gate would have skipped is the only way the false-negative "
                       "rate stays observable. A gate that only ever verifies what it already "
                       "suspects cannot measure what it misses.", 950)], tinted=False))
    S.append(shape("byp", ML, 4.54, CW, 0.30,
                   [para([run("VERIFIER INVOCATION BY POLICY — THE RATIO IS THE PRODUCT",
                              950, B, PURPLE)], space_after=0)]))
    by = [("clinical_intake", "6 / 6", "100%"), ("fintech_advisor", "6 / 6", "100%"),
          ("batch_analytics", "1 / 1", "100%"), ("default", "2 / 2", "100%"),
          ("support_copilot", "2 / 38", "5%")]
    w2, gap2 = 2.379, 0.111
    for i, (n, frac, pct) in enumerate(by):
        S.append(shape("pol", ML + i * (w2 + gap2), 4.90, w2, 0.86,
                       [para([run(n, 875, B, INK)], space_after=200),
                        para([run(pct + "  ", 1300, B, PURPLE), run(frac + " held", 850, N, MUTED)],
                             space_after=0)],
                       geom="roundRect", fill=TINT, ins=(0.12, 0.09, 0.10, 0.06)))
    S.append(shape("lat", ML, 5.94, CW, 0.70,
                   [para([run("LATENCY — MEASURED ON THE REFERENCE MACHINE  ", 900, B, PURPLE),
                          run("Median inline overhead 2.3 ms; 51 of 53 held requests completed "
                              "inside their own policy budget; p95 overhead 1.8% of that budget; "
                              "streamed requests add 0.0 ms. Wall-clock figures vary with "
                              "hardware, so they are reported rather than pinned — the "
                              "deterministic figures above are not.", 900, N, INK)],
                         space_after=0, line_pct="110000")],
                   geom="roundRect", fill=WHITE, line=MUTED, line_w=9525))
    return slide(S)

# ---------------------------------------------------------------- 9. RIGOR
def s_rigor():
    S = [title("Evidence of rigor: a defect an adversarial review found, and how it was closed")]
    S.append(lede([
        run("The prototype was reviewed adversarially against its own claims. This was the most "
            "serious thing that review found — and it was in the half of the system that already "
            "looked finished.", 1150)]))
    tri = [("WHAT WAS ALREADY CORRECT",
            "A clinical_intake response under the EU/GDPR overlay was ruled RED, its visible "
            "answer replaced with a safe template, streaming refused despite the client asking "
            "for it, and a human queued. The policy engine did its job exactly as designed."),
           ("WHAT WAS STILL WRONG",
            "The metadata beside it carried original_text verbatim. The gateway ruled the content "
            "unreleasable and then released it in the same HTTP response — MRN, date of birth, "
            "phone and SSN — on both the streamed and non-streamed paths."),
           ("WHY REVIEW HAD MISSED IT",
            "The regression test's own docstring claimed it asserted on “the bytes a client "
            "actually receives”. It only reassembled the streamed content deltas, and never "
            "inspected the envelope those deltas arrive inside.")]
    for i, (x, (h, b)) in enumerate(zip(COL3, tri)):
        S.append(card(x, 1.70, COL3W, 1.74, h, [run(b, 900)], tinted=(i != 1), hsz=1000))
    S.append(shape("fix", ML, 3.56, CW, 1.28,
                   [para([run("THE FIX, VERIFIED ON THE RAW HTTP RESPONSE — NOT ON A PASSING "
                              "UNIT TEST", 1050, B, PURPLE)], space_after=380),
                    para([run("Before: ", 950, B, RED),
                          run("all four PHI markers present in the response body, on both "
                              "transports.   ", 950, N, INK),
                          run("After: ", 950, B, GREEN),
                          run("none. Free text is now withheld by default — a caller opts in with "
                              "include_raw_trace — and that opt-in never overrides policy: RED, "
                              "edited, rerouted, retracted or PII-bearing responses stay redacted "
                              "whatever the caller asked for.", 950, N, INK)],
                         space_after=0, line_pct="108000")],
                   geom="roundRect", fill=WHITE, line=PURPLE, ins=(0.15, 0.11, 0.15, 0.08)))
    S.append(card(ML, 4.96, CW, 1.30, "THE PART THAT WAS NOT IN THE REVIEW'S BRIEF",
                  [run("Redacting only the fields the review named would not have closed it. "
                       "claims[].reasons names the offending figures one at a time — "
                       "“figure 6789 does not appear anywhere in the context” — so an "
                       "SSN redacted from the claim stayed reassemblable from the reasons beside "
                       "it. The fix redacts the whole trace. The audit ledger still keeps the "
                       "original, because an audit trail that redacts the thing it is auditing "
                       "is worthless.", 950)]))
    S.append(shape("tests", ML, 6.38, CW, 0.44,
                   [para([run("Ten new regression tests parse the full raw body on both "
                              "transports. All ten fail against the previous commit and pass "
                              "against this one — the test has teeth, and that was checked rather "
                              "than assumed.", 900, N, MUTED)], space_after=0)]))
    return slide(S)
