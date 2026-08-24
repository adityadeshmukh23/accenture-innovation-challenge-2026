"""Deterministically generate the long contract used by the budget-miss scenario.

Committed output, fixed seed: the file is identical from a clean clone. The
document is long enough that the verifier's genuine compute -- IDF index build
plus a per-claim scan over every sentence -- exceeds the 300ms inline budget.
Nothing here simulates a delay; the cost is real work over a real document.
"""
import random
import sys

SUBJECTS = ["the Supplier", "the Customer", "either party", "the Data Processor",
            "the Service Provider", "the Licensee", "the Contracting Authority"]
VERBS = ["shall provide", "must retain", "may terminate", "will indemnify", "shall notify",
         "must not disclose", "shall procure", "may audit", "will remediate", "shall escalate"]
OBJECTS = ["the deliverables", "all personal data", "the service credits", "the audit report",
           "the escrow materials", "the incident log", "the subcontractor list",
           "the change request", "the security assessment", "the retention schedule"]
QUALIFIERS = ["within {n} business days", "no later than {n} days after the Effective Date",
              "on {n} days written notice", "at a rate of {n}% of the annual charges",
              "for a period of {n} months", "subject to a cap of {n}% of fees paid",
              "following {n} consecutive failures", "upon {n} hours notice"]
TOPICS = ["Confidentiality", "Service Levels", "Data Protection", "Termination", "Liability",
          "Change Control", "Business Continuity", "Audit Rights", "Intellectual Property",
          "Subprocessing", "Force Majeure", "Insurance", "Governing Law", "Escalation"]


def main(n_clauses: int, out_path: str) -> None:
    rng = random.Random(20260824)
    lines = [
        "MASTER SERVICES AGREEMENT — synthetic demonstration document.",
        "This document is machine-generated for load and latency testing. "
        "It contains no real contractual terms and no real party names.",
    ]
    for i in range(1, n_clauses + 1):
        topic = TOPICS[i % len(TOPICS)]
        q = rng.choice(QUALIFIERS).format(n=rng.choice([3, 5, 7, 10, 14, 20, 24, 30, 45, 60, 90, 120]))
        lines.append(
            f"Clause {i}.{rng.randint(1, 9)} ({topic}): {rng.choice(SUBJECTS)} "
            f"{rng.choice(VERBS)} {rng.choice(OBJECTS)} {q}."
        )
    # A handful of specific, checkable facts buried in the bulk. These are what
    # the scenario's question is actually about.
    lines.insert(60, "Clause 4.2 (Service Levels): The monthly availability target is 99.5% "
                     "measured over each calendar month.")
    lines.insert(220, "Clause 9.1 (Liability): The aggregate liability cap is 125% of the "
                      "charges paid in the preceding twelve months.")
    lines.insert(400, "Clause 12.3 (Termination): Either party may terminate for convenience "
                      "on 90 days written notice.")
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {out_path}: {len(lines)} sentences")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1500,
         sys.argv[2] if len(sys.argv) > 2 else "scenarios/corpus/long_msa.txt")
