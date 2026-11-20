"""Complete numerical comparison and selection for an explicitly requested workflow."""

import hashlib
import itertools
import json
import math

import numpy as np

LEVELS = ("G1", "G2", "G3", "G4")


def effective_identity(project):
    from odatix_batch import file_digest

    firmware = project / "firmware"
    files = [
        firmware / "defines.h",
        firmware / "parameters.h",
        *sorted(firmware.glob("*.cpp")),
        *(
            p
            for folder in ("weights", "nnet_utils")
            for p in sorted((firmware / folder).rglob("*"))
            if p.is_file()
        ),
    ]
    record = [[p.relative_to(firmware).as_posix(), file_digest(p)] for p in files]
    return hashlib.sha256(json.dumps(record).encode()).hexdigest()


def compare_levels(levels):
    if set(levels) != set(LEVELS):
        raise ValueError("all four numerical levels are required")
    arrays = {key: np.asarray(value, dtype=np.float64) for key, value in levels.items()}
    first = arrays["G1"]
    if (
        first.ndim != 2
        or not first.size
        or any(row.shape != first.shape or not np.isfinite(row).all() for row in arrays.values())
    ):
        raise ValueError("four-level shape, coverage or finiteness differs")
    pairs = {}
    for left, right in itertools.combinations(LEVELS, 2):
        expected, actual = arrays[left], arrays[right]
        errors = np.abs(actual - expected)
        ratios = errors / (0.001 + 0.001 * np.abs(expected))
        indices = np.argwhere(ratios > 1)
        pairs[f"{left}-{right}"] = {
            "values": first.size,
            "violations": len(indices),
            "max_abs_error": float(errors.max()),
            "max_tolerance_ratio": float(ratios.max()),
            "first_differences": [
                {
                    "sample": int(i),
                    "output": int(j),
                    "expected": float(expected[i, j]),
                    "actual": float(actual[i, j]),
                    "abs_error": float(errors[i, j]),
                }
                for i, j in indices[:20]
            ],
        }
    return {
        "complete": True,
        "pass": all(p["violations"] == 0 for p in pairs.values()),
        "shape": list(first.shape),
        "pairs": pairs,
    }


def choose(candidates, fronts=None):
    eligible = [
        r
        for r in candidates
        if r.get("comparison", {}).get("complete")
        and r["comparison"].get("pass")
        and r.get("clock_mhz") == 100
        and all(
            type(r.get(k)) in (int, float) and math.isfinite(r[k]) and r[k] >= 0
            for k in ("wns_ns", "whs_ns", "adapter_cycles")
        )
        and r.get("physical_pass", True)
    ]
    if len({r.get("effective_sha256", r["configuration_sha256"]) for r in eligible}) < 2:
        raise ValueError("two distinct fully measured four-level candidates are required")
    identifiers = {row["id"] for row in eligible}
    if fronts is not None and set(itertools.chain.from_iterable(fronts)) != identifiers:
        raise ValueError("native Odatix ranking does not cover the measured candidates")
    groups = fronts if fronts is not None else [list(identifiers)]
    rows = {row["id"]: row for row in eligible}
    order = [
        key
        for front in groups
        for key in sorted(
            front,
            key=lambda key: (
                rows[key]["adapter_cycles"],
                rows[key]["resources"]["dsp"],
                rows[key]["resources"]["lut"],
                key,
            ),
        )
    ]
    return {
        "selected": order[0],
        "ranking": order,
        "rule": "Odatix Pareto fronts then measured cycles, DSP, LUT and ID",
        "eligible": len(eligible),
    }
