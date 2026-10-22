#!/usr/bin/env python
"""Query the workflow memory written by `record_workflow.py`.

Entries live in `<this skill>/examples/*.json`. An entry recorded from a run carries the
measured gates, so the query can tell a real pass from a tool that merely exited 0: the
printout shows violations and the margin (max error / tolerance) next to each verdict.

Usage:
  python query_workflows.py [--framework keras] [--status success] [--vivado 2025.2]
                            [--board <board-id>] [--part <fpga-part>]
                            [--examples <dir>] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLES = SKILL_DIR / "examples"


def load_entries(examples: Path) -> list[dict]:
    entries = []
    for path in sorted(examples.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        payload["_path"] = str(path)
        entries.append(payload)
    return entries


def matches(entry: dict, filters: dict) -> bool:
    for key, wanted in filters.items():
        if wanted is None:
            continue
        if key in {"vivado", "vitis", "hls4ml", "tensorflow"}:
            actual = (entry.get("config") or {}).get(key)
        elif key in {"model_type", "framework"}:
            field = "type" if key == "model_type" else "framework"
            actual = (entry.get("model") or {}).get(field)
        else:
            actual = entry.get(key)
        if actual is None or str(wanted).lower() not in str(actual).lower():
            return False
    return True


def gate_line(entry: dict, name: str) -> str:
    measured = (entry.get("measured") or {}).get("gates") or {}
    gate = measured.get(name) or {}
    if not gate:
        return "-"
    if name == "resource":
        return f"{gate.get('worst')}={gate.get('worst_fraction')} limit={gate.get('limit')}"
    if name == "timing":
        return f"WNS={gate.get('wns_ns')} met={gate.get('met')}"
    return (
        f"violations={gate.get('violations')}/{gate.get('values_compared')} "
        f"margin={gate.get('max_error_over_tolerance')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    parser.add_argument("--framework", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--vivado", default=None)
    parser.add_argument("--hls4ml", default=None)
    parser.add_argument("--board", default=None)
    parser.add_argument("--part", default=None)
    parser.add_argument("--model-type", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    filters = {
        "framework": args.framework,
        "status": args.status,
        "vivado": args.vivado,
        "hls4ml": args.hls4ml,
        "board": args.board,
        "part": args.part,
        "model_type": args.model_type,
    }
    entries = [entry for entry in load_entries(args.examples) if matches(entry, filters)]
    entries.sort(key=lambda entry: entry.get("timestamp") or "", reverse=True)
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0 if entries else 1

    print("=== Workflow Memory Query ===")
    print("Query: " + ", ".join(f"{k}={v}" for k, v in filters.items() if v))
    print(f"\nFound {len(entries)} matching workflow(s) in {args.examples}\n")
    for index, entry in enumerate(entries, 1):
        model = entry.get("model") or {}
        results = entry.get("results") or {}
        print(f"{index}. {entry.get('project')} ({(entry.get('timestamp') or '')[:10]}) status={entry.get('status')}")
        print(
            f"   {entry.get('board')} / {entry.get('part')} | "
            f"precision {model.get('precision')} rf={model.get('reuse_factor')} "
            f"{model.get('io_type')} {model.get('strategy')}"
        )
        print(
            "   csim={csim} csynth={csynth} cosim={cosim} impl={impl} board_sim={board_sim}".format(
                **{k: results.get(k, "NOT_RUN") for k in ("csim", "csynth", "cosim", "impl", "board_sim")}
            )
        )
        if entry.get("run"):
            print(f"   run={entry['run']} dir={(entry.get('measured') or {}).get('run_dir')}")
            print(f"   gate HLS C++ vs python : {gate_line(entry, 'hls_cpp_vs_python')}")
            print(f"   gate RTL vs HLS C++    : {gate_line(entry, 'rtl_vs_hls_cpp')}")
            print(f"   resource gate          : {gate_line(entry, 'resource')}")
            print(f"   timing                 : {gate_line(entry, 'timing')}")
        print()

    successes = [entry for entry in entries if entry.get("status") == "success"]
    if successes:
        best = successes[0]
        model = best.get("model") or {}
        print("Most recent success recommends:")
        print(f"  precision: {model.get('precision')}")
        print(f"  reuse_factor: {model.get('reuse_factor')}  strategy: {model.get('strategy')}  io_type: {model.get('io_type')}")
        print(f"  board/part: {best.get('board')} / {best.get('part')}")
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
