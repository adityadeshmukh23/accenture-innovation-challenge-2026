# -*- coding: utf-8 -*-
"""Slides 10-13 of the R2 deck (ROI, differentiation, roadmap, risks)."""
from gen import *

B, N = True, False

# ---------------------------------------------------------------- 10. ROI
def s_roi():
    S = [title("Business case: what per-response scrutiny costs, and what it is worth")]
    S.append(lede([
        run("The brief's reference parameter is tens of thousands of interactions per week. At "
            "30,000/week the run cost is measured from the prototype. The value is not — it "
            "depends on assumptions, so those are stated on this slide rather than buried in a "
            "footnote.", 1150)]))
    # --- MEASURED column -----------------------------------------------------
    S.append(shape("mh", ML, 1.62, 5.95, 0.40,
                   [para([run("MEASURED — TAKEN FROM THE RUNNING PROTOTYPE", 1000, B, WHITE)],
                         space_after=0)],
                   geom="roundRect", fill=PURPLE, anchor="ctr", ins=(0.14, 0.04, 0.10, 0.04)))
    measured = [
        ("$3,697 / year to run", "30,000 interactions/week × 52 × $0.00237 per request, the "
         "measured all-in cost including the verifier's own tokens."),
        ("96.4% of tokens are the verifier", "Which is why it is gated to 32% of held requests "
         "rather than run on everything — the gate is the cost control."),
        ("Recall 1.000 at precision 0.917", "Performance lane against the seeded set; Cost and "
         "Responsibility both 1.000/1.000. Final decision accuracy 95.2%."),
        ("Overhead is not the constraint", "Median 2.3 ms inline, 51 of 53 held requests inside "
         "their own budget (reference machine; wall-clock varies by hardware)."),
    ]
    y = 2.12
    for h, b in measured:
        S.append(card(ML, y, 5.95, 0.85, h, [run(b, 875)], hsz=1000,
                      ins=(0.13, 0.08, 0.13, 0.05)))
        y += 0.94
    # --- MODELED column ------------------------------------------------------
    S.append(shape("dh", 6.73, 1.62, 5.95, 0.40,
                   [para([run("MODELED — ASSUMPTIONS, NOT EVIDENCE", 1000, B, PURPLE)],
                         space_after=0)],
                   geom="roundRect", fill=WHITE, line=PURPLE, anchor="ctr",
                   ins=(0.14, 0.04, 0.10, 0.04)))
    modeled = [
        ("1.0% material defect rate", "ASSUMPTION", "Of 1,560,000 interactions a year, 15,600 "
         "carry a material defect. Not measured here — the seeded set is deliberately enriched "
         "with failures and is not a production base rate."),
        ("0.5% of defects become incidents", "ASSUMPTION", "78 costly incidents a year. Most "
         "defects are absorbed harmlessly; only a fraction reach a customer, a regulator or a "
         "balance sheet."),
        ("$25,000 per incident", "ASSUMPTION", "Remediation, regulatory exposure and "
         "reputational cost combined. This is the number most worth arguing with — so vary it "
         "below."),
        ("≈ $1.95M / year addressed", "DERIVED", "78 incidents × $25,000, against $3,697/year to "
         "run the gateway. The ratio is a consequence of the three assumptions above, not a "
         "measurement."),
    ]
    y = 2.12
    for h, tag, b in modeled:
        S.append(shape("mod", 6.73, y, 5.95, 0.85,
                       [para([run(h + "   ", 1000, B, PURPLE),
                              run(tag, 750, B, D_TAG(tag))], space_after=300),
                        para([run(b, 875, N, INK)], space_after=0, line_pct="106000")],
                       geom="roundRect", fill=WHITE, line=PURPLE, line_w=12700,
                       ins=(0.13, 0.08, 0.13, 0.05)))
        y += 0.94
    S.append(shape("sens", ML, 5.92, CW, 0.88,
                   [para([run("SENSITIVITY — CHANGE THE ONE NUMBER YOU DISAGREE WITH",
                              950, B, PURPLE)], space_after=320),
                    para([run("Cost per incident $10,000 → $780k/year  ·  $25,000 → $1.95M/year  "
                              "·  $50,000 → $3.9M/year.   Against $3,697/year to run, measured. "
                              "Every input is stated above; substitute your own and the "
                              "arithmetic holds. The point of the slide is the formula, not the "
                              "headline.", 900, N, INK)], space_after=0, line_pct="108000")],
                   geom="roundRect", fill=TINT, ins=(0.15, 0.10, 0.15, 0.07)))
    return slide(S)

def D_TAG(tag):
    return AMBER if tag == "ASSUMPTION" else MUTED

# ---------------------------------------------------------------- 11. DIFFERENTIATION
def s_diff():
    S = [title("Differentiation: what is hard to replicate, argued against named alternatives")]
    S.append(lede([
        run("Three properties are structural rather than cosmetic. Each is a consequence of "
            "deciding scrutiny per response — which is the thing none of the categories below "
            "do.", 1150)]))
    moats = [
        ("CLOSED-FORM THRESHOLDS",
         "The yellow and red cut points are derived from a stated cost-of-miss ratio, not "
         "hand-tuned. Move the ratio and the thresholds move with it, with measured calibration "
         "behind them — Brier 0.073, ECE 0.100. Anyone can copy a threshold; deriving it from a "
         "declared risk appetite is what makes it defensible to an auditor."),
        ("THE EXPLORATION SAMPLER",
         "A fraction of the traffic the gate would have skipped is verified anyway. Without it, a "
         "system only ever measures the failures it already suspected, and its false-negative "
         "rate is structurally unknowable. This is the difference between a guardrail and a "
         "measurement."),
        ("STANDALONE LEDGER VERIFIER",
         "A separate tool re-derives the hash chain without importing the application, and "
         "detects both record alteration and tail truncation. Evidence that depends on the "
         "accused system to validate it is not evidence — so the verifier does not trust the "
         "gateway, and says so in its output."),
    ]
    for i, (x, (h, b)) in enumerate(zip(COL3, moats)):
        S.append(card(x, 1.62, COL3W, 2.02, h, [run(b, 900)], tinted=(i % 2 == 0), hsz=1050))
    comps = [
        ("EVAL FRAMEWORKS", "promptfoo, DeepEval",
         "Test before launch. Nothing runs in the live path, so nothing catches the response "
         "that ships tomorrow against a context that changed today."),
        ("RUNTIME GUARDRAILS", "Guardrails AI, NeMo Guardrails",
         "Run in the path, but apply the same static checks to every request — uniform cost on "
         "uniform suspicion, with no notion of what this particular response is worth."),
        ("OBSERVABILITY", "Langfuse, Arize",
         "See everything, after delivery. Excellent for diagnosis, and structurally unable to "
         "hold a response back before a customer acts on it."),
    ]
    for x, (h, names, b) in zip(COL3, comps):
        S.append(shape("comp", x, 3.76, COL3W, 1.46,
                       [para([run(h, 1000, B, PURPLE)], space_after=220),
                        para([run(names, 900, B, INK, i=True)], space_after=300),
                        para([run(b, 875, N, INK)], space_after=0, line_pct="107000")],
                       geom="roundRect", fill=WHITE, line=MUTED, line_w=9525,
                       ins=(0.13, 0.10, 0.13, 0.07)))
    S.append(card(ML, 5.34, CW, 1.20, "THE GAP NONE OF THEM FILLS",
                  [run("Each category is competent at its own job, and a serious platform team "
                       "will run more than one of them. None decides, per response and inside the "
                       "request path, how much scrutiny that specific response deserves — which "
                       "is the only place a latency budget and a risk appetite can actually be "
                       "traded against one another. AEGIS is a gateway: not a test suite, and not "
                       "a dashboard.", 950)], tinted=False))
    return slide(S)

# ---------------------------------------------------------------- 12. ROADMAP
def s_roadmap():
    S = [title("Phased roadmap: from a verified prototype to enterprise rollout")]
    S.append(lede([
        run("Phase 1 is not a mock or a storyboard. ", 1150, B),
        run("It runs offline from a clean clone in one command, with 96 tests, an independently "
            "verifiable audit trail, and metrics that reproduced exactly on second hardware.",
            1150)]))
    phases = [
        ("PHASE 1 · POC — SHIPPED AND VERIFIED",
         ["Working gateway; 4 use-case policies plus an EU/GDPR overlay",
          "3 lanes with calibrated models; adaptive gate holding 32% invocation",
          "Hash-chained ledger, standalone verifier, tamper proof for alteration and truncation",
          "96 tests; reproducible from a cold clone in a single command",
          "Deterministic metrics identical across two independent machines"]),
        ("PHASE 2 · PILOT — ONE TEAM, REAL TRAFFIC",
         ["Point one production application at the gateway in shadow mode",
          "Embedding retrieval replacing lexical; a small NLI model behind the same interface",
          "Operator identity and API keys on the control endpoints",
          "Shared baseline store so cost baselines survive a restart",
          "Calibrate the cost-of-miss ratio against that team's real incident cost"]),
        ("PHASE 3 · ENTERPRISE — MANY TEAMS, ONE CONTROL PLANE",
         ["Per-tenant policy binding, quotas and segmented baselines",
          "WORM or externally anchored ledger — head hash published outside operator control",
          "Batched review with an approval quorum and per-operator trust weighting",
          "Sampled deep pass for high-volume tiers; the policy field already exists",
          "Fairness monitoring across cohorts rather than per individual response"]),
    ]
    for i, (x, (h, items)) in enumerate(zip(COL3, phases)):
        ps = [para([run(h, 1050, B, PURPLE)], space_after=380)]
        for it in items:
            ps.append(para([run("—  ", 900, B, PURPLE), run(it, 900, N, INK)],
                           space_after=260, line_pct="106000"))
        ps[-1]["sa"] = 0
        S.append(shape("phase", x, 1.62, COL3W, 3.06, ps, geom="roundRect",
                       fill=TINT if i % 2 == 0 else WHITE,
                       line=None if i % 2 == 0 else PURPLE, line_w=12700,
                       ins=(0.14, 0.11, 0.13, 0.08)))
    S.append(rule("tl", 0.77, 4.98, 11.37))
    for x in (1.99, 6.26, 10.52):
        S.append(dot("d", x, 4.90))
    S.append(card(ML, 5.30, CW, 1.28, "WHAT GATES EACH TRANSITION — NEITHER GATE IS A DATE",
                  [run("Phase 1 → 2 requires a team willing to run shadow mode and able to state "
                       "a cost of miss for their use case; without that number the thresholds "
                       "cannot be derived, only guessed. Phase 2 → 3 requires the pilot's "
                       "measured false-negative rate — which the exploration sampler is what "
                       "makes observable — to be acceptable at that team's declared risk "
                       "appetite.", 950)], tinted=False))
    return slide(S)

# ---------------------------------------------------------------- 13. RISKS
def s_risks():
    S = [title("Key risks and mitigations, taken from the prototype's measured limits")]
    S.append(lede([
        run("These are the repository's own documented limitations, not a generic risk list. ",
            1150, B),
        run("A judge who reads the code will find every one of them written down there, with the "
            "measurement that established it — including one that only became true today.",
            1150)]))
    risks = [
        ("Retrieval is lexical (IDF cosine), so a correct answer that shares no vocabulary with "
         "its source under-scores",
         "This is exactly what fin_overflag_01 demonstrates — the deliberate false positive on "
         "the results slide. Production adds embedding retrieval, keeping the lexical score as a "
         "cheap prefilter rather than replacing it."),
        ("Negation handling is a parity count over a cue set, not entailment",
         "Measured, not assumed: it catches plain flips and double negatives, and misses litotes "
         "outside the cue set and qualifiers silently dropped from the context. Production puts a "
         "small NLI model behind the same ClaimTrace interface the dashboard already renders."),
        ("PII detection is regex plus checksum, tuned for English and US formats",
         "The Luhn check generalises; the address pattern does not. Production adds an NER model "
         "and locale-specific detectors — and until then, the policy layer compensates by never "
         "streaming PHI tiers at all."),
        ("The control endpoints have no authentication",
         "Overrides are rate-limited and guarded against contradicting checksum-grade detections, "
         "but anyone who can reach the port can submit feedback under any operator name. "
         "Production adds operator identity, API keys and per-tenant policy binding."),
        ("Cost baselines are per-process and in memory, and a simultaneous burst partly masks "
         "itself",
         "Measured: five identical anomalies against a warm baseline score 1.0 then near zero — "
         "the first is caught and the rest are absorbed by the EWMA they are moving. Production "
         "scores against a window-open snapshot or a windowed quantile."),
        ("Inline metrics and wall-clock latency vary by hardware",
         "Established this session: an independent cold-clone run reproduced every deterministic "
         "figure exactly and differed on inline accuracy and latency. Now handled as two "
         "documented tiers — deterministic figures enforced by the build, machine-dependent ones "
         "labelled and reported — rather than presented as one."),
        ("No concurrency or load evidence: every figure here is a single process answering "
         "sequential requests",
         "Stated, not solved. The reference run is 57 requests issued one after another against "
         "one process holding its baselines in memory. No throughput, concurrency or p99-under-"
         "load figure has been measured, so nothing on these slides should be read as a scale "
         "claim. A load harness is prerequisite work for the pilot, not a deadline item."),
    ]
    y = 1.60
    for i, (r, m) in enumerate(risks):
        S.append(shape("risk", ML, y, CW, 0.78,
                       [para([run("RISK  ", 800, B, AMBER), run(r, 950, B, INK)],
                             space_after=250),
                        para([run("MITIGATION  ", 800, B, GREEN), run(m, 875, N, INK)],
                             space_after=0, line_pct="105000")],
                       geom="roundRect", fill=TINT if i % 2 == 0 else WHITE,
                       line=None if i % 2 == 0 else MUTED, line_w=9525,
                       ins=(0.14, 0.07, 0.14, 0.05)))
        y += 0.83
    return slide(S)
