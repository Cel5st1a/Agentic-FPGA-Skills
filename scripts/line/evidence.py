"""Per-value numerical evidence for one stage.

Rules this module enforces (operator principle 2: a memoryless agent must get the
same answer, and must not be able to claim a pass it did not measure):

  * A stage reports PASSED only when every compared value is inside tolerance.
  * Insufficient sample coverage is a failure with a reason, never a silent pass.
  * Shape mismatch with a different element count, non-finite values and an empty
    comparison are failures with reasons.
  * The reference and the actual values are hashed into the evidence so a later
    reader can tell whether the numbers behind a verdict changed.
"""

from __future__ import annotations

import hashlib
import json
import math

from .contract import Criterion, tolerance_for

SCHEMA_VERSION = 1
MAX_REPORTED_INDICES = 20


def _digest(values) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), default=float).encode("utf-8")
    ).hexdigest()


def _flatten(value) -> list[float]:
    """Flatten arbitrarily nested sample data in C order into scalars."""
    out: list[float] = []

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        else:
            out.append(float(node))

    walk(value)
    return out


def _as_rows(values):
    """One row per sample, one column per scalar output.

    A model whose outputs are images delivers each sample as a nested array
    ((32, 32, 1) or (2, 16, 16)), so a sample row has to be flattened at *any* depth:
    the earlier two-level version raised `TypeError: float() argument must be ... not
    'list'` for every convolutional or auto-encoder contract, which meant no evidence
    was written at all for those models.

    A single-output model delivers its values as one flat vector. Reading that as a
    single sample would make every coverage requirement fail, so it is read as one
    sample per value - deterministically, and recorded (see `compare`).

    Rows are never dropped, however empty: dropping them would silently reduce the
    compared sample count and re-pair reference and actual rows. `compare` turns an
    empty row into a named failure instead. Non-numeric data raises `ValueError`, which
    `compare` also converts into a reason rather than a traceback.
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"comparison values must be a list, got {type(values).__name__}")
    if not values:
        return []
    if isinstance(values[0], (list, tuple)):
        return [_flatten(sample) for sample in values]
    return [[float(value) for value in values]]


def _file_sha256(source) -> str | None:
    """Hash of the file a value set was read from, when the caller named one."""
    from pathlib import Path

    if not source:
        return None
    path = Path(source)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def compare(
    stage: str,
    reference,
    actual,
    criterion: Criterion,
    *,
    reference_source: str | None = None,
    actual_source: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Compare two same-shaped value sets under one criterion and return evidence."""
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "criterion": criterion.to_dict(),
        "reference_source": reference_source,
        "actual_source": actual_source,
        # These two are digests of the canonicalised VALUES, not of the files; the file
        # hashes are recorded separately so a reader is not misled.
        "reference_sha256": _digest(reference),
        "actual_sha256": _digest(actual),
        "reference_sha256_kind": "canonical_values",
        "actual_sha256_kind": "canonical_values",
        "reference_file_sha256": _file_sha256(reference_source),
        "actual_file_sha256": _file_sha256(actual_source),
        "reasons": [],
        "passed": False,
    }
    if extra:
        evidence["extra"] = extra

    try:
        reference_rows = _as_rows(reference)
        actual_rows = _as_rows(actual)
    except (TypeError, ValueError) as error:
        evidence["reasons"].append(f"non-numeric comparison data: {error}")
        return evidence
    if not reference_rows or not actual_rows:
        evidence["reasons"].append("empty comparison")
        return evidence
    if any(not row for row in reference_rows) or any(not row for row in actual_rows):
        evidence["reasons"].append(
            "empty sample row in the comparison: "
            f"reference widths {[len(row) for row in reference_rows]}, "
            f"actual widths {[len(row) for row in actual_rows]}"
        )
        return evidence
    # A single-output model delivers its values as one flat vector. Reading that as a
    # single sample would make every coverage requirement fail, so it is read as one
    # sample per value -- deterministically, and recorded.
    if len(reference_rows) == 1 and criterion.samples > 1 and len(reference_rows[0]) == criterion.samples:
        reference_rows = [[value] for value in reference_rows[0]]
        actual_rows = [[value] for value in actual_rows[0]] if len(actual_rows) == 1 else actual_rows
        evidence["flat_values_read_as"] = "one_sample_per_value"
    if len(reference_rows) != len(actual_rows) or any(
        len(a) != len(b) for a, b in zip(reference_rows, actual_rows)
    ):
        evidence["reasons"].append(
            "size mismatch: reference rows "
            f"{[len(row) for row in reference_rows]} vs actual rows "
            f"{[len(row) for row in actual_rows]}"
        )
        return evidence
    if any(not math.isfinite(value) for row in reference_rows + actual_rows for value in row):
        evidence["reasons"].append("non-finite value in comparison")
        return evidence

    tolerances = tolerance_for(criterion, reference_rows)
    errors = [
        [abs(ref - act) for ref, act in zip(ref_row, act_row)]
        for ref_row, act_row in zip(reference_rows, actual_rows)
    ]
    flat_errors = [error for row in errors for error in row]
    flat_tolerances = [tol for row in tolerances for tol in row]
    violations = [
        index
        for index, (error, tol) in enumerate(zip(flat_errors, flat_tolerances))
        if error > tol
    ]
    margins = [error / tol for error, tol in zip(flat_errors, flat_tolerances)]
    per_sample = [max(row_errors) / max(row_tol) for row_errors, row_tol in zip(errors, tolerances)]

    # The *declared* sample count. When the contract pinned one it is the requirement and
    # the data must match it; when it did not, the criterion carries DEFAULT_SAMPLES and
    # the data itself is the only honest requirement. Reporting the pinned number as
    # `expected` while the data is larger is what let a 256-row run advertise 100 samples
    # and still pass (incident report B-EVID-1).
    pinned = criterion.samples_source != "code_default"
    compared = len(reference_rows)
    expected = criterion.samples if pinned else compared
    evidence["samples"] = {
        "expected": expected,
        "compared": compared,
        "declared_source": criterion.samples_source,
    }
    evidence["values"] = {
        "total": len(flat_errors),
        "violations": len(violations),
        "first_failing_indices": violations[:MAX_REPORTED_INDICES],
    }
    evidence["max_abs_error"] = max(flat_errors)
    evidence["max_error_over_tolerance"] = max(margins)
    evidence["per_sample_pass_fraction"] = sum(1 for m in per_sample if m <= 1.0) / len(per_sample)
    if pinned and compared < expected:
        evidence["reasons"].append(
            f"sample coverage {compared} < required {expected}"
        )
    elif pinned and compared > expected:
        evidence["reasons"].append(
            f"declared verification.samples={expected} contradicts the compared data: "
            f"{compared} samples were measured, more than the {expected} the contract "
            "pins. Coverage is a floor, so nothing was skipped, but the contract must be "
            "re-pinned to the dataset it names."
        )
    evidence["passed"] = not evidence["reasons"] and not violations
    return evidence


def write_evidence(path, evidence: dict) -> str:
    """Persist evidence and return its SHA-256."""
    text = json.dumps(evidence, indent=2, ensure_ascii=False)
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
