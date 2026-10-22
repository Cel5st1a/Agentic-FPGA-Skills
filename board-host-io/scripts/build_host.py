"""Run host-board synthesis, implementation and bitstream serially under the EDA lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "scripts" / "line"))
sys.path.insert(0, str(SKILLS / "vivado-manager" / "scripts"))
from run_line import Lock  # noqa: E402
from stage_rtl import stage_rtl  # noqa: E402


def require_run_local_sources(wrapper: dict, run_dir: Path) -> None:
    for key in ("source_wrapper", "core_verilog", "model_path"):
        path = Path(wrapper.get(key, "")).resolve()
        if not path.is_relative_to(run_dir):
            raise ValueError(f"{key} must come from the same run as the host board")


def require_host_sim(path: Path, contract_hash: str, core_hash: str, model_hash: str,
                     rtl_hashes: dict[str, str]) -> None:
    if not path.is_file():
        raise ValueError("host simulation evidence is missing")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    expected = {"contract_sha256": contract_hash, "core_verilog_sha256": core_hash,
                "model_sha256": model_hash}
    if (evidence.get("status") != "PASSED"
            or any(evidence.get(key) != value for key, value in expected.items())
            or not evidence.get("requested_samples")
            or evidence.get("received_samples") != evidence.get("requested_samples")
            or evidence.get("staged_rtl") != rtl_hashes
            or not evidence.get("python", {}).get("passed")
            or not evidence.get("hls", {}).get("passed")):
        raise ValueError("host simulation did not pass for this contract, model and core")


def execute_stages(run_stage) -> list[dict]:
    results = []
    for stage in ("synth", "impl", "bitstream"):
        result = run_stage(stage)
        results.append({"stage": stage, **result})
        if result.get("status") != "PASSED":
            raise ValueError(f"host board {stage} did not pass; stopped before later EDA stages")
    return results


def build(deployment: Path, board: Path, python: Path) -> list[dict]:
    deployment, board = Path(deployment).resolve(), Path(board).resolve()
    run_dir = deployment.parents[2]
    if not board.is_relative_to(run_dir):
        raise ValueError("host board must live in the deployment's one-contract run")
    wrapper = json.loads((board / "wrapper.json").read_text(encoding="utf-8"))
    if wrapper.get("skill") != "board-host-io" or not wrapper.get("host_input_output_available"):
        raise ValueError("board is not a generated host-I/O variant")
    require_run_local_sources(wrapper, run_dir)
    if wrapper.get("contract_sha256") != hashlib.sha256(deployment.read_bytes()).hexdigest():
        raise ValueError("board wrapper belongs to a different deployment contract")
    if wrapper.get("model_sha256") != hashlib.sha256(Path(wrapper["model_path"]).read_bytes()).hexdigest():
        raise ValueError("model file changed after host board generation")
    if wrapper.get("core_verilog_sha256") != hashlib.sha256(Path(wrapper["core_verilog"]).read_bytes()).hexdigest():
        raise ValueError("core RTL changed after host board generation")
    if wrapper.get("source_wrapper_sha256") != hashlib.sha256(Path(wrapper["source_wrapper"]).read_bytes()).hexdigest():
        raise ValueError("source wrapper changed after host board generation")
    evidence_dir = run_dir / "evidence"
    current_rtl = stage_rtl(board, wrapper)["files"]
    require_host_sim(evidence_dir / "host-sim" / "host-sim-evidence.json",
                     wrapper["contract_sha256"], wrapper["core_verilog_sha256"],
                     wrapper["model_sha256"], current_rtl)
    for stage, artifact in (("synth", "synth.dcp"), ("impl", "routed.dcp"),
                            ("bitstream", "board_top.bit")):
        if (board / artifact).exists() or (evidence_dir / f"host-vivado-{stage}-evidence.json").exists():
            raise ValueError(f"host {stage} artifact already exists; create a new bootstrap run")

    def run_stage(stage):
        evidence_path = evidence_dir / f"host-vivado-{stage}-evidence.json"
        command = [str(python), str(SKILLS / "vivado-manager" / "scripts" / "run_vivado.py"),
                   "--stage", stage, "--board", str(board), "--deployment", str(deployment),
                   "--work", str(board), "--evidence", str(evidence_path)]
        completed = subprocess.run(command, cwd=run_dir)
        if completed.returncode or not evidence_path.is_file():
            return {"status": "FAILED", "returncode": completed.returncode,
                    "evidence": str(evidence_path)}
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        return {"status": evidence.get("status", "FAILED"),
                "returncode": completed.returncode, "evidence": str(evidence_path)}

    with Lock(False):
        return execute_stages(run_stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    results = build(args.deployment, args.board, args.python)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
