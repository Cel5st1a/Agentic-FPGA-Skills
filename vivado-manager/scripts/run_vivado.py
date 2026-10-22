#!/usr/bin/env python
"""vivado-manager: run one Vivado stage of the board flow for a rendered wrapper.

Stages: synth (project + synthesis), impl (opt/place/route + timing/DRC gate),
bitstream (write the .bit from a checkpoint that still meets timing).

Design rules (operator principles, 2026-09-27): the part, project name, source dir and
constraints all come from the rendered wrapper/contract -- no board or model literal
lives here; the resource gate is the contract's own `constraints.resource_fraction_max`,
and a violation is reported as BLOCKED rather than as a failed tool run, so "the design
does not fit the frozen budget" stays distinguishable from "the tool broke".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_rtl import stage_rtl  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from line.identity import identity_fields  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
DEFAULT_VIVADO = Path(r"D:\vitis\2025.2\Vivado\bin\vivado.bat")
RESOURCE_KEYS = {"lut": "CLB LUTs", "ff": "CLB Registers", "bram_tile": "Block RAM Tile",
                 "dsp": "DSPs", "carry4": "CARRY4"}
#: Vivado 2025.2 labels the same rows "Slice LUTs" / "Slice Registers" in report_utilization.
#: F8 Muxes is not gated; it is read because it supplies CARRY4's denominator (see
#: parse_utilization).
RESOURCE_LABEL_ALIASES = {
    "CLB LUTs": ("CLB LUTs", "Slice LUTs"),
    "CLB Registers": ("CLB Registers", "Slice Registers"),
    "Block RAM Tile": ("Block RAM Tile",),
    "DSPs": ("DSPs",),
    "CARRY4": ("CARRY4",),
    "F8 Muxes": ("F8 Muxes",),
}


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
    """Kill the tool and its children.

    Vivado launches grandchildren that keep the build alive after the launcher exits;
    with captured pipes that also blocks the reader forever, so this runner writes tool
    output straight to files and kills the whole tree on timeout.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:  # pragma: no cover - the project's toolchain is Windows
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)


def run_vivado(vivado: Path, script: Path, work: Path, timeout: int) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    argv = (["cmd", "/c", str(vivado)] if vivado.suffix.lower() in {".bat", ".cmd"} else [str(vivado)])
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


def parse_utilization(path: Path) -> dict:
    """Read the Site Type table of a report_utilization file.

    Column positions are taken from the table's own header row: the earlier
    fixed-offset reader took the "Prohibited" column for "Available" and produced
    impossible fractions (37.48 for a 22.4% design).
    """
    if not path.is_file():
        return {}
    accepted = {label: key for key, labels in RESOURCE_LABEL_ALIASES.items() for label in labels}
    rows: dict[str, dict] = {}
    columns: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if "Site Type" in cells and "Used" in cells:
            columns = {name: index for index, name in enumerate(cells)}
            continue
        if not columns or not cells:
            continue
        # report_utilization marks rows it has footnotes for with a trailing "*"
        # ("Slice LUTs*"), which the exact-match label lookup below rejected. The effect
        # was silent and asymmetric: the LUT row was dropped from every *synthesis*
        # report (so the synth-stage resource gate checked only DSPs / registers / BRAM
        # and could pass a design that cannot be placed -- measured on run
        # asymmetric_cnn_fc\20260928-cnn04: synth gate PASSED on 3 keys while the design
        # needed 178,785 of 134,600 LUTs), while the *routed* report of the same design
        # has no asterisk on that row and did resolve it. Normalising the label here
        # makes the gate check the same quantities at both stages.
        name = cells[0].rstrip("*").strip()

        # CARRY4 is not in the "Slice Logic" table: it is printed later in the
        # primitive-count table as three cells, `| CARRY4 | 36885 | CarryLogic |`, with no
        # `Available` column at all. So it can never be read through the branch below and
        # was silently absent from the gate. Measured on run
        # asymmetric_cnn_fc\20260928-cnn04: the synth gate reported PASSED while the design
        # needed 36,885 CARRY4 of 33,650, and place_design then refused with
        # `[DRC UTLZ-1] CARRY4 over-utilized ... 36747 of 33650` -- 13 minutes later.
        #
        # Denominator: CARRY4 and F8 Muxes are both bounded by the CLB slice count, and
        # `F8 available + F8 prohibited` reproduces Vivado's own comparable denominator in
        # every report checked here (33,650 at synthesis; 33,450 + 200 = 33,650 routed for
        # the delivered mlp_64_32 package). Using the F8 row keeps this derived from the
        # tool's own numbers instead of hard-coding a device constant.
        if accepted.get(name) == "CARRY4":
            try:
                used = float(cells[1].replace(",", ""))
            except (IndexError, ValueError):
                continue
            f8 = rows.get("F8 Muxes")
            if not f8:
                continue
            available = f8["available"] + f8.get("prohibited", 0.0)
            rows["CARRY4"] = {"used": used, "available": available,
                              "fraction": (used / available) if available else 0.0}
            continue

        if name not in accepted:
            continue
        try:
            used = float(cells[columns["Used"]].replace(",", ""))
            available = float(cells[columns["Available"]].replace(",", ""))
        except (KeyError, IndexError, ValueError):
            continue
        row = {"used": used, "available": available,
               "fraction": (used / available) if available else 0.0}
        # `Prohibited` is carried for the rows that can be excluded from a region, because
        # CARRY4's denominator is derived from the F8-Muxes row's available + prohibited.
        try:
            row["prohibited"] = float(cells[columns["Prohibited"]].replace(",", ""))
        except (KeyError, IndexError, ValueError):
            pass
        rows[accepted[name]] = row
    return rows


def parse_timing(path: Path) -> dict:
    result = {"wns_ns": None, "tns_ns": None, "whs_ns": None, "ths_ns": None, "met": None}
    if not path.is_file():
        return result
    text = path.read_text(encoding="utf-8", errors="replace")
    result["met"] = "All user specified timing constraints are met." in text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("WNS(ns)"):
            for follow in lines[index + 1 : index + 4]:
                values = re.findall(r"[-+]?\d+\.\d+", follow)
                if len(values) >= 4:
                    result.update({"wns_ns": float(values[0]), "tns_ns": float(values[1]),
                                   "whs_ns": float(values[2]), "ths_ns": float(values[3])})
                    return result
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("synth", "impl", "bitstream"), required=True)
    parser.add_argument("--board", type=Path, required=True, help="board run dir (holds wrapper.json)")
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--vivado", type=Path, default=DEFAULT_VIVADO)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()

    board = args.board.resolve()
    wrapper = json.loads((board / "wrapper.json").read_text(encoding="utf-8"))
    contract = yaml.safe_load(args.deployment.resolve().read_text(encoding="utf-8-sig"))
    part = wrapper["part"]
    top = "board_top"
    rtl_dir = board / "rtl"
    project_name = f"{wrapper['top']}_board"

    if args.stage == "synth":
        staged_rtl = stage_rtl(board, wrapper)
    else:
        staged_rtl = None

    if args.stage in {"impl", "bitstream"}:
        # A checkpoint older than the RTL would silently implement the previous design:
        # a failed synth run leaves the previous synth.dcp in place and the later stages
        # would build a bitstream from RTL that no longer exists.
        checkpoint = board / "synth.dcp"
        if not checkpoint.is_file():
            raise SystemExit(f"no synthesis checkpoint to implement: {checkpoint}")
        newest_source = max(
            [p.stat().st_mtime for p in rtl_dir.glob("*.v")] or [0.0]
        )
        if checkpoint.stat().st_mtime < newest_source:
            raise SystemExit(
                "STALE_CHECKPOINT: synth.dcp is older than the RTL sources; re-run the "
                "synth stage before implementing"
            )

    values = {
        "PROJECT_NAME": project_name, "ROOT": board.as_posix(), "PART": part,
        "TOP": top, "RTL_DIR": rtl_dir.as_posix(), "XDC": (rtl_dir / "board.xdc").as_posix(),
        "THREADS": args.threads, "BITSTREAM": (board / f"{top}.bit").as_posix(),
    }
    template = TEMPLATES / {"synth": "run_synth.tcl", "impl": "run_impl.tcl",
                            "bitstream": "write_bitstream.tcl"}[args.stage]
    script = board / f"{args.stage}.tcl"
    script.write_text(render(template, values), encoding="utf-8")

    execution = run_vivado(args.vivado, script, args.work.resolve(), args.timeout)
    rtl_record = {
        "rtl_dir": rtl_dir.as_posix(),
        "files": {p.name: sha256_file(p) for p in sorted(rtl_dir.glob("*")) if p.is_file()},
    }
    evidence: dict = {
        **(identity_fields(args.deployment, (wrapper.get("board") or {}).get("pins_profile")) if args.deployment else {}),
        "schema_version": 1,
        "stage": f"vivado_{args.stage}",
        "skill": "vivado-manager",
        "part": part,
        "top": top,
        "board_dir": str(board),
        "contract": {"path": str(args.deployment.resolve()),
                     "sha256": sha256_file(args.deployment.resolve())},
        "tool": {"argv": execution["argv"], "returncode": execution["returncode"],
                 "timed_out": execution["timed_out"], "duration_s": execution["duration_s"]},
        "status": "NOT_RUN",
        "artifacts": {},
        "rtl_used": rtl_record,
    }
    if staged_rtl is not None:
        evidence["staged_rtl"] = staged_rtl
    if execution["timed_out"]:
        evidence["status"] = "BUDGET_EXHAUSTED"
        evidence["reasons"] = [f"stage exceeded {args.timeout}s"]
    elif execution["returncode"] != 0:
        evidence["status"] = "FAILED"
        tail = [line for line in (execution["stdout"] or "").splitlines() if "ERROR" in line][-5:]
        evidence["reasons"] = [f"tool exit code {execution['returncode']}"] + tail
    else:
        evidence["status"] = "PASSED"

    for name in ("utilization_synth.rpt", "utilization_routed.rpt", "timing_routed.rpt",
                 "drc_routed.rpt", "synth.dcp", "routed.dcp"):
        candidate = board / name
        if candidate.is_file():
            evidence["artifacts"][name] = {"path": str(candidate), "sha256": sha256_file(candidate),
                                           "bytes": candidate.stat().st_size}

    utilization = parse_utilization(board / ("utilization_routed.rpt" if args.stage != "synth"
                                             else "utilization_synth.rpt"))
    if utilization:
        evidence["resources"] = utilization
    timing = parse_timing(board / "timing_routed.rpt")
    if timing.get("wns_ns") is not None or timing.get("met") is not None:
        evidence["timing"] = timing

    if args.stage == "bitstream" and evidence["status"] == "PASSED":
        bit = board / f"{top}.bit"
        if bit.is_file():
            evidence["artifacts"]["bitstream"] = {"path": str(bit), "sha256": sha256_file(bit),
                                                  "bytes": bit.stat().st_size}
        else:
            evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append("tool reported success but no bitstream was written")

    limit = float((contract.get("constraints") or {}).get("resource_fraction_max", 0.8))
    if utilization:
        # Gate exactly the resources this tool declares as gated (RESOURCE_KEYS), not every
        # row parse_utilization happens to return. F8 Muxes is parsed only because it
        # supplies CARRY4's denominator, and must not become a pass/fail criterion of its
        # own -- that would silently widen the gate to an undeclared resource.
        gated = {label for label in RESOURCE_KEYS.values() if label in utilization}
        fractions = {key: utilization[key]["fraction"] for key in gated}
        worst_key = max(fractions, key=fractions.get)
        gate = {"limit": limit, "fractions": fractions, "worst": worst_key,
                "worst_fraction": fractions[worst_key], "passed": fractions[worst_key] <= limit,
                "gated_resources": sorted(fractions)}
        evidence["resource_gate"] = gate
        if not gate["passed"] and evidence["status"] == "PASSED":
            evidence["status"] = "BLOCKED"
            evidence.setdefault("reasons", []).append(
                f"RESOURCE_LIMIT: {worst_key} at {fractions[worst_key]:.4f} exceeds "
                f"resource_fraction_max={limit}"
            )

    # Unmet timing is a failed gate, not a footnote. Without this the stage reports
    # PASSED with timing.met=false and the line would spend a write_bitstream run on a
    # design that cannot clock at the contract's period; only the packager refused.
    if args.stage in {"impl", "bitstream"} and evidence.get("timing"):
        if evidence["timing"].get("met") is not True:
            if evidence["status"] == "PASSED":
                evidence["status"] = "FAILED"
            evidence.setdefault("reasons", []).append(
                "TIMING_NOT_MET: "
                f"WNS={evidence['timing'].get('wns_ns')} ns, WHS={evidence['timing'].get('whs_ns')} ns "
                f"for clock period {contract.get('clock_period_ns')} ns"
            )
    elif args.stage in {"impl", "bitstream"}:
        if evidence["status"] == "PASSED":
            evidence["status"] = "FAILED"
        evidence.setdefault("reasons", []).append(
            "no routed timing report was produced, so timing could not be checked"
        )

    args.evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.evidence.resolve().write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "stage": evidence["stage"], "status": evidence["status"],
        "returncode": execution["returncode"], "duration_s": execution["duration_s"],
        "resource_gate": evidence.get("resource_gate"),
        "timing": evidence.get("timing"),
        "reasons": evidence.get("reasons"),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
