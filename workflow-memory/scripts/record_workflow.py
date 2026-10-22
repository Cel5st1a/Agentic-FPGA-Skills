#!/usr/bin/env python
"""Record a successful FPGA+AI workflow for future reuse.

Two modes:

  * free form (the original CLI): every field is named on the command line.
  * `--run <run-dir>`: measurements are READ FROM THE RUN'S OWN EVIDENCE FILES, so a
    recorded entry cannot claim a gate that the run did not actually pass. In this
    mode `--status success` is refused unless the hard gates (HLS C++ vs python,
    synthesised RTL vs HLS C++, routed resource gate, timing) all passed.

Entries are written to `<this skill>/examples/`, next to the skill that produced them,
so the memory travels with the skills working copy. `--out-dir` overrides that.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = SKILL_DIR / "examples"

HARD_GATES = (
    ("csim-evidence.json", ("numerical_gate", "passed")),
    ("board-sim-evidence.json", ("board_gate", "passed")),
    ("vivado-impl-evidence.json", ("resource_gate", "passed")),
    ("vivado-impl-evidence.json", ("timing", "met")),
)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def dig(node: dict, keys: tuple[str, ...]):
    current = node
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def safe_shape(raw: str):
    """Parse `(16,)` / `[100, 16]` without eval()."""
    if not raw:
        return None
    text = raw.strip().strip("()[]")
    if not text or text == ",":
        return []
    try:
        return [int(part) for part in text.replace(" ", "").split(",") if part]
    except ValueError:
        return raw


def from_run(run_dir: Path) -> tuple[dict, list[str]]:
    """Measured facts, read from the run's evidence. Never inferred."""
    evidence_dir = run_dir / "evidence"
    starting = load_json(run_dir / "starting-state.json") or {}
    evidence = {
        name: load_json(evidence_dir / name)
        for name in {
            "convert-evidence.json",
            "csim-evidence.json",
            "csynth-evidence.json",
            "cosim-evidence.json",
            "board-sim-evidence.json",
            "vivado-synth-evidence.json",
            "vivado-impl-evidence.json",
            "vivado-bitstream-evidence.json",
        }
    }
    refusals = []
    for name, keys in HARD_GATES:
        node = evidence.get(name)
        if node is None:
            refusals.append(f"missing evidence/{name}")
        elif dig(node, keys) is not True:
            refusals.append(f"evidence/{name}: {'.'.join(keys)} is not True")

    parameters = starting.get("deployment_parameters") or {}
    csim = evidence.get("csim-evidence.json") or {}
    board = evidence.get("board-sim-evidence.json") or {}
    impl = evidence.get("vivado-impl-evidence.json") or {}
    bit = evidence.get("vivado-bitstream-evidence.json") or {}
    convert = evidence.get("convert-evidence.json") or {}

    def gate(node: dict, key: str) -> dict:
        block = node.get(key) or {}
        return {
            "passed": block.get("passed"),
            "criterion": block.get("criterion"),
            "violations": (block.get("values") or {}).get("violations"),
            "values_compared": (block.get("values") or {}).get("total"),
            "max_abs_error": block.get("max_abs_error"),
            "max_error_over_tolerance": block.get("max_error_over_tolerance"),
            "reference_source": block.get("reference_source"),
            "actual_source": block.get("actual_source"),
        }

    facts = {
        "run": run_dir.name,
        "run_dir": str(run_dir),
        "contract_path_in_run": starting.get("contract_path_in_run"),
        "contract_sha256": starting.get("contract_sha256"),
        "deployment_parameters": parameters,
        "model": {
            "files": starting.get("model_files"),
            "format": None,
            "input_shape_used": convert.get("input_shape_used"),
        },
        "results": {
            "convert": "PASSED" if convert else "NOT_RUN",
            "csim": csim.get("status", "NOT_RUN"),
            "csynth": (evidence.get("csynth-evidence.json") or {}).get("status", "NOT_RUN"),
            "cosim": (evidence.get("cosim-evidence.json") or {}).get("status", "NOT_RUN"),
            "impl": impl.get("status", "NOT_RUN"),
            "bitstream": bit.get("status", "NOT_RUN"),
            "board_sim": board.get("status", "NOT_RUN"),
            "programmed": False,
            "hardware_verified": False,
        },
        "gates": {
            "hls_cpp_vs_python": gate(csim, "numerical_gate"),
            "rtl_vs_hls_cpp": gate(board, "board_gate"),
            "resource": impl.get("resource_gate"),
            "timing": impl.get("timing"),
        },
        "resources": impl.get("resources"),
        "bitstream": bit.get("artifacts", {}).get("bitstream"),
        "evidence_files": {
            name: ({"sha256": sha256_file(evidence_dir / name)} if (evidence_dir / name).is_file() else None)
            for name in sorted(evidence)
        },
    }
    return facts, refusals


def main() -> int:
    parser = argparse.ArgumentParser(description="Record FPGA+AI workflow")
    parser.add_argument("--project", required=True)
    parser.add_argument("--status", default="success")
    parser.add_argument("--run", type=Path, default=None,
                        help="run directory: record the measured evidence instead of free-form fields")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--vivado", default="2025.2")
    parser.add_argument("--vitis", default="2025.2")
    parser.add_argument("--python", default="3.10.11")
    parser.add_argument("--tf", default="2.13.1")
    parser.add_argument("--hls4ml", default="1.3.0")
    parser.add_argument("--qkeras", default="0.9.0")
    parser.add_argument("--keras-type", default="v2")
    parser.add_argument("--protobuf", default="3.20.3")
    parser.add_argument("--numpy", default="1.24.3")
    parser.add_argument("--model-type", default=None)
    parser.add_argument("--framework", default="Keras")
    parser.add_argument("--layers", type=int, default=None)
    parser.add_argument("--input-shape", default=None)
    parser.add_argument("--precision", default=None)
    parser.add_argument("--reuse-factor", type=int, default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--csim", default="NOT_RUN")
    parser.add_argument("--csynth", default="NOT_RUN")
    parser.add_argument("--cosim", default="NOT_RUN")
    parser.add_argument("--impl", default="NOT_RUN")
    parser.add_argument("--hls-written", type=bool, default=False)
    parser.add_argument("--backend", default="Vivado")
    parser.add_argument("--board", default=None)
    parser.add_argument("--part", default=None)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    entry = {
        "timestamp": datetime.now(CST).isoformat(timespec="seconds"),
        "project": args.project,
        "status": args.status,
        "config": {
            "vivado": args.vivado,
            "vitis": args.vitis,
            "python": args.python,
            "tensorflow": args.tf,
            "hls4ml": args.hls4ml,
            "qkeras": args.qkeras,
            "keras_type": args.keras_type,
            "protobuf": args.protobuf,
            "numpy": args.numpy,
        },
        "model": {
            "type": args.model_type,
            "framework": args.framework,
            "layers": args.layers,
            "input_shape": safe_shape(args.input_shape) if args.input_shape else None,
            "precision": args.precision,
            "reuse_factor": args.reuse_factor,
            "strategy": args.strategy,
        },
        "results": {
            "csim": args.csim,
            "csynth": args.csynth,
            "cosim": args.cosim,
            "impl": args.impl,
            "hls_code_written": args.hls_written,
        },
        "backend": args.backend,
        "board": args.board,
        "part": args.part,
        "notes": args.notes,
    }

    if args.run:
        run_dir = args.run.resolve()
        facts, refusals = from_run(run_dir)
        parameters = facts["deployment_parameters"] or {}
        entry["measured"] = facts
        entry["model"].update({
            "precision": parameters.get("precision"),
            "reuse_factor": parameters.get("reuse_factor"),
            "strategy": parameters.get("strategy"),
            "io_type": parameters.get("io_type"),
            "input_shape": facts["model"]["input_shape_used"],
        })
        entry["results"] = facts["results"]
        entry["run"] = facts["run"]
        if args.status == "success" and refusals:
            print(
                json.dumps(
                    {"recorded": False, "refusals": refusals,
                     "why": "a success entry is only recorded when every hard gate passed"},
                    indent=2,
                )
            )
            return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"workflow_{args.project}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"recorded": True, "path": str(path), "status": entry["status"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
