#!/usr/bin/env python
"""Recommend a starting deployment configuration from the workflow memory.

Answers the question the memory exists for: "what worked last time for this kind of
model?" It only ever recommends configurations whose entry carries measured gates, and
it refuses to recommend an entry that did not pass them.

Usage:
  python find_best_config.py --model-type cnn --framework keras [--board AX7A200B]
                             [--examples <dir>] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from query_workflows import DEFAULT_EXAMPLES, gate_line, load_entries  # noqa: E402


def passed(entry: dict) -> bool:
    """A recommendation requires a recorded success AND all four gates present and true.

    An absent gate is `NOT_RUN`, never `PASSED`: an entry with `status: failure` or one
    that never recorded a gate must not seed a new conversion.
    """
    if entry.get("status") != "success":
        return False
    gates = ((entry.get("measured") or {}).get("gates")) or {}
    required = ("hls_cpp_vs_python", "rtl_vs_hls_cpp", "resource", "timing")
    if any(not gates.get(name) for name in required):
        return False
    for name in ("hls_cpp_vs_python", "rtl_vs_hls_cpp", "resource"):
        if gates[name].get("passed") is not True:
            return False
    return gates["timing"].get("met") is True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-type", default=None)
    parser.add_argument("--framework", default=None)
    parser.add_argument("--board", default=None)
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    entries = load_entries(args.examples)

    def relevant(entry: dict) -> bool:
        model = entry.get("model") or {}
        if args.model_type and (model.get("type") or "").lower() != args.model_type.lower():
            return False
        if args.framework and (model.get("framework") or "").lower() != args.framework.lower():
            return False
        if args.board and (entry.get("board") or "").lower() != args.board.lower():
            return False
        return True

    candidates = [entry for entry in entries if relevant(entry)]
    verified = [entry for entry in candidates if passed(entry)]
    verified.sort(key=lambda entry: entry.get("timestamp") or "", reverse=True)

    if not verified:
        print(
            json.dumps(
                {
                    "found": False,
                    "candidates": len(candidates),
                    "why": "no entry with measured gates that all passed; a conversion must not "
                           "be seeded from an unverified config",
                },
                indent=2,
            )
        )
        return 1

    best = verified[0]
    model = best.get("model") or {}
    recommendation = {
        "found": True,
        "project": best.get("project"),
        "run": best.get("run"),
        "run_dir": (best.get("measured") or {}).get("run_dir"),
        "precision": model.get("precision"),
        "reuse_factor": model.get("reuse_factor"),
        "strategy": model.get("strategy"),
        "io_type": model.get("io_type"),
        "board": best.get("board"),
        "part": best.get("part"),
        "gates": {
            "hls_cpp_vs_python": gate_line(best, "hls_cpp_vs_python"),
            "rtl_vs_hls_cpp": gate_line(best, "rtl_vs_hls_cpp"),
            "resource": gate_line(best, "resource"),
            "timing": gate_line(best, "timing"),
        },
        "also_verified": [entry.get("project") for entry in verified[1:]],
    }
    print(json.dumps(recommendation, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
