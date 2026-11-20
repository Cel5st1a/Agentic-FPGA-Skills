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
from run_line import board_pin_profile, model_frontend, resolve_interpreter  # noqa: E402

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
    parser.add_argument(
        "--model-id",
        default=None,
        help="directory under <root> that holds the run (defaults to the contract package name)",
    )
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
    model = contract["model"]
    frontend = model_frontend(model)

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

    for stage in (
        "p1-env",
        "p2-convert",
        "p3-build",
        "p4-analysis",
        "p5-package",
        "evidence",
        "logs",
    ):
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
        "contract_byte_identical_to_source": sha256_file(relocated_contract)
        == sha256_file(contract_path),
        "package_dir_in_run": str(contract_copy_dir),
        "shared_assets_copied": shared_copied,
        "model_files": model_files,
        "verification_files": verification_files,
        "board_profile": board_profile_record,
        "skills_root": str(skills),
        "board_profile_sha256": sha256_file(resolved) if board_profile_record else None,
        "board_pins_profile_sha256": sha256_file(
            board_pin_profile(contract, relocated_contract, skills)
        )
        if board_profile_record
        else None,
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
            (3, ("vitis-build-agent", "vivado-manager", "board-host-io")),
            (4, ("resource-estimator", "timing-closure-debugger", "log-analyzer")),
            (5, ("board-host-io", "bitstream-package", "workflow-memory")),
        )
    )
    contract_rel = relocated_contract.relative_to(run_dir).as_posix()
    # The conversion command depends on the model frontend, not on the model: an ONNX
    # contract needs the ONNX converter and an interpreter that has qonnx. For a `.keras`
    # archive the *interpreter* additionally depends on the archive's own Keras major
    # version -- a Keras 3 container cannot be deserialised by Keras 2.13 (defect D-N01) --
    # so it is resolved from the model file rather than assumed from the frontend. `run_line.py`
    # does the same by default and accepts an explicit `--python` override.
    conversion_env = {
        "keras_json_weights": ("convert_keras.py", "Path A (Keras)"),
        "qkeras_json_weights": ("convert_keras.py", "Path A (QKeras)"),
        "keras_h5": ("convert_keras.py", "Path A (Keras .h5)"),
        "keras_archive": ("convert_keras.py", "Path A (Keras archive)"),
        "onnx": ("convert_onnx.py", "Path B (ONNX)"),
    }
    if frontend in conversion_env:
        converter_script, path_label = conversion_env[frontend]
        converter_python = resolve_interpreter(contract, contract_copy_dir / model["path"])
        conversion_block = (
            f"# P2 - conversion (hls4ml-convert {path_label}, frontend `{frontend}`)\n"
            f'& "{converter_python}" `\n'
            f'  "{skills / "hls4ml-convert" / "scripts" / converter_script}" `\n'
            '  --deployment $contract --output (Join-Path $run "p2-convert\\project")'
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
| contract in run | `{contract_rel}` (sha256 `{starting_state["contract_sha256"]}`) |
| package dir in run | `{starting_state["package_dir_in_run"].split("runs")[-1].lstrip(chr(92))}` |
| skills root | `{skills}` |
| model frontend | `{frontend}` |
| precision | `{contract.get("precision")}` |
| reuse_factor | `{contract.get("reuse_factor")}` |
| io_type / strategy | `{contract.get("io_type")}` / `{contract.get("strategy")}` |
| clock_period_ns | `{contract.get("clock_period_ns")}` |

## Deployment stages

See `{skills}\\ORDER.md` for the full order, the frozen criterion and the stage list.

| phase | skills |
| --- | --- |
{skill_layout}

## UART deployment

Set $py to the conversion environment for the contract frontend. For authorized
hardware tasks, use the observed USB bridge port and JTAG cable for the board.

For a fresh UART deployment, start the hardware server and use the observed $port.
See {skills / "board-host-io/SKILL.md"} for the board and cable checks.

```powershell
& $py "{skills / "scripts/line/run_uart.py"}" `
  --deployment "{relocated_contract}" --run "{run_dir}" --python $py --port $port
```

Use the individual stages below to inspect or resume a failed attempt.

```powershell
$run = "{run_dir}"
$contract = Join-Path $run "{contract_rel.replace("/", chr(92))}"
$hostTools = "{skills / "board-host-io/scripts"}"
$board = Join-Path $run "p3-build/host-board"
$audit = Join-Path $run "evidence/host-port-audit"
$bit = Join-Path $board "board_top.bit"

& $py "{skills / "scripts/line/run_line.py"}" `
  --deployment $contract --run $run --skills "{skills}" --python $py `
  --stages convert,csynth,wrapper
& $py "$hostTools/make_host_board.py" `
  --source-wrapper "$run/p3-build/board/wrapper.json" --output $board
& $py "$hostTools/build_host.py" --deployment $contract --board $board --python $py
& $py "$hostTools/audit_ports.py" --wrapper "$board/wrapper.json" `
  --routed-dcp "$board/routed.dcp" --output $audit
```

Start the hardware server and set the observed $cable and $port using
{skills / "board-host-io/SKILL.md"}. Check each step before proceeding.

```powershell
& $py "$hostTools/program_jtag.py" --bitstream $bit --io-scope "$audit/io-scope.json" `
  --bitstream-evidence "$run/evidence/host-vivado-bitstream-evidence.json" `
  --output "$run/hardware-program" --cable-serial $cable
& $py "$hostTools/verify_board.py" --deployment $contract --board $board --bitstream $bit `
  --io-scope "$audit/io-scope.json" --program-summary "$run/hardware-program/summary.json" `
  --port $port --output "$run/hardware-validation"
```

## Targeted diagnostics

```powershell

# P1 - environment audit (the skills' own scripts)
bash "{(skills / "fpga-env-audit/scripts/audit_system.sh").as_posix()}"
bash "{(skills / "fpga-env-audit/scripts/audit_python_env.sh").as_posix()}"
bash "{(skills / "compatibility-checker/scripts/check_compat.sh").as_posix()}"

{conversion_block}

```

## Evidence contract

Every stage writes evidence JSON under `evidence/` and raw logs under `logs/`.
The original UART capture and `hardware-validation/summary.json` record the
result against the original-model Python reference for all supplied inputs.
""",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "contract_in_run": str(relocated_contract),
                "contract_sha256": starting_state["contract_sha256"],
                "contract_byte_identical_to_source": starting_state[
                    "contract_byte_identical_to_source"
                ],
                "board_profile_resolves": bool(
                    board_profile_record and board_profile_record["resolves_inside_run"]
                ),
                "missing_verification_files": [
                    k for k, v in verification_files.items() if v.get("missing")
                ],
                "deployment_parameters": starting_state["deployment_parameters"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
