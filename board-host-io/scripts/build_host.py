"""Run host-board synthesis, implementation and bitstream serially under the EDA lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "scripts"))
sys.path.insert(0, str(SKILLS / "scripts" / "line"))
sys.path.insert(0, str(SKILLS / "vivado-manager" / "scripts"))
from line.identity import compare_reuse_identity  # noqa: E402
from run_line import Lock  # noqa: E402
from stage_rtl import stage_rtl  # noqa: E402


def require_run_local_sources(wrapper: dict, run_dir: Path) -> None:
    for key in ("source_wrapper", "core_verilog", "model_path"):
        path = Path(wrapper.get(key, "")).resolve()
        if not path.is_relative_to(run_dir):
            raise ValueError(f"{key} must come from the same run as the host board")


def require_host_sim(
    path: Path, contract_hash: str, core_hash: str, model_hash: str, rtl_hashes: dict[str, str]
) -> None:
    if not path.is_file():
        raise ValueError("host simulation evidence is missing")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_sha256": contract_hash,
        "core_verilog_sha256": core_hash,
        "model_sha256": model_hash,
    }
    if (
        evidence.get("status") != "PASSED"
        or any(evidence.get(key) != value for key, value in expected.items())
        or not evidence.get("requested_samples")
        or evidence.get("received_samples") != evidence.get("requested_samples")
        or evidence.get("staged_rtl") != rtl_hashes
        or not evidence.get("python", {}).get("passed")
    ):
        raise ValueError("host simulation did not pass for this contract, model and core")


def host_sim_diagnostic(path, *identity):
    """Simulation is optional context, never authorization or board numerical proof."""
    if not path.is_file():
        return {"status": "NOT_RUN", "passed": None}
    try:
        require_host_sim(path, *identity)
    except (ValueError, OSError, AttributeError, TypeError) as exc:
        return {"status": "INVALID_OR_FAILED", "passed": False, "reason": str(exc)}
    return {"status": "PASSED", "passed": True}


STAGES = ("synth", "impl", "bitstream")


def prepare_continuation(deployment, board, target, start_stage, prior_evidence):
    """Clone a same-run checkpoint into a fresh attempt; never overwrite its history."""
    deployment, board, target, prior_evidence = (
        Path(p).resolve() for p in (deployment, board, target, prior_evidence)
    )
    run = deployment.parents[2]
    if target.exists():
        raise FileExistsError(target)
    if start_stage not in {"impl", "bitstream"}:
        raise ValueError("continuation starts at impl or bitstream")
    if (
        not board.is_relative_to(run)
        or not target.is_relative_to(run)
        or target.is_relative_to(board)
        or not prior_evidence.is_relative_to(run)
    ):
        raise ValueError("continuation must use a distinct destination in the same run")
    previous = "synth" if start_stage == "impl" else "impl"
    checkpoint = "synth.dcp" if start_stage == "impl" else "routed.dcp"
    record = json.loads(prior_evidence.read_text(encoding="utf-8"))

    def digest(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    if (
        record.get("status") != "PASSED"
        or record.get("stage") != "vivado_" + previous
        or Path(record.get("board_dir", "")).resolve() != board
        or record.get("contract", {}).get("sha256") != digest(deployment)
    ):
        raise ValueError("predecessor evidence does not identify this stage, board and contract")
    artifact = board / checkpoint
    expected = record.get("artifacts", {}).get(checkpoint, {}).get("sha256")
    if not expected or not artifact.is_file() or digest(artifact) != expected:
        raise ValueError("predecessor checkpoint is missing or changed")
    files = {p.name: digest(p) for p in (board / "rtl").glob("*") if p.is_file()}
    if not files or files != record.get("rtl_used", {}).get("files"):
        raise ValueError("predecessor RTL or constraints changed")
    # Stage only the clone: source reconciliation may write, and the original is evidence.
    # Failed later checkpoints may exist. They stay in the source attempt, but
    # must not masquerade as completed outputs in the new attempt.
    ignored = {"board_top.bit"}
    if start_stage == "impl":
        ignored.add("routed.dcp")
    shutil.copytree(
        board,
        target,
        ignore=lambda directory, names: (
            ignored.intersection(names) if Path(directory) == board else set()
        ),
    )
    wrapper = json.loads((target / "wrapper.json").read_text(encoding="utf-8"))
    staged = stage_rtl(target, wrapper)
    if staged["files"] != files:
        raise ValueError("current sources differ; retain clone and regenerate affected stage")
    identity = compare_reuse_identity(
        record.get("staged_rtl", {}).get("reuse_identity"), staged.get("reuse_identity")
    )
    if (
        identity["core_identity_matches"] is not True
        or identity["wrapper_identity_matches"] is not True
    ):
        raise ValueError(
            "checkpoint reuse identity changed or unknown; retain clone and inspect dependencies"
        )
    marker = {
        "source_board": str(board),
        "start_stage": start_stage,
        "predecessor_evidence": str(prior_evidence),
        "predecessor_evidence_sha256": digest(prior_evidence),
        "checkpoint": checkpoint,
        "checkpoint_sha256": expected,
        "reuse_identity_comparison": identity,
        "hardware_verified": False,
    }
    (target / "continuation.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return target


def execute_stages(run_stage, stages=STAGES) -> list[dict]:
    results = []
    for stage in stages:
        result = run_stage(stage)
        results.append({"stage": stage, **result})
        if result.get("status") != "PASSED":
            raise ValueError(f"host board {stage} did not pass; stopped before later EDA stages")
    return results


def build(
    deployment: Path,
    board: Path,
    python: Path,
    *,
    start_stage="synth",
    prior_evidence=None,
    continuation_board=None,
) -> list[dict]:
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
    if (
        wrapper.get("model_sha256")
        != hashlib.sha256(Path(wrapper["model_path"]).read_bytes()).hexdigest()
    ):
        raise ValueError("model file changed after host board generation")
    if (
        wrapper.get("core_verilog_sha256")
        != hashlib.sha256(Path(wrapper["core_verilog"]).read_bytes()).hexdigest()
    ):
        raise ValueError("core RTL changed after host board generation")
    if (
        wrapper.get("source_wrapper_sha256")
        != hashlib.sha256(Path(wrapper["source_wrapper"]).read_bytes()).hexdigest()
    ):
        raise ValueError("source wrapper changed after host board generation")
    if start_stage != "synth":
        if prior_evidence is None or continuation_board is None:
            raise ValueError("continuation needs predecessor evidence and a fresh board directory")
        board = prepare_continuation(
            deployment, board, continuation_board, start_stage, prior_evidence
        )
    stages = STAGES[STAGES.index(start_stage) :]
    prefix = "host-vivado" if start_stage == "synth" else board.name + "-vivado"
    evidence_dir = run_dir / "evidence"
    current_rtl = stage_rtl(board, wrapper)["files"]
    simulation = host_sim_diagnostic(
        evidence_dir / "host-sim" / "host-sim-evidence.json",
        wrapper["contract_sha256"],
        wrapper["core_verilog_sha256"],
        wrapper["model_sha256"],
        current_rtl,
    )
    for stage, artifact in (
        ("synth", "synth.dcp"),
        ("impl", "routed.dcp"),
        ("bitstream", "board_top.bit"),
    ):
        if stage not in stages:
            continue
        if (board / artifact).exists() or (
            evidence_dir / f"{prefix}-{stage}-evidence.json"
        ).exists():
            raise ValueError(f"host {stage} artifact already exists; create a new bootstrap run")

    def run_stage(stage):
        evidence_path = evidence_dir / f"{prefix}-{stage}-evidence.json"
        command = [
            str(python),
            str(SKILLS / "vivado-manager" / "scripts" / "run_vivado.py"),
            "--stage",
            stage,
            "--board",
            str(board),
            "--deployment",
            str(deployment),
            "--work",
            str(
                board.parent
                / ("host-vivado-logs" if start_stage == "synth" else board.name + "-vivado-logs")
                / stage
            ),
            "--evidence",
            str(evidence_path),
        ]
        completed = subprocess.run(command, cwd=run_dir)
        if completed.returncode or not evidence_path.is_file():
            return {
                "status": "FAILED",
                "returncode": completed.returncode,
                "evidence": str(evidence_path),
            }
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        return {
            "status": evidence.get("status", "FAILED"),
            "returncode": completed.returncode,
            "evidence": str(evidence_path),
        }

    with Lock(False):
        return [
            {**result, "host_sim_diagnostic": simulation}
            for result in execute_stages(run_stage, stages)
        ]


def build_with_odatix(
    deployment,
    board,
    python,
    odatix,
    output,
    *,
    start_stage="synth",
    prior_evidence=None,
    continuation_board=None,
):
    """Wrap the existing host build while keeping its run-local evidence and lock."""
    from tempfile import TemporaryDirectory

    from odatix_batch import run_batch, write_json

    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    interpreter = shutil.which(str(python))
    if interpreter is None:
        raise FileNotFoundError(f"host build interpreter not found: {python}")
    interpreter = str(Path(interpreter).resolve())
    command = [
        interpreter,
        str(Path(__file__).resolve()),
        "--deployment",
        str(Path(deployment).resolve()),
        "--board",
        str(Path(board).resolve()),
        "--python",
        interpreter,
        "--start-stage",
        start_stage,
    ]
    if prior_evidence is not None:
        command += ["--prior-evidence", str(Path(prior_evidence).resolve())]
    if continuation_board is not None:
        command += ["--continuation-board", str(Path(continuation_board).resolve())]
    with TemporaryDirectory(prefix="host-batch-", dir=output.parent) as temporary:
        payload = Path(temporary)
        write_json(payload / "command.json", command)
        (payload / "invoke.py").write_text(
            "import json\nimport os\nfrom pathlib import Path\nimport subprocess\n"
            "command = json.loads(Path('command.json').read_text(encoding='utf-8'))\n"
            "subprocess.run(command, check=True, env={**os.environ, 'S15_ODATIX_WORKER': '1'})\n"
            "Path('host-build-complete.json').write_text('{\"completed\": true}')\n",
            encoding="utf-8",
        )
        return run_batch(
            {
                "entrypoint": "invoke.py",
                "expected_outputs": ["host-build-complete.json"],
                "candidates": [{"id": "host-build", "args": [], "independent": True}],
            },
            payload,
            output,
            odatix,
        )


def workflow_default(board: Path) -> tuple[Path | None, Path | None]:
    executable = os.environ.get("S15_ODATIX_EXE")
    if not executable or os.environ.get("S15_ODATIX_WORKER") == "1":
        return None, None
    return Path(executable), board.resolve() / f"odatix-{uuid4().hex[:12]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--start-stage", choices=STAGES, default="synth")
    parser.add_argument("--prior-evidence", type=Path)
    parser.add_argument("--continuation-board", type=Path)
    parser.add_argument("--odatix", type=Path, help="optional Odatix 4.0 executable")
    parser.add_argument("--odatix-output", type=Path, help="fresh Odatix workspace directory")
    args = parser.parse_args()
    if (args.odatix is None) != (args.odatix_output is None):
        parser.error("--odatix and --odatix-output must be supplied together")
    if args.odatix is None:
        args.odatix, args.odatix_output = workflow_default(args.board)
    if args.odatix is not None:
        batch = build_with_odatix(
            args.deployment,
            args.board,
            args.python,
            args.odatix,
            args.odatix_output,
            start_stage=args.start_stage,
            prior_evidence=args.prior_evidence,
            continuation_board=args.continuation_board,
        )
        print(json.dumps(batch, indent=2))
        return 0 if batch["status"] == "success" else 1
    results = build(
        args.deployment,
        args.board,
        args.python,
        start_stage=args.start_stage,
        prior_evidence=args.prior_evidence,
        continuation_board=args.continuation_board,
    )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
