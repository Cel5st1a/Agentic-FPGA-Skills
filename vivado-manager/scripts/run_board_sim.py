#!/usr/bin/env python
"""vivado-manager: board-level per-value verification.

The board wrapper captures one result word set per sample and folds them into a kept
checksum; its testbench reads those captured words hierarchically and writes them as
signed raw integers on the contract's own fixed-point grid. This script runs that
simulation, converts the C++ reference log to the same grid and compares per value
under the line's single criterion.

A run that produces no comparable results is NOT_RUN, never PASSED: a simulation that
finished is not a numerical verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import sys
import yaml

LINE_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(LINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LINE_SCRIPTS))

from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from line.identity import identity_fields  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_rtl import stage_rtl  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
DEFAULT_VIVADO = Path(r"D:\vitis\2025.2\Vivado\bin\vivado.bat")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def render(template: Path, values: dict) -> str:
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    if "{{" in text:
        raise SystemExit(f"unrendered placeholder in {template.name}")
    return text


def _kill_tree(pid: int) -> None:
    if hasattr(subprocess, "run"):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)


def run_tool(tool: Path, script: Path, work: Path, timeout: int) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    argv = (["cmd", "/c", str(tool)] if tool.suffix.lower() in {".bat", ".cmd"} else [str(tool)])
    argv += ["-mode", "batch", "-source", str(script), "-nojournal", "-notrace"]
    stdout_path, stderr_path = work / "stdout.txt", work / "stderr.txt"
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8", errors="replace") as out, \
            stderr_path.open("w", encoding="utf-8", errors="replace") as err:
        process = subprocess.Popen(argv, cwd=str(script.parent), stdout=out, stderr=err)
        try:
            returncode, timed_out = process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, None
            _kill_tree(process.pid)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
    duration = round(time.monotonic() - started, 2)
    (work / "command.json").write_text(json.dumps(
        {"argv": argv, "timeout_s": timeout, "returncode": returncode,
         "timed_out": timed_out, "duration_s": duration}, indent=2), encoding="utf-8")
    return {"argv": argv, "returncode": returncode, "timed_out": timed_out,
            "duration_s": duration,
            "stdout": stdout_path.read_text(encoding="utf-8", errors="replace")}


def read_ints(path: Path) -> list[list[int]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append([int(float(v)) for v in line.split()])
    return rows


def read_reals(path: Path) -> list[list[float]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append([float(v) for v in line.split()])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True, help="HLS C++ results log")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--vivado", type=Path, default=DEFAULT_VIVADO)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    board = args.board.resolve()
    wrapper = json.loads((board / "wrapper.json").read_text(encoding="utf-8"))
    contract = yaml.safe_load(args.deployment.resolve().read_text(encoding="utf-8-sig"))
    criterion = criterion_from_deployment(contract)
    # The board log holds the output stream's own element, which is not always the
    # contract's precision (measured on this line: an output stream typed narrower, with
    # more fractional bits, than the contract's precision). Scaling it on the contract grid
    # would divide every value by the wrong power of two.
    grid = wrapper.get("output_grid") or wrapper["rom_image"]["grid"]
    scale = float(2 ** (grid["total_bits"] - grid["integer_bits"]))

    values = {
        "PROJECT_NAME": f"{wrapper['top']}_board",
        "PART": wrapper["part"],
        "TOP": "board_top",
        "TB": "board_top_tb.v",
        "TB_TOP": "board_top_tb",
        "RTL_DIR": (board / "rtl").as_posix(),
        "XDC": (board / "rtl" / "board.xdc").as_posix(),
        "THREADS": args.threads,
    }
    # A stale simulation project makes the elaborate step fail with a Windows spawn error
    # ("not a valid Win32 application"), so every run starts from a clean sim project.
    # Vivado copies `glbl.v` in read-only, so the removal has to clear that attribute.
    sim_project = board / "sim"
    if sim_project.exists():
        import stat as stat_module

        def _on_error(function, target, exc_info):
            os.chmod(target, stat_module.S_IWRITE)
            function(target)

        shutil.rmtree(sim_project, onerror=_on_error)

    sim_run = board / "sim_run.tcl"
    sim_run.write_text(
        (TEMPLATES / "sim_run.tcl").read_text(encoding="utf-8").replace(
            "{{RTL_DIR}}", (board / "rtl").as_posix()
        ),
        encoding="utf-8",
    )
    values["SIM_RUN_TCL"] = sim_run.as_posix()
    script = board / "simulate.tcl"
    script.write_text(render(TEMPLATES / "simulate.tcl", values), encoding="utf-8")

    # A stale result file from an earlier run must never be read as this run's output.
    for stale in board.rglob("board_results.log"):
        stale.unlink()

    # The simulator reads <board>/rtl/*.v. Stage it here instead of depending on an
    # earlier synthesis run having left it behind: that dependency made the first board
    # simulation of a fresh design fail with "no files matched glob .../rtl/*.v".
    staged_rtl = stage_rtl(board, wrapper)

    execution = run_tool(args.vivado, script, args.work.resolve(), args.timeout)
    evidence: dict = {
        **(identity_fields(args.deployment, (wrapper.get("board") or {}).get("pins_profile")) if args.deployment else {}),
        "schema_version": 1,
        "stage": "board_sim",
        "skill": "vivado-manager",
        "board_dir": str(board),
        "contract": {"path": str(args.deployment.resolve()),
                     "sha256": sha256_file(args.deployment.resolve())},
        "tool": {"argv": execution["argv"], "returncode": execution["returncode"],
                 "timed_out": execution["timed_out"], "duration_s": execution["duration_s"]},
        "status": "NOT_RUN",
        "artifacts": {},
        "staged_rtl": staged_rtl,
        "notes": [
            "the testbench reads the captured result words hierarchically; the board top "
            "exposes only board-legal pins",
        ],
    }
    if execution["timed_out"]:
        evidence["status"] = "BUDGET_EXHAUSTED"
        evidence["reasons"] = [f"simulation exceeded {args.timeout}s"]
    elif execution["returncode"] != 0:
        evidence["status"] = "FAILED"
        evidence["reasons"] = [f"tool exit code {execution['returncode']}"]
    else:
        evidence["status"] = "PASSED"

    produced = sorted(board.glob("sim/*.sim/sim_1/behav/xsim/board_results.log"))
    if not produced:
        produced = sorted(board.rglob("board_results.log"))
    if not produced:
        evidence["status"] = "FAILED" if evidence["status"] == "PASSED" else evidence["status"]
        evidence.setdefault("reasons", []).append("simulation produced no board_results.log")
    else:
        source = produced[0]
        target = args.work.resolve() / "board_results.log"
        shutil.copyfile(source, target)
        evidence["artifacts"]["board_results"] = {
            "file": target.name, "sha256": sha256_file(target),
            "source_path": source.relative_to(board).as_posix(),
        }
        raw = read_ints(target)
        reference = read_reals(args.reference)
        # How many values one sample carries: the ap_vld wrapper lists one port per result,
        # while the AXI-Stream wrapper states the beat count, because a stream port has no
        # per-value names.
        interface = wrapper["interface"]
        if interface.get("result_outputs"):
            expected = len(interface["result_outputs"])
        else:
            expected = int((interface.get("stream") or {}).get("output_elements") or 0)
        if not expected:
            raise SystemExit(
                f"wrapper.json does not say how many values one sample has: {interface}"
            )
        evidence["columns"] = {"expected": expected,
                               "board": sorted({len(row) for row in raw}),
                               "reference": sorted({len(row) for row in reference})}
        evidence["rows"] = {"board": len(raw), "reference": len(reference)}
        if not raw or any(len(row) != expected for row in raw) or not reference or \
                any(len(row) != expected for row in reference):
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append(
                f"column count mismatch: wrapper declares {expected} result outputs, "
                f"board wrote {evidence['columns']['board']}, reference has "
                f"{evidence['columns']['reference']}"
            )
            args.evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.evidence.resolve().write_text(
                json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"stage": evidence["stage"], "status": evidence["status"],
                              "columns": evidence["columns"],
                              "reasons": evidence.get("reasons")}, indent=2, ensure_ascii=False))
            return 0
        if len(raw) != len(reference):
            # A run that simulated fewer samples than the gate expects has not compared the
            # whole reference; recording that as a clean FAILED beats comparing row k of one
            # against row k of the other and crashing on the shape later.
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append(
                f"sample count mismatch: the reference has {len(reference)} rows, the board "
                f"wrote {len(raw)}"
            )
            args.evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.evidence.resolve().write_text(
                json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"stage": evidence["stage"], "status": evidence["status"],
                              "rows": evidence["rows"],
                              "reasons": evidence.get("reasons")}, indent=2, ensure_ascii=False))
            return 0
        actual = [[value / scale for value in row] for row in raw]
        reference_int = [[int(round(value * scale)) for value in row] for row in reference]
        gate = compare(
            "board_sim", reference, actual, criterion,
            reference_source=str(args.reference),
            actual_source=evidence["artifacts"]["board_results"]["source_path"],
        )
        gate["bit_exact_on_the_grid"] = reference_int == raw
        gate["grid"] = {"total_bits": grid["total_bits"], "integer_bits": grid["integer_bits"],
                        "scale": scale}
        evidence["board_gate"] = gate
        if evidence["status"] == "PASSED" and not gate["passed"]:
            evidence["status"] = "FAILED"
            counted = gate.get("values") or {}
            if "violations" in counted and "total" in counted:
                detail = (f"{counted['violations']}/{counted['total']} board values outside "
                          "tolerance")
            else:
                # compare() can refuse before counting anything (shape/criterion problems);
                # carrying its own reasons is honest, and inventing a count would not be.
                detail = "no per-value comparison: " + "; ".join(
                    gate.get("reasons") or ["unspecified"])
            evidence.setdefault("reasons", []).append("board gate: " + detail)

    args.evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.evidence.resolve().write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "stage": evidence["stage"], "status": evidence["status"],
        "returncode": execution["returncode"], "duration_s": execution["duration_s"],
        "board_gate": {k: evidence["board_gate"][k]
                       for k in ("passed", "values", "max_abs_error", "max_error_over_tolerance",
                                 "bit_exact_on_the_grid")
                       if k in evidence.get("board_gate", {})} if "board_gate" in evidence else None,
        "reasons": evidence.get("reasons"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
