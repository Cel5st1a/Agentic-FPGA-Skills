"""Materialise an identical starting state for one conversion attempt.

Design rules this file must keep:
  * Generic. Everything comes from the deployment contract; there is no table of
    model names, precisions or package paths in this file.
  * Memoryless. A fresh agent runs this once and gets byte-identical inputs, the
    same directory layout and the same RUN.md as any other attempt with the same
    contract, so two runs are comparable.
  * Self-contained. The contract's own relative references (`model.path`,
    `associated_files`, `verification.*`, `board_profile`) must resolve inside the
    run directory, so the layout is copied rather than flattened.
  * Refuse to overwrite. A run directory is created once; re-running against an
    existing run is an error, never a silent merge.

Usage:
  python bootstrap_run.py --deployment <deployment.yaml> --run <run-id> [--root <dir>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import yaml

# The Keras-version probe lives in run_line.py, which resolves the interpreter at run time.
# `bootstrap_run` writes the same choice into RUN.md, and the two must not drift: a RUN.md
# that names the wrong interpreter is followed literally by a memoryless agent. Importing
# the one implementation is how they stay in agreement.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_line import detect_keras_major, board_pin_profile  # noqa: E402

SKILLS = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = Path(r"D:\ML_models\_line\runs")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_contract(path: Path) -> dict:
    # Contracts in this project may carry a UTF-8 BOM.
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def iter_tree(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--run", required=True, help="run id, e.g. 20260927-01")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--skills", type=Path, default=SKILLS)
    parser.add_argument("--model-id", default=None,
                        help="directory under <root> that holds the run (defaults to the contract package name)")
    parser.add_argument(
        "--with-shared-assets",
        default=True,
        help="also copy the package-level sibling directory named public/ when present",
    )
    args = parser.parse_args()
    skills = args.skills.resolve()

    contract_path = args.deployment.resolve()
    contract_dir = contract_path.parent
    package_root = contract_dir.parent
    contract = read_contract(contract_path)

    model_id = args.model_id or contract_dir.name
    run_dir = (args.root / model_id / args.run).resolve()
    if run_dir.exists():
        raise SystemExit(f"run already exists, refusing to overwrite: {run_dir}")

    # Layout is preserved: the relocated contract must resolve its own relative
    # references without rewriting a single byte of it.
    contract_copy_dir = run_dir / "contract" / contract_dir.name
    contract_copy_dir.parent.mkdir(parents=True)
    shutil.copytree(contract_dir, contract_copy_dir)
    relocated_contract = contract_copy_dir / contract_path.name

    shared_copied = None
    if args.with_shared_assets:
        shared = package_root / "public"
        if shared.is_dir():
            shutil.copytree(shared, run_dir / "contract" / "public")
            shared_copied = "public"

    for stage in ("p1-env", "p2-convert", "p3-build", "p4-analysis", "p5-package", "evidence", "logs"):
        (run_dir / stage).mkdir()

    model_files = {}
    for relative in [contract["model"]["path"], *(contract["model"].get("associated_files") or [])]:
        source = (contract_copy_dir / relative).resolve()
        if not source.is_file():
            raise SystemExit(f"model file does not resolve inside the run: {relative}")
        model_files[Path(relative).as_posix()] = {
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
        }

    verification_files = {}
    for key in ("inputs", "references", "labels"):
        entry = (contract.get("verification") or {}).get(key)
        if not entry:
            continue
        source = (contract_copy_dir / entry).resolve()
        if source.is_file():
            verification_files[key] = {
                "relative": entry,
                "sha256": sha256_file(source),
                "bytes": source.stat().st_size,
            }
        else:
            verification_files[key] = {"relative": entry, "missing": True}

    board_profile = contract.get("board_profile")
    board_profile_record = None
    if board_profile:
        resolved = (contract_copy_dir / board_profile).resolve()
        board_profile_record = {
            "relative": board_profile,
            "resolves_inside_run": resolved.is_file(),
            "path": str(resolved),
        }

    starting_state = {
        "schema_version": 1,
        "model_id": model_id,
        "run": args.run,
        "original_contract_path": str(contract_path),
        "contract_path_in_run": str(relocated_contract),
        "contract_sha256": sha256_file(relocated_contract),
        "contract_byte_identical_to_source": sha256_file(relocated_contract) == sha256_file(contract_path),
        "package_dir_in_run": str(contract_copy_dir),
        "shared_assets_copied": shared_copied,
        "model_files": model_files,
        "verification_files": verification_files,
        "board_profile": board_profile_record,
        "skills_root": str(skills),
        "board_profile_sha256": sha256_file(resolved) if board_profile_record else None,
        "board_pins_profile_sha256": sha256_file(
            board_pin_profile(contract, relocated_contract, skills)) if board_profile_record else None,
        "deployment_parameters": {
            key: contract.get(key)
            for key in (
                "precision",
                "reuse_factor",
                "layer_reuse_factors",
                "top_dataflow",
                "io_type",
                "strategy",
                "clock_period_ns",
                "backend",
            )
        },
        "verification": contract.get("verification"),
        "constraints": contract.get("constraints"),
    }
    (run_dir / "starting-state.json").write_text(
        json.dumps(starting_state, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    skill_layout = "\n".join(
        f"| P{phase} | {', '.join(names)} |"
        for phase, names in (
            (1, ("fpga-env-audit", "compatibility-checker")),
            (2, ("tensorflow-to-fpga", "hls4ml-convert")),
            (3, ("vitis-build-agent", "vivado-manager")),
            (4, ("resource-estimator", "timing-closure-debugger", "log-analyzer")),
            (5, ("bitstream-package", "workflow-memory", "zynq-deploy (NOT run: no flashing)")),
        )
    )
    contract_rel = relocated_contract.relative_to(run_dir).as_posix()
    # The conversion command depends on the model frontend, not on the model: an ONNX
    # contract needs the ONNX converter and an interpreter that has qonnx. For a `.keras`
    # archive the *interpreter* additionally depends on the archive's own Keras major
    # version -- a Keras 3 container cannot be deserialised by Keras 2.13 (defect D-N01) --
    # so it is resolved from the model file rather than assumed from the frontend. `run_line.py`
    # does the same, and `--python` overrides both.
    frontend = (contract.get("model") or {}).get("frontend")
    keras_major = None
    model_rel = (contract.get("model") or {}).get("path")
    if model_rel:
        keras_major = detect_keras_major((contract_copy_dir / model_rel).resolve())
    keras3_python = r"D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe"
    legacy_python = r"D:\python_envs\hls4ml_conv\Scripts\python.exe"
    conversion_env = {
        "keras_json_weights": (legacy_python, "convert_keras.py", "Path A (Keras)"),
        "qkeras_json_weights": (legacy_python, "convert_keras.py", "Path A (QKeras)"),
        "keras_h5": (legacy_python, "convert_keras.py", "Path A (Keras .h5)"),
        "keras_archive": (legacy_python, "convert_keras.py", "Path A (Keras archive)"),
        "onnx": (r"D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe", "convert_onnx.py", "Path B (ONNX)"),
    }
    if frontend in conversion_env:
        converter_python, converter_script, path_label = conversion_env[frontend]
        if frontend in {"keras_archive", "keras_h5"} and keras_major == 3:
            converter_python = keras3_python
            path_label += ", Keras 3 archive -> Keras 3 env"
        conversion_block = (
            f"# P2 - conversion (hls4ml-convert {path_label}, frontend `{frontend}`)\n"
            f"& \"{converter_python}\" `\n"
            f'  "{skills / "hls4ml-convert" / "scripts" / converter_script}" `\n'
            "  --deployment $contract --output (Join-Path $run \"p2-convert\\project\")"
        )
    else:
        conversion_block = (
            f"# P2 - no documented converter for frontend `{frontend}`; see\n"
            "# hls4ml-convert/SKILL.md and report the gap instead of improvising."
        )

    (run_dir / "RUN.md").write_text(
        f"""# Run {args.run} - {model_id}

Starting state produced by `bootstrap_run.py`. The contract copy and the model /
verification files are byte-identical for any attempt with the same contract, so a
memoryless agent starts from the same inputs every time. `RUN.md` and
`starting-state.json` themselves carry this run's own paths.

| item | value |
| --- | --- |
| contract in run | `{contract_rel}` (sha256 `{starting_state['contract_sha256']}`) |
| package dir in run | `{starting_state['package_dir_in_run'].split('runs')[-1].lstrip(chr(92))}` |
| skills root | `{skills}` |
| model frontend | `{frontend}` |
| precision | `{contract.get('precision')}` |
| reuse_factor | `{contract.get('reuse_factor')}` |
| io_type / strategy | `{contract.get('io_type')}` / `{contract.get('strategy')}` |
| clock_period_ns | `{contract.get('clock_period_ns')}` |

## Stage order (13 skills)

See `{skills}\\ORDER.md` for the full order, the frozen criterion and the stage list.

| phase | skills |
| --- | --- |
{skill_layout}

## Commands (fresh shell, absolute paths only)

```powershell
$run = "{run_dir}"
$contract = Join-Path $run "{contract_rel.replace('/', chr(92))}"

# P1 - environment audit (the skills' own scripts)
bash "{(skills / "fpga-env-audit/scripts/audit_system.sh").as_posix()}"
bash "{(skills / "fpga-env-audit/scripts/audit_python_env.sh").as_posix()}"
bash "{(skills / "compatibility-checker/scripts/check_compat.sh").as_posix()}"

{conversion_block}

# P3-P5 - drive the rest of the line (stages are checked against their evidence)
& D:\\python_envs\\hls4ml_conv\\Scripts\\python.exe `
  "{skills / "scripts/line/run_line.py"}" `
  --deployment $contract --run $run --skills "{skills}" `
  --stages csim,csynth,wrapper,boardsim,synth,impl,bitstream,ports,package
```

## Evidence contract

Every stage writes evidence JSON under `evidence/` and raw logs under `logs/`.
A stage that produced no evidence is `NOT_RUN`, never `PASSED`.
No programming, no on-board inference.
""",
        encoding="utf-8",
    )

    print(json.dumps({
        "run_dir": str(run_dir),
        "contract_in_run": str(relocated_contract),
        "contract_sha256": starting_state["contract_sha256"],
        "contract_byte_identical_to_source": starting_state["contract_byte_identical_to_source"],
        "board_profile_resolves": bool(board_profile_record and board_profile_record["resolves_inside_run"]),
        "missing_verification_files": [k for k, v in verification_files.items() if v.get("missing")],
        "deployment_parameters": starting_state["deployment_parameters"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
