#!/usr/bin/env python
"""vitis-build-agent: run one Vitis HLS stage of an hls4ml project.

The SKILL.md documents the four stages (csim / csynth / cosim / impl) and the
report parsing step. The stage is selected by rewriting the option block that
hls4ml's own `build_prj.tcl` sources (`build_opt.tcl`), then invoking

    vitis-run --mode hls --tcl build_prj.tcl

which is the invocation this project has actually used on Vitis 2025.2. The form
written in the SKILL.md (`--csim --work_dir ...`) is not accepted by vitis-run; the
deviation is recorded in the evidence as `invocation_note` so the doc gap is
visible instead of silently patched.

Design rules (operator principles, 2026-09-27): generic (stage, project, tool and
reference all come from arguments/contract), memoryless (absolute paths, one
command per stage, evidence JSON next to the logs), and no acceptance claim of its
own: when `--reference` and `--deployment` are supplied the verdict comes from
`scripts/line/evidence.py`.

Usage:
  python run_stage.py --stage csim --project <dir> --work <execution-dir>
                      [--deployment <yaml>] [--reference <json>] [--evidence <json>]
                      [--vitis-run <path>] [--timeout <seconds>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

LINE_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(LINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LINE_SCRIPTS))

from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from line.identity import identity_fields  # noqa: E402

OPT_BLOCK = """array set opt {{
    reset      {reset}
    csim       {csim}
    synth      {synth}
    cosim      {cosim}
    validation {validation}
    export     {export}
    vsynth     {vsynth}
    fifo_opt   0
}}
"""

#: Which `build_opt.tcl` flags each stage needs. cosim needs the synthesised RTL;
#: export and the bundled Vivado synthesis both need csynth output too.
STAGE_FLAGS = {
    "csim": {"csim": 1},
    "csynth": {"synth": 1},
    "cosim": {"synth": 1, "cosim": 1, "validation": 1},
    "export": {"synth": 1, "export": 1},
    "vsynth": {"synth": 1, "vsynth": 1},
}
DEFAULT_VITIS_RUN = Path(r"D:\vitis\2025.2\Vitis\bin\vitis-run.bat")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def project_name(project: Path) -> str:
    text = (project / "project.tcl").read_text(encoding="utf-8")
    match = re.search(r'set project_name\s+"([^"]+)"', text)
    if match is None:
        raise SystemExit("generated project.tcl does not declare project_name")
    return match.group(1)


def write_options(project: Path, flags: dict) -> dict:
    options = {"reset": 0, "csim": 0, "synth": 0, "cosim": 0, "validation": 0, "export": 0, "vsynth": 0}
    options.update(flags)
    (project / "build_opt.tcl").write_text(OPT_BLOCK.format(**options), encoding="utf-8")
    return options


def _kill_tree(pid: int) -> None:
    """Kill a tool and its children.

    Vitis and Vivado launch grandchildren that keep the build alive after the launcher
    exits. With captured pipes that also blocks the reader forever, which is why this
    runner writes tool output straight to files and kills the whole tree on timeout.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:  # pragma: no cover - the project's toolchain is Windows
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)


def run_tool(tool: Path, project: Path, work: Path, timeout: int) -> dict:
    if tool.suffix.lower() in {".bat", ".cmd"}:
        argv = ["cmd", "/c", str(tool), "--mode", "hls", "--tcl", "build_prj.tcl"]
    else:
        argv = [str(tool), "--mode", "hls", "--tcl", "build_prj.tcl"]
    work.mkdir(parents=True, exist_ok=True)
    stdout_path = work / "stdout.txt"
    stderr_path = work / "stderr.txt"
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8", errors="replace") as out, \
            stderr_path.open("w", encoding="utf-8", errors="replace") as err:
        process = subprocess.Popen(argv, cwd=str(project), stdout=out, stderr=err)
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
    (work / "command.json").write_text(
        json.dumps({"argv": argv, "cwd": str(project), "timeout_s": timeout,
                    "returncode": returncode, "timed_out": timed_out,
                    "duration_s": duration}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"argv": argv, "returncode": returncode, "timed_out": timed_out,
            "duration_s": duration,
            "stdout": stdout_path.read_text(encoding="utf-8", errors="replace"),
            "stderr": stderr_path.read_text(encoding="utf-8", errors="replace")}


def collect(project: Path, work: Path, name: str) -> dict | None:
    canonical = project / "tb_data" / name
    source = None
    if canonical.is_file():
        source = canonical
    else:
        matches = [p for p in project.rglob(name) if p.is_file()]
        if matches:
            source = max(matches, key=lambda path: path.stat().st_mtime)
    if source is None:
        return None
    target = work / name
    shutil.copyfile(source, target)
    return {"file": name, "sha256": sha256_file(target),
            "source_path": source.relative_to(project).as_posix()}


def read_results(path: Path) -> list[list[float]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append([float(value) for value in line.split()])
    if not rows:
        raise SystemExit(f"result log is empty: {path}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=sorted(STAGE_FLAGS), required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, default=None)
    parser.add_argument("--reference", type=Path, default=None)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--vitis-run", type=Path, default=DEFAULT_VITIS_RUN)
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()
    if args.timeout is None:
        args.timeout = 300 if args.stage == "cosim" else 1800

    project = args.project.resolve()
    work = args.work.resolve()
    if not (project / "build_prj.tcl").is_file():
        raise SystemExit(f"not an hls4ml project (no build_prj.tcl): {project}")
    if not args.vitis_run.is_file():
        raise SystemExit(f"vitis-run not found: {args.vitis_run}")

    options = write_options(project, STAGE_FLAGS[args.stage])
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / "build_opt.tcl", work / "build-opt.tcl")

    execution = run_tool(args.vitis_run, project, work, args.timeout)
    evidence: dict = {
        **(identity_fields(args.deployment) if args.deployment else {}),
        "schema_version": 1,
        "stage": f"vitis_{args.stage}",
        "skill": "vitis-build-agent",
        "invocation_note": (
            "SKILL.md documents `vitis-run --mode hls --csim --work_dir ...`; vitis-run "
            "on 2025.2 accepts `--mode hls --tcl <script>`, which is what ran here."
        ),
        "project": str(project),
        "options": options,
        "build_opt_sha256": sha256_file(work / "build-opt.tcl"),
        "tool": {"path": str(args.vitis_run), "argv": execution["argv"],
                 "returncode": execution["returncode"], "timed_out": execution["timed_out"],
                 "duration_s": execution["duration_s"]},
        "artifacts": {},
        "status": "NOT_RUN",
    }

    if execution["timed_out"]:
        evidence["status"] = "BUDGET_EXHAUSTED"
        evidence["reasons"] = [f"stage exceeded {args.timeout}s"]
    elif execution["returncode"] != 0:
        evidence["status"] = "FAILED"
        evidence["reasons"] = [f"tool exit code {execution['returncode']}"]
    else:
        evidence["status"] = "PASSED"

    if args.stage == "csim":
        record = collect(project, work, "csim_results.log")
        if record:
            evidence["artifacts"]["csim_results"] = record
        elif evidence["status"] == "PASSED":
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append("no csim_results.log produced")

    if args.stage in {"cosim"}:
        record = collect(project, work, "rtl_cosim_results.log")
        if record:
            evidence["artifacts"]["rtl_cosim_results"] = record
        # RTL must agree with the C++ per value. A tool exit code of 0 is not that
        # evidence: the stage is downgraded to NOT_RUN when no per-value comparison
        # could be made, and to FAILED when the comparison fails.
        csim_record = collect(project, work, "csim_results.log")
        if csim_record:
            evidence["artifacts"]["csim_results"] = csim_record
        rtl_record = evidence["artifacts"].get("rtl_cosim_results")
        if args.deployment and csim_record and rtl_record:
            contract = yaml.safe_load(args.deployment.read_text(encoding="utf-8-sig"))
            criterion = criterion_from_deployment(contract)
            reference = read_results(work / "csim_results.log")
            actual = read_results(work / "rtl_cosim_results.log")
            rtl_gate = compare(
                "vitis_cosim_rtl_vs_cpp", reference, actual, criterion,
                reference_source=csim_record["source_path"],
                actual_source=rtl_record["source_path"],
            )
            rtl_gate["bit_identical_values"] = reference == actual
            # Which phase produced this log decides what the verdict means: hls4ml's cosim
            # runs a C-wrapper phase (identical to CSim by construction) before the traced
            # RTL phase, and only the traced phase exercises the RTL.
            rtl_gate["phase"] = (
                "c_wrapper"
                if "wrapc" in rtl_record["source_path"] or "wrapcpp" in rtl_record["source_path"]
                else "traced_rtl"
            )
            if rtl_gate["phase"] == "c_wrapper":
                rtl_gate["phase_note"] = (
                    "this is the C-wrapper phase; it is not evidence about the RTL. "
                    "RTL-level per-value evidence comes from the board simulation, which "
                    "runs the synthesised RTL."
                )
            evidence["rtl_gate"] = rtl_gate
            if not rtl_gate["passed"]:
                reasons = evidence.setdefault("reasons", [])
                reasons.append(
                    "rtl gate: "
                    f"{rtl_gate['values']['violations']}/{rtl_gate['values']['total']} "
                    "RTL values outside tolerance against the C++ run"
                )
                if evidence["status"] == "PASSED":
                    evidence["status"] = "FAILED"
            elif rtl_gate["phase"] == "c_wrapper" and evidence["status"] == "PASSED":
                # The comparison passed, but against the C-wrapper's log, which is CSim by
                # construction (measured: byte-identical value digests). Calling that PASSED
                # would let a stage that never exercised the RTL be read as RTL evidence -
                # exactly the situation in run 20260927-p20r16, where the traced phase did
                # not finish. NOT_RUN is the honest verdict: the RTL was not measured here.
                evidence["status"] = "NOT_RUN"
                evidence.setdefault("reasons", []).append(
                    "the only per-value log belongs to hls4ml's C-wrapper phase, which is "
                    "not RTL evidence; the stage is recorded as NOT_RUN (RTL-level per-value "
                    "evidence comes from the board simulation of the synthesised netlist)"
                )
        else:
            evidence["rtl_gate"] = {
                "schema_version": 1,
                "stage": "vitis_cosim_rtl_vs_cpp",
                "passed": False,
                "status": "NOT_RUN",
                "reasons": ["no per-value comparison: missing csim or rtl results log"],
            }
            if evidence["status"] == "PASSED":
                evidence["status"] = "NOT_RUN"
                evidence.setdefault("reasons", []).append(
                    "cosim finished but produced no comparable per-value results"
                )

    if args.stage == "csynth":
        # The CSynth numbers are an ESTIMATE. The frozen resource gate is applied to the
        # routed utilisation in the vivado-manager stage, so the estimate is recorded and
        # labelled rather than used as a verdict.
        reports = sorted(project.rglob("*_csynth.xml"))
        top = [p for p in reports if p.name == f"{project_name(project)}_csynth.xml"]
        for report in (top or reports)[:1]:
            target = work / report.name
            shutil.copyfile(report, target)
            evidence["artifacts"]["csynth_xml"] = {
                "file": report.name, "sha256": sha256_file(target),
                "source_path": report.relative_to(project).as_posix(),
            }
            try:
                import xml.etree.ElementTree as ElementTree

                root = ElementTree.parse(report).getroot()
                used = {child.tag: float(child.text) for child in root.find(".//AreaEstimates/Resources")}
                available = {child.tag: float(child.text)
                             for child in root.find(".//AreaEstimates/AvailableResources")}
                evidence["resources_estimate"] = {
                    "kind": "csynth_estimate_not_the_resource_gate",
                    "used": used,
                    "available": available,
                    "fraction": {k: round(used[k] / available[k], 4) for k in used if available.get(k)},
                }
            except Exception as exc:  # noqa: BLE001 - the estimate is best effort, the gate is not
                evidence.setdefault("reasons", []).append(f"csynth report not parsed: {exc}")
        if "csynth_xml" not in evidence["artifacts"]:
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append("csynth produced no report")

    # The numeric gate. A stage may be structurally fine and numerically wrong, so
    # the two verdicts are recorded separately.
    if args.deployment and args.reference and "csim_results" in evidence["artifacts"]:
        contract = yaml.safe_load(args.deployment.read_text(encoding="utf-8-sig"))
        criterion = criterion_from_deployment(contract)
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        actual = read_results(work / "csim_results.log")
        gate = compare(
            f"vitis_{args.stage}", reference, actual, criterion,
            reference_source=str(args.reference),
            actual_source=evidence["artifacts"]["csim_results"]["source_path"],
        )
        evidence["numerical_gate"] = gate
        if not gate["passed"] and evidence["status"] == "PASSED":
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append(
                "numerical gate: "
                f"{gate['values']['violations']}/{gate['values']['total']} values out of tolerance"
            )

    evidence_path = args.evidence or (work / "evidence.json")
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "stage": evidence["stage"],
        "status": evidence["status"],
        "returncode": execution["returncode"],
        "duration_s": execution["duration_s"],
        "numerical_gate": (
            {k: evidence["numerical_gate"][k]
             for k in ("passed", "values", "max_abs_error", "max_error_over_tolerance")}
            if "numerical_gate" in evidence else None
        ),
        "evidence": str(evidence_path),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
