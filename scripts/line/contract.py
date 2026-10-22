"""The single source of truth for numerical acceptance in this skill line.

Operator principle (2026-09-27): the line must stay generic. Therefore this module
hardcodes no tolerance of its own — every threshold is read from the deployment
contract. What it does fix is the *shape* of a criterion:

  * `four_effective_output_lsbs` - the frozen rule
        |out - py| <= max(floor, 4 * 2^-(W-I)) + rtol * |py|
    where W and I come from the contract's `precision`.
  * `range_relative` - the earlier protocol rule
        |out - py| <= max(rho * max(ptp, rms), 4 * 2^-(W-I))

A stage may not invent its own threshold: import `criterion_from_deployment` and use
the returned object. Two different rules giving two different verdicts on the same
artifacts is exactly the defect this module removes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RULE_FOUR_LSB = "four_effective_output_lsbs"
RULE_RANGE = "range_relative"
RULES = (RULE_FOUR_LSB, RULE_RANGE)

#: Absolute floor of the frozen rule, in output units. The operator's rule uses
#: 0.001 when 4 * 2^-(W-I) is smaller (e.g. ap_fixed<24,6> -> 1.5e-5).
DEFAULT_FLOOR = 0.001
#: Relative term of the frozen rule.
DEFAULT_RTOL = 0.01
#: Fraction of the dynamic range used by the earlier protocol rule.
DEFAULT_RHO = 0.10
#: Fallback sample count, used only when a contract does not declare
#: `verification.samples`. A criterion resolved from that fallback reports
#: `samples_source = "code_default"` so a reader can tell a pinned count from this one:
#: the earlier version reported `source = "contract.verification"` for both, which is how
#: a 256-row dataset came to advertise 100 samples while passing (incident report
#: B-EVID-1).
DEFAULT_SAMPLES = 100


@dataclass(frozen=True)
class Criterion:
    """A resolved acceptance criterion. Immutable so no stage can mutate it."""

    rule: str
    atol: float
    rtol: float = DEFAULT_RTOL
    rho: float = DEFAULT_RHO
    floor: float = DEFAULT_FLOOR
    samples: int = DEFAULT_SAMPLES
    source: str = "contract"
    #: Where the threshold came from. A criterion whose `rule_source` is
    #: "code_default" is not pinned by the contract, and a reader can see that.
    rule_source: str = "code_default"
    atol_source: str = "derived_from_precision"
    #: Where `samples` came from. "contract.verification.samples" when the contract
    #: pinned it; "code_default" when it fell back to DEFAULT_SAMPLES. Carried for the
    #: same reason as `rule_source`: an unpinned count must not look pinned.
    samples_source: str = "code_default"

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "rule_source": self.rule_source,
            "atol": self.atol,
            "atol_source": self.atol_source,
            "rtol": self.rtol,
            "rho": self.rho if self.rule == RULE_RANGE else None,
            "floor": self.floor,
            "samples": self.samples,
            "samples_source": self.samples_source,
            "source": self.source,
        }


def effective_lsb(precision: str) -> float:
    """The output LSB of an ap_fixed<W,I> declaration."""
    body = precision.strip()
    if body.startswith("ap_fixed<") and body.endswith(">"):
        body = body[len("ap_fixed<") : -1]
    total, integer = (int(part) for part in body.split(","))
    if total <= 0 or integer > total:
        raise ValueError(f"invalid fixed-point precision: {precision}")
    return 2.0 ** -(total - integer)


def criterion_from_deployment(contract: dict) -> Criterion:
    """Resolve the criterion a contract declares.

    `verification.rule` selects the rule; the remaining fields carry the thresholds.
    Absent fields fall back to the rule's own definition so an old contract still
    resolves to the rule it names, never to a different one.
    """
    verification = contract.get("verification") or {}
    declared_rule = verification.get("rule")
    rule = declared_rule or RULE_FOUR_LSB
    if rule not in RULES:
        raise ValueError(f"unknown numerical rule: {rule}")
    precision = contract.get("precision")
    if not precision:
        raise ValueError("contract has no precision; the LSB bound cannot be derived")
    lsb_bound = 4 * effective_lsb(precision)
    declared_atol = verification.get("atol")
    if declared_atol is None:
        atol, atol_source = lsb_bound, "derived_from_precision"
    else:
        atol, atol_source = float(declared_atol), "contract.verification.atol"
        # The rule's name must mean its formula. A contract that keeps the frozen label
        # while declaring a different absolute bound would silently change the criterion,
        # so it is refused instead of honoured.
        if rule == RULE_FOUR_LSB and abs(atol - lsb_bound) > 1e-12:
            raise ValueError(
                f"verification.atol={atol} contradicts rule '{RULE_FOUR_LSB}': "
                f"for precision {precision} the frozen LSB term is {lsb_bound}. "
                "Declare the value the rule defines, or declare a different rule."
            )
    declared_samples = verification.get("samples")
    return Criterion(
        rule=rule,
        atol=atol,
        rtol=float(verification.get("rtol", DEFAULT_RTOL)),
        rho=float(verification.get("rho", DEFAULT_RHO)),
        floor=float(verification.get("floor", DEFAULT_FLOOR)),
        samples=int(declared_samples if declared_samples is not None else DEFAULT_SAMPLES),
        source="contract.verification",
        rule_source="contract.verification.rule" if declared_rule else "code_default",
        atol_source=atol_source,
        samples_source=(
            "contract.verification.samples"
            if declared_samples is not None
            else "code_default"
        ),
    )


def _as_rows(values) -> list[list[float]]:
    if isinstance(values, (list, tuple)) and values and isinstance(values[0], (list, tuple)):
        return [list(map(float, row)) for row in values]
    return [[float(value) for value in values]]


def tolerance_for(criterion: Criterion, reference) -> list[list[float]]:
    """Per-value tolerance for the given reference, in the reference's own shape."""
    rows = _as_rows(reference) or [[0.0]]
    if criterion.rule == RULE_FOUR_LSB:
        base = max(criterion.floor, criterion.atol)
        return [[base + criterion.rtol * abs(value) for value in row] for row in rows]
    tolerances = []
    for row in rows:
        spread = (max(row) - min(row)) if row else 0.0
        rms = math.sqrt(sum(value * value for value in row) / len(row)) if row else 0.0
        tolerances.append([max(criterion.rho * max(spread, rms), criterion.atol)] * len(row))
    return tolerances
