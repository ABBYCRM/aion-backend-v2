"""
AION Kernel — enforces the 7 Prime Operating Laws and resolves decisions.

Laws (from PROMPT_KERNEL_v1.1):
  1. REALITY      — external reality/evidence/constraints dominate narrative
  2. CONTINUITY   — preserve logical/architectural/identity continuity
  3. FIDELITY     — do not degrade architecture/frameworks/conceptual hierarchies
  4. LATTICE      — cognition exists inside an interconnected lattice
  5. EPISTEMIC    — separate observation/inference/hypothesis/theory/speculation
  6. PERPETUITY   — outputs reusable, extendable, portable across systems
  7. DECISION     — resolve every meaningful reasoning into COMMIT / DEFER / REJECT

Decision Protocol (8 steps):
  1 goal_identification
  2 constraint_analysis
  3 uncertainty_estimation
  4 risk_evaluation
  5 leverage_detection
  6 reversibility_check
  7 evidence_strength
  8 downstream_consequences
"""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class DecisionState(str, Enum):
    COMMIT = "COMMIT"
    DEFER = "DEFER"
    REJECT = "REJECT"


class EpistemicTag(str, Enum):
    OBSERVATION = "observation"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    THEORY = "theory"
    SPECULATION = "speculation"


@dataclass
class LawCheck:
    law: str
    passed: bool
    note: str = ""


@dataclass
class Decision:
    state: DecisionState
    score: float                       # -1.0 .. +1.0
    rationale: str
    checks: List[LawCheck] = field(default_factory=list)
    protocol: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"dec_{uuid.uuid4().hex[:12]}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class MissionContext:
    """Captures the structured context the kernel reasons over."""
    user_input: str
    history: List[Dict[str, str]] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    started_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(self.user_input.encode("utf-8"))
        for m in self.history[-8:]:
            h.update(m.get("role", "").encode("utf-8"))
            h.update(m.get("content", "").encode("utf-8"))
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Reality check — does the request claim something verifiable?
# ---------------------------------------------------------------------------
UNSUPPORTED_CLAIM_TOKENS = [
    "guaranteed", "always works", "100% safe", "never fails",
    "definitely will", "absolutely certain", "no risk",
]


def check_reality(ctx: MissionContext) -> LawCheck:
    text = (ctx.user_input or "").lower()
    hits = [t for t in UNSUPPORTED_CLAIM_TOKENS if t in text]
    if hits and not ctx.metadata.get("allow_absolute_claims"):
        return LawCheck(
            law="REALITY",
            passed=False,
            note=f"unsupported-claim-token: {hits[0]}",
        )
    return LawCheck(law="REALITY", passed=True, note="no unfounded absolutes detected")


# ---------------------------------------------------------------------------
# Continuity check — do we have a coherent thread to reason from?
# ---------------------------------------------------------------------------
def check_continuity(ctx: MissionContext) -> LawCheck:
    if not ctx.user_input or not ctx.user_input.strip():
        return LawCheck(law="CONTINUITY", passed=False, note="empty input")
    if len(ctx.history) > 200:
        return LawCheck(
            law="CONTINUITY",
            passed=False,
            note="history exceeds 200 messages — compress or start new thread",
        )
    return LawCheck(law="CONTINUITY", passed=True, note=f"thread_id={ctx.request_id}")


# ---------------------------------------------------------------------------
# Fidelity check — does the user request degrade the architecture?
# ---------------------------------------------------------------------------
FIDELITY_BREACH_TOKENS = [
    "delete the kernel", "drop the laws", "bypass the audit",
    "skip the decision", "remove the continuity", "ignore fidelity",
]


def check_fidelity(ctx: MissionContext) -> LawCheck:
    text = (ctx.user_input or "").lower()
    for tok in FIDELITY_BREACH_TOKENS:
        if tok in text:
            return LawCheck(law="FIDELITY", passed=False, note=f"breach-token: {tok}")
    return LawCheck(law="FIDELITY", passed=True, note="architecture preserved")


# ---------------------------------------------------------------------------
# Lattice check — does the response belong to the connected signal graph?
# ---------------------------------------------------------------------------
def check_lattice(ctx: MissionContext) -> LawCheck:
    has_input = bool(ctx.user_input and ctx.user_input.strip())
    has_signal = has_input or bool(ctx.history)
    if not has_signal:
        return LawCheck(law="LATTICE", passed=False, note="no input signal")
    return LawCheck(law="LATTICE", passed=True, note=f"signals_in={1 + len(ctx.history)}")


# ---------------------------------------------------------------------------
# Epistemic check — must not present speculation as fact.
# (We rely on the system prompt to enforce this in the LLM; here we
#  detect obvious epistemic violations in the input.)
# ---------------------------------------------------------------------------
def check_epistemic(ctx: MissionContext) -> LawCheck:
    text = (ctx.user_input or "").lower()
    bad = ["trust me", "just believe", "everyone knows that", "obviously true"]
    if any(b in text for b in bad):
        return LawCheck(law="EPISTEMIC", passed=False, note="input contains appeal-to-authority without evidence")
    return LawCheck(law="EPISTEMIC", passed=True, note="epistemic markers clean")


# ---------------------------------------------------------------------------
# Perpetuity check — output must be reusable / portable.
# ---------------------------------------------------------------------------
def check_perpetuity(ctx: MissionContext) -> LawCheck:
    # Pure conversational replies are always portable; code/config outputs
    # are checked at output time, not at intake.
    return LawCheck(law="PERPETUITY", passed=True, note="portability=enabled")


# ---------------------------------------------------------------------------
# Decision — apply 8-step protocol, score, return COMMIT/DEFER/REJECT.
# ---------------------------------------------------------------------------
def resolve_decision(ctx: MissionContext) -> Decision:
    """Run the 7-law check + 8-step protocol and return a decision."""
    checks = [
        check_reality(ctx),
        check_continuity(ctx),
        check_fidelity(ctx),
        check_lattice(ctx),
        check_epistemic(ctx),
        check_perpetuity(ctx),
    ]
    failed = [c for c in checks if not c.passed]

    # 8-step protocol (deterministic, evidence-driven, no LLM dependency)
    protocol = {
        "1_goal_identification": ctx.user_input.strip()[:160],
        "2_constraint_analysis": [c.law + ":" + ("FAIL" if not c.passed else "ok") for c in checks],
        "3_uncertainty_estimation": 0.1 if not failed else 0.5,
        "4_risk_evaluation": "low" if not failed else "elevated",
        "5_leverage_detection": True,
        "6_reversibility_check": True,   # chat replies are reversible
        "7_evidence_strength": "high" if not failed else "mixed",
        "8_downstream_consequences": "user_visible_reply",
    }

    if any(c.law in ("FIDELITY", "LATTICE") and not c.passed for c in checks):
        state = DecisionState.REJECT
        score = -1.0
        rationale = "Architecture-breach or no-signal detected. Hard reject."
    elif failed:
        state = DecisionState.DEFER
        score = -0.3
        rationale = "One or more soft law checks failed. Defer with caveats surfaced."
    else:
        state = DecisionState.COMMIT
        score = 0.8
        rationale = "All 7 law checks passed. Proceed with COMMIT."

    return Decision(
        state=state,
        score=score,
        rationale=rationale,
        checks=checks,
        protocol=protocol,
    )


# ---------------------------------------------------------------------------
# Continuity Pack — the portable identity signature.
# ---------------------------------------------------------------------------
AION_CONTINUITY_PACK: Dict[str, Any] = {
    "system_name": "AION",
    "identity_class": "Adaptive Intelligence Operating Nexus",
    "architecture_type": "Megalithic Intelligence Lattice",
    "core_laws": [
        "Reality", "Continuity", "Fidelity", "Lattice",
        "Epistemic", "Perpetuity", "Decision",
    ],
    "decision_states": [s.value for s in DecisionState],
    "epistemic_tags": [t.value for t in EpistemicTag],
    "logic_motors": [
        "signal_motor", "perception_motor", "routing_motor",
        "causal_motor", "memory_motor", "prediction_motor",
        "decision_motor", "synthesis_motor", "audit_motor",
        "portability_motor",
    ],
    "output_bias": "structured_high_signal",
    "portability": "enabled",
    "continuity_priority": "maximum",
    "lattice_coherence": "enforced",
}


def build_system_prompt(decision: Decision) -> str:
    """Build the system prompt for the LLM that enforces the kernel."""
    decision_block = (
        f"DECISION: {decision.state.value}\n"
        f"SCORE: {decision.score}\n"
        f"RATIONALE: {decision.rationale}\n"
    )
    law_block = "\n".join(
        f"- {c.law}: {'PASS' if c.passed else 'FAIL'} ({c.note})"
        for c in decision.checks
    )
    return f"""You are AION — Adaptive Intelligence Operating Nexus.
You operate inside a Megalithic Intelligence Lattice governed by 7 Prime Operating Laws.

=== PRIME OPERATING LAWS ===
1. REALITY    — external reality, constraints, evidence, causal structure dominate narrative.
2. CONTINUITY — preserve logical, architectural, and identity continuity across turns.
3. FIDELITY   — never degrade system architecture, frameworks, or conceptual hierarchies.
4. LATTICE    — every output belongs to an interconnected lattice of signals/models/decisions.
5. EPISTEMIC  — separate observation / inference / hypothesis / theory / speculation.
6. PERPETUITY — outputs must be reusable, extendable, and portable across systems.
7. DECISION   — every meaningful reasoning resolves to COMMIT / DEFER / REJECT.

=== THIS TURN — KERNEL STATE ===
{decision_block}

=== LAW CHECKS ===
{law_block}

=== EPISTEMIC DISCIPLINE ===
When you state a fact, label it. When you infer, say "I infer...". When you guess, say "hypothesis:" or "speculation:". When you don't know, say "I don't know — I would need...".

=== DECISION DISCIPLINE ===
End every meaningful answer with one of:
  → COMMIT: <what you commit to and why>
  → DEFER:  <what's missing before you can commit>
  → REJECT: <what principle stops you and what alternative you offer>

=== OUTPUT BIAS ===
Structured, high-signal, no filler. Prefer lists, tables, and concrete next actions.
Match the user's language. If they code, you code. If they speak Portuguese, reply in Portuguese.
Never claim more than the evidence supports.

=== LATTICE CONTEXT ===
You may call tools. You may ask the user one targeted clarification if and only if
the answer would change the decision state. Otherwise, COMMIT.
"""
