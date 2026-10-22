"""bitstream-package skill: assemble a deployment package from a verified **host-I/O** run.

`package_bitstream.py` handles the ROM variant: a top whose only board-facing ports are
`sys_clk_p/sys_clk_n/rst_n/led`, fed by a sample ROM that travels inside the bitstream.
It refuses the host variant by name, because a design whose samples arrive over UART needs
gates the ROM path does not have.

This packager is that gate. It is contract-driven and generic in the same way: the model id
is an argument and everything else (model files, board, part, precision, clock, resource
limit) comes from the deployment contract inside the run.

Hard rules enforced here
------------------------
A bitstream plus a zero tool exit code is not evidence of correctness, so this script does
not trust the run's own evidence files either. Before packaging it:

  1. requires the host build to be where the host variant puts it, with the routed
     checkpoint, the wrapper record, the top and the constraints all present;
  2. **re-derives the routed port audit** from the audit stage's recorded tool output -- it
     never re-runs Vivado. It re-parses that output, recomputes the classification, checks
     the routed UART pins against the board profile the wrapper was built for (L14/L15 on
     the AX7A200B, V18/AA19 on the Nexys Video), and binds
     the audit's checkpoint and wrapper to the files on disk by sha256;
  3. requires the host simulation to have observed the same RTL, file by file by sha256,
     that the synthesis stage consumed -- otherwise the verified numbers describe a
     different design than the bitstream;
  4. **recomputes three numerical gates** from the raw artifacts: the HLS C++ log against
     the python reference, the decoded UART response frames against the python reference,
     and the decoded frames against the HLS C++ log. The recorded verdicts, margins and
     value digests must all reproduce, or packaging is refused. The frames are decoded
     with the geometry the wrapper itself materialised -- never re-derived from shapes;
  5. requires the three host Vivado stages to report PASSED, the routed resource fraction
     to stay inside the contract's own `constraints.resource_fraction_max`, and timing to
     be met;
  6. re-hashes every artifact the evidence names and refuses on any mismatch;
  7. requires the wrapper record to be self-consistent: the contract it names, the source
     wrapper it was generated from, the core Verilog it wraps, and every generated source
     must still hash to what it recorded;
  8. requires the bitstream to live inside the run, to match the recorded sha256, and to be
     newer than the RTL and checkpoints it was built from.

And, because none of the above is a live board measurement, the package it writes says so:
`hardware_verified = false`, `on_board_numerical = NOT_RUN`, `programmed = false`. A run
that carries `evidence/hardware-validation/summary.json` is refused rather than silently
promoted, because this packager has no rule that could accept a capture it cannot check.

Usage
-----
python package_host_bitstream.py --run <run-dir> --model-id <id> --release-root D:\\ML_models
python package_host_bitstream.py --run <run-dir> --model-id <id> --release-root <dir> --check-only
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SKILL_ROOT = Path(__file__).resolve().parents[2]
for candidate in (SKILL_ROOT / "scripts",
                  SKILL_ROOT / "board-host-io" / "scripts",
                  SKILL_ROOT / "vivado-manager" / "scripts",
                  Path(__file__).resolve().parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from host_geometry import geometry_for_wrapper  # noqa: E402
from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from package_bitstream import (  # noqa: E402
    CRITERION_FIELDS,
    check_artifacts,
    copy_if_exists,
    dig,
    drc_summary,
    find_contract,
    load_json,
    pins_profile,
    read_results,
    resolve,
    same_numbers,
    sha256_file,
)
#: The report parsers come from the stage that *wrote* the evidence, so the packager
#: recomputes a verdict with the same code that produced it instead of with a second,
#: subtly different reader. Importing is deliberate; nothing here executes the tool.
from run_vivado import RESOURCE_KEYS, parse_timing, parse_utilization  # noqa: E402

import audit_ports  # noqa: E402
import board_facts  # noqa: E402

HOST_BOARD = Path("p3-build") / "host-board"
AUDIT_DIR = Path("evidence") / "host-port-audit"

#: The three host Vivado stages, in build order.
HOST_STAGES = (
    "host-vivado-synth-evidence.json",
    "host-vivado-impl-evidence.json",
    "host-vivado-bitstream-evidence.json",
)
#: Stages for which a timing verdict is required. Synthesis alone is not routed, so it has
#: no routed timing to report; the routed stages do, and `met` must be true there.
TIMING_REQUIRED = ("host-vivado-impl-evidence.json", "host-vivado-bitstream-evidence.json")
#: Which report each stage's resource gate is recomputed from. The routed stages share the
#: routed report; synthesis reports on the synthesised (pre-place) netlist.
RESOURCE_REPORT = {
    "host-vivado-synth-evidence.json": "utilization_synth.rpt",
    "host-vivado-impl-evidence.json": "utilization_routed.rpt",
    "host-vivado-bitstream-evidence.json": "utilization_routed.rpt",
}
#: The routed timing report, and the report that carries the pad lines used to anchor the
#: port audit.
TIMING_REPORT = "timing_routed.rpt"
#: Stages that must report PASSED. `convert-evidence.json` carries no verdict at all, so
#: it is recorded rather than dressed up as one.
PASS_REQUIRED = ("csim-evidence.json", "csynth-evidence.json") + HOST_STAGES
#: Evidence files copied into the package for a reader to re-check.
COPIED_EVIDENCE = ("convert-evidence.json", "csim-evidence.json", "csynth-evidence.json") + HOST_STAGES

#: The UART acceptance bound is the board profile the wrapper was built for; it is read
#: in `verify_port_audit` through `board_facts`, never restated here. What is pinned is
#: the *shape* of the claim: a host path is an FPGA input on `uart_rx` and an FPGA output
#: on `uart_tx`.
HOST_PORT_DIRECTIONS = {"uart_rx": "IN", "uart_tx": "OUT"}
#: The routed resources the board may use, as a fraction, read from the contract.
RESOURCE_FRACTION_KEY = "resource_fraction_max"

#: Numeric comparison tolerance, the same one `package_bitstream.same_numbers` uses.
TOLERANCE = 1e-12

#: Fields that must read "not verified" wherever they appear in a record that ships.
#: Machine-readable on purpose: this packager's own output legitimately contains
#: `hardware_verified: false`, so a substring scan would refuse the package it just wrote.
HONESTY_FALSE_FIELDS = ("hardware_verified", "programmed", "hardware_numerical_verified",
                        "board_programmed", "jtag_programmed", "on_board_verified")
HONESTY_NOT_RUN_FIELDS = ("on_board_numerical", "board_inference", "board_sim", "rtl_cosim")
HONESTY_SCOPE_FIELDS = ("verification_scope",)
#: Scope values this line uses for "a bitstream exists, nothing was measured on hardware".
UNVERIFIED_SCOPES = {"configure-only", "host-path-present-unverified",
                     "host-path-built-unverified"}
#: A field whose name sounds like a board claim must not be a positive verdict.
CLAIM_KEY = re.compile(r"(live|capture|jtag|programmed|on_board|hardware|burn|flash|verified)",
                       re.IGNORECASE)
POSITIVE_VERDICTS = {"PASSED", "PASS", "VERIFIED", "TRUE", "YES", "OK", "DONE", "MET"}
#: Positive-claim needles for the two run documents that are copied verbatim. These are
#: text, so the machine-readable rule above cannot reach them; only *positive* forms are
#: refused, never the negative wording this packager and `bootstrap_run.py` write.
TEXT_CLAIM_NEEDLES = (
    re.compile(r"hardware_verified\s*[:=]\s*true", re.IGNORECASE),
    re.compile(r"on_board_numerical\s*[:=]\s*[\"']?PASSED", re.IGNORECASE),
    re.compile(r"programmed\s*[:=]\s*true", re.IGNORECASE),
    re.compile(r"verified on the (physical|real) board", re.IGNORECASE),
    re.compile(r"live (uart|serial|board) capture", re.IGNORECASE),
)
#: Files copied verbatim into the package whose JSON contents get the honesty scan.
HONESTY_JSON_SOURCES = tuple(COPIED_EVIDENCE) + (
    "host-sim/host-sim-evidence.json", "host-port-audit/io-scope.json")
#: Files copied verbatim that are text, checked with the needles above.
HONESTY_TEXT_SOURCES = ("RUN.md", "starting-state.json")

#: `--model-id` becomes a directory name under the release root, so it is constrained to
#: one path component and never `.`/`..`.
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: Report/record files worth shipping, as (source relative to the run, package relative).
REPORTS = {
    HOST_BOARD / "timing_routed.rpt": "reports/timing_impl.rpt",
    HOST_BOARD / "utilization_routed.rpt": "reports/utilization_impl.rpt",
    HOST_BOARD / "utilization_synth.rpt": "reports/utilization_synth.rpt",
    HOST_BOARD / "drc_routed.rpt": "reports/drc.rpt",
    HOST_BOARD / "clocks.rpt": "reports/clocks.rpt",
    HOST_BOARD / "timing_synth.rpt": "reports/timing_synth.rpt",
    Path("p3-build") / "csynth" / "csynth-summary.json": "reports/csynth-summary.json",
    Path("p3-build") / "csynth" / "myproject_csynth.xml": "reports/myproject_csynth.xml",
}

NUMERIC_KEYS = ("passed", "max_abs_error", "max_error_over_tolerance",
                "reference_sha256", "actual_sha256",
                "reference_file_sha256", "actual_file_sha256")


def refuse_geometry(wrapper_path: Path, refusals: list[str]) -> None:
    """The record gap the host variant must not paper over.

    A wrapper without the materialised geometry cannot yield the frame layout, and the
    layout must not be guessed from the model's shape or the contract's precision: two
    different designs can share both. The fix is a new record, not a new derivation.
    """
    refusals.append(
        f"host_geometry: the host wrapper must carry the materialised geometry, but "
        f"{wrapper_path} records None; regenerate the record in a new run; never re-derive "
        "from shape, precision or references."
    )


def load_geometry(deployment: Path, contract: dict, wrapper: dict, wrapper_path: Path,
                  board_profile: dict | None, refusals: list[str]):
    """The frame geometry, from the wrapper that belongs to *this* bitstream.

    Only the bitstream-bearing wrapper is consulted. A sibling build directory that
    happens to carry a `host_geometry` block -- a recheck, say -- describes its own design
    and must never become the source of this bitstream's numbers.
    """
    if "host_geometry" not in wrapper:
        refuse_geometry(wrapper_path, refusals)
        return None
    try:
        return geometry_for_wrapper(deployment, contract, wrapper, board_profile=board_profile)
    except ValueError as exc:
        refusals.append(
            f"host_geometry: the wrapper's materialised geometry is not usable: {exc}"
        )
        return None


def verify_host_layout(run_dir: Path, refusals: list[str]) -> Path:
    board = run_dir / HOST_BOARD
    if not board.is_dir():
        refusals.append(f"the host build directory is missing: {board}")
        return board
    for name in ("routed.dcp", "wrapper.json", "board_top.v", "board.xdc"):
        if not (board / name).is_file():
            refusals.append(f"the host build is incomplete: {board / name} is missing")
    return board


def parse_pad_anchors(path: Path) -> dict[str, dict]:
    """Pad lines for the UART ports, read from the routed timing report.

    `report_timing_summary` lists the pad and the port name for the paths it shows. Because
    the board XDC marks the UART pins as false paths, they land in the report's
    unconstrained section, which gives the package a second, hash-bound statement of where
    the host path's two pads are that does not come from the audit itself.
    """
    anchors: dict[str, dict] = {}
    if not path.is_file():
        return anchors
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        matched = re.match(r"\s*([A-Z]{1,3}\d+)\b", line)
        if not matched:
            continue
        for name, direction in HOST_PORT_DIRECTIONS.items():
            if re.search(rf"\b{name}\s*\({direction}\)", line):
                anchors.setdefault(name, {"pin": matched.group(1), "direction": direction})
    return anchors


def verify_port_audit(run_dir: Path, evidence: dict, wrapper: dict,
                      refusals: list[str]) -> dict | None:
    """Re-derive the routed port audit from its recorded output.

    This does not re-run Vivado: it re-parses the audit stage's own recorded stdout,
    recomputes the classification with the producer's own functions, and checks the
    result against the verified pinout and against the files on disk. Recomputing from the
    raw output is the point -- `io-scope.json` on its own is a recorded verdict.
    """
    board = run_dir / HOST_BOARD
    audit = run_dir / AUDIT_DIR
    paths = {name: audit / name for name in ("io-scope.json", "stdout.txt", "ports.tcl")}
    missing = [path for path in paths.values() if not path.is_file()]
    for path in missing:
        refusals.append(f"missing routed port audit file: {path}")
    if missing:
        return None

    scope = load_json(paths["io-scope.json"], refusals)
    if scope is None:
        return None
    try:
        details = audit_ports.parse_vivado_ports(
            paths["stdout.txt"].read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        refusals.append(f"the recorded routed port audit output does not parse: {exc}")
        return None

    recomputed = audit_ports.classify_ports(
        {name: value["direction"] for name, value in details.items()})
    for key in ("top_ports", "host_input_output_available", "verification_scope",
                "missing_for_host_path", "on_board_numerical", "programmed",
                "hardware_verified"):
        if scope.get(key) != recomputed.get(key):
            refusals.append(
                f"io-scope.json.{key} = {scope.get(key)!r} does not reproduce from the "
                f"recorded routed port audit output ({recomputed.get(key)!r})")
    if scope.get("ports") != details:
        refusals.append(
            "io-scope.json port details do not reproduce from the recorded routed port "
            "audit output")

    checkpoint = board / "routed.dcp"
    wrapper_path = board / "wrapper.json"
    if Path(str(scope.get("checkpoint", ""))).resolve() != checkpoint.resolve():
        refusals.append(
            f"the port audit was taken on {scope.get('checkpoint')!r}, not this run's "
            f"{checkpoint}")
    elif scope.get("checkpoint_sha256") != sha256_file(checkpoint):
        refusals.append(
            "the port audit is stale: its checkpoint_sha256 does not match the routed "
            "checkpoint it names")
    if Path(str(scope.get("wrapper", ""))).resolve() != wrapper_path.resolve():
        refusals.append(
            f"the port audit was taken against {scope.get('wrapper')!r}, not this run's "
            f"{wrapper_path}")
    elif scope.get("wrapper_sha256") != sha256_file(wrapper_path):
        refusals.append(
            "the port audit is stale: its wrapper_sha256 does not match the wrapper it names")

    recorded_dcp = ((evidence.get("host-vivado-bitstream-evidence.json") or {})
                    .get("artifacts") or {}).get("routed.dcp", {}).get("sha256")
    if recorded_dcp and scope.get("checkpoint_sha256") != recorded_dcp:
        refusals.append(
            "the port audit's checkpoint differs from the routed checkpoint the bitstream "
            "build evidence names")

    tcl = paths["ports.tcl"].read_text(encoding="utf-8", errors="replace")
    if "open_checkpoint" not in tcl:
        refusals.append(f"{paths['ports.tcl']} does not open a checkpoint")
    elif checkpoint.resolve().as_posix() not in tcl.replace("\\\\", "/"):
        refusals.append(
            f"{paths['ports.tcl']} does not read this run's routed checkpoint ({checkpoint})")

    if recomputed.get("host_input_output_available") is not True:
        refusals.append(
            "the routed top has no complete host input/output path: missing "
            f"{recomputed.get('missing_for_host_path')}")
    if recomputed.get("verification_scope") != "host-path-present-unverified":
        refusals.append(
            "a routed design with a host path must be recorded as "
            "'host-path-present-unverified', not "
            f"{recomputed.get('verification_scope')!r}")

    # The acceptance bound is the profile this wrapper was built for, not a pinout
    # restated in code: the AX7A200B puts UART on L14/L15 LVCMOS33 and the Nexys Video
    # puts it on V18/AA19 LVCMOS33, so a literal here would silently accept one board's
    # artifact as the other's.
    try:
        facts = board_facts.facts_for_wrapper(wrapper)
    except ValueError as exc:
        refusals.append(f"board profile: {exc}")
        facts = None
    if facts is None:
        return None
    uart_pins = {"uart_rx": ("IN", facts.uart_rx_pin), "uart_tx": ("OUT", facts.uart_tx_pin)}

    # The verified pinout is checked against both halves: the audit output that was
    # recorded and the scope record derived from it. Checking only one would let an edited
    # `io-scope.json` pass on the strength of the other.
    recorded_ports = scope.get("ports")
    for label, source in (("the recorded port audit output", details),
                          ("io-scope.json", recorded_ports if isinstance(recorded_ports, dict) else {})):
        for name, (direction, expected_pin) in sorted(uart_pins.items()):
            detail = source.get(name)
            if not isinstance(detail, dict) or detail.get("direction") != direction:
                refusals.append(
                    f"{label}: no {name} {direction} port (found {detail!r})")
                continue
            if detail.get("pin") != expected_pin:
                refusals.append(
                    f"{label}: routed UART pin {name} = {detail.get('pin')!r}, expected "
                    f"{expected_pin!r} from {facts.profile_path} ({facts.board_id})")
            if detail.get("iostandard") != facts.uart_iostandard:
                refusals.append(
                    f"{label}: routed UART IOSTANDARD {name} = "
                    f"{detail.get('iostandard')!r}, expected {facts.uart_iostandard!r} "
                    f"from {facts.profile_path}")

    # The third-party anchor. `stdout.txt` and `io-scope.json` are not hash-bound by
    # anything, so on their own they only prove that the audit agrees with itself -- which
    # a hand-written triple also does. The routed timing report is bound by
    # `artifacts.*.sha256`, ships in the package, and states the same two pads, so it is
    # what turns "self-consistent" into "corroborated".
    anchor_path = board / TIMING_REPORT
    anchors = parse_pad_anchors(anchor_path)
    for name, (direction, expected_pin) in sorted(uart_pins.items()):
        anchor = anchors.get(name)
        if anchor is None:
            refusals.append(
                f"{TIMING_REPORT} carries no {name} ({direction}) pad line, so the routed "
                "port audit is not anchored to any hash-bound artifact; the routed timing "
                "report must show the UART pads")
        elif anchor["pin"] != expected_pin:
            refusals.append(
                f"{TIMING_REPORT} places {name} on {anchor['pin']}, expected "
                f"{expected_pin!r} from {facts.profile_path} ({facts.board_id})")
        elif isinstance(details.get(name), dict) and details[name].get("pin") != anchor["pin"]:
            refusals.append(
                f"the recorded port audit puts {name} on {details[name].get('pin')!r} but "
                f"{TIMING_REPORT} puts it on {anchor['pin']}")

    if scope.get("wrapper_ports_match") is not True:
        refusals.append("io-scope.json does not record that the routed ports match the wrapper")
    elif set(details) != set(wrapper.get("top_ports") or []):
        refusals.append("the routed top ports disagree with the wrapper's top_ports")
    if scope.get("hardware_verified") is not False:
        refusals.append(
            f"io-scope.json claims hardware_verified = {scope.get('hardware_verified')!r}; no "
            "live board capture exists")
    if scope.get("on_board_numerical") != "NOT_RUN":
        refusals.append(
            f"io-scope.json records on_board_numerical = {scope.get('on_board_numerical')!r}, "
            "expected 'NOT_RUN'")
    if scope.get("programmed") is not False:
        refusals.append(
            f"io-scope.json records programmed = {scope.get('programmed')!r}, expected False")
    return scope


def verify_wrapper(run_dir: Path, contract_path: Path, refusals: list[str]) -> dict:
    """The wrapper record must describe the files that are actually there."""
    board = run_dir / HOST_BOARD
    wrapper_path = board / "wrapper.json"
    wrapper = load_json(wrapper_path, refusals)
    if wrapper is None:
        refusals.append(f"missing host wrapper record: {wrapper_path}")
        return {}
    if wrapper.get("skill") != "board-host-io":
        refusals.append(
            f"wrapper.json was written by {wrapper.get('skill')!r}, not the host-I/O skill")
    if wrapper.get("contract_sha256") != sha256_file(contract_path):
        refusals.append(
            "wrapper.json is stale: contract_sha256 does not match this run's deployment "
            "contract")
    source = Path(str(wrapper.get("source_wrapper") or ""))
    if not source.is_file():
        refusals.append(f"wrapper.json names a source wrapper that does not exist: {source}")
    elif wrapper.get("source_wrapper_sha256") != sha256_file(source):
        refusals.append(
            "wrapper.json is stale: source_wrapper_sha256 does not match the source wrapper "
            "it names")
    core = Path(str(wrapper.get("core_verilog") or ""))
    if not core.is_file():
        refusals.append(f"wrapper.json names a core Verilog that does not exist: {core}")
    elif wrapper.get("core_verilog_sha256") != sha256_file(core):
        refusals.append(
            "wrapper.json is stale: core_verilog_sha256 does not match the core file")
    generated = wrapper.get("generated_sources") or {}
    outputs_value = wrapper.get("outputs")
    if not generated:
        refusals.append("wrapper.json records no generated_sources hashes")
    if not isinstance(outputs_value, list) or not outputs_value:
        refusals.append(
            "wrapper.json records no outputs list, so its generated_sources cannot be "
            "checked against the files the host board is built from")
    else:
        outputs = set(outputs_value)
        if set(generated) != outputs:
            refusals.append(
                "wrapper.json generated_sources do not cover exactly the files it lists as "
                f"outputs (differs by {sorted(set(generated) ^ outputs)})")
    for name, digest in sorted(generated.items()):
        candidate = board / name
        if not candidate.is_file():
            refusals.append(f"wrapper.json names a generated source that is missing: {candidate}")
        elif sha256_file(candidate) != digest:
            refusals.append(
                f"wrapper.json is stale: generated_sources.{name} does not match the file "
                "on disk")
    if wrapper.get("hardware_verified") is not False:
        refusals.append(
            f"wrapper.json claims hardware_verified = {wrapper.get('hardware_verified')!r}; no "
            "live board capture exists")
    if wrapper.get("on_board_numerical") != "NOT_RUN":
        refusals.append(
            f"wrapper.json records on_board_numerical = {wrapper.get('on_board_numerical')!r}, "
            "expected 'NOT_RUN'")
    if wrapper.get("host_input_output_available") is not True:
        refusals.append(
            "wrapper.json does not declare a host input/output path; this packager is for "
            "the host variant")
    return wrapper


def measured_number(value) -> float | None:
    """A recorded number, or None when it is absent, boolean, non-numeric or not finite.

    `bool` is excluded explicitly: `True` is an `int` in Python, and reading a verdict flag
    as `1.0` would let it pass a numeric comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def recompute_resource_gate(board: Path, report_name: str, limit, refusals: list[str],
                            label: str) -> dict | None:
    """Recompute the routed resource fractions from the utilization report itself.

    The recorded gate is a claim. This reads the same report the stage read -- with the
    stage's own parser -- and rebuilds the gate from it, so a record that is missing, null,
    a string, or simply different from the report cannot be packaged.
    """
    report_path = board / report_name
    if not report_path.is_file():
        refusals.append(
            f"{label}: the utilization report the resource gate is computed from is "
            f"missing: {report_path}")
        return None
    utilization = parse_utilization(report_path)
    gated = {label_name for label_name in RESOURCE_KEYS.values() if label_name in utilization}
    if not gated:
        refusals.append(
            f"{label}: no gated resource row could be read from {report_name}, so the "
            "resource gate cannot be recomputed")
        return None
    fractions = {key: utilization[key]["fraction"] for key in gated}
    worst = max(fractions, key=fractions.get)
    return {"limit": limit, "fractions": fractions, "worst": worst,
            "worst_fraction": fractions[worst],
            "passed": (limit is not None and fractions[worst] <= float(limit)),
            "gated_resources": sorted(fractions), "recomputed_from": report_name}


def check_resource_gate(name: str, gate, recomputed: dict, limit, refusals: list[str]) -> None:
    """Compare the stage's recorded resource gate against the recomputed one, field by field."""
    if not isinstance(gate, dict):
        refusals.append(
            f"{name}: resource_gate is {type(gate).__name__}, expected an object carrying "
            f"limit/fractions/worst/worst_fraction (found {gate!r})")
        return
    if recomputed is None:
        return
    report_name = recomputed["recomputed_from"]
    if gate.get("passed") is not True:
        refusals.append(
            f"{name}: resource_gate.passed is {gate.get('passed')!r}, expected True")
    recorded_fractions = gate.get("fractions")
    if not isinstance(recorded_fractions, dict):
        refusals.append(
            f"{name}: resource_gate.fractions is {type(recorded_fractions).__name__}, "
            "expected an object of per-resource fractions")
    else:
        if set(recorded_fractions) != set(recomputed["fractions"]):
            refusals.append(
                f"{name}: resource_gate.fractions covers "
                f"{sorted(recorded_fractions)}, but {report_name} yields "
                f"{sorted(recomputed['fractions'])}")
        for key, measured in recomputed["fractions"].items():
            recorded = measured_number(recorded_fractions.get(key))
            if recorded is None:
                refusals.append(
                    f"{name}: resource_gate.fractions.{key} = "
                    f"{recorded_fractions.get(key)!r} is not a number")
            elif abs(recorded - measured) > TOLERANCE:
                refusals.append(
                    f"{name}: resource_gate.fractions.{key} = {recorded} does not reproduce "
                    f"from {report_name} ({measured})")
    if gate.get("worst") != recomputed["worst"]:
        refusals.append(
            f"{name}: resource_gate.worst = {gate.get('worst')!r} does not reproduce from "
            f"{report_name} ({recomputed['worst']!r})")
    recorded_worst = measured_number(gate.get("worst_fraction"))
    if recorded_worst is None:
        refusals.append(
            f"{name}: resource_gate.worst_fraction = {gate.get('worst_fraction')!r} is not a "
            f"number; it cannot be checked against {report_name}")
    elif abs(recorded_worst - recomputed["worst_fraction"]) > TOLERANCE:
        refusals.append(
            f"{name}: resource_gate.worst_fraction = {recorded_worst} does not reproduce "
            f"from {report_name} ({recomputed['worst_fraction']})")
    if gate.get("passed") is True and not recomputed["passed"]:
        refusals.append(
            f"{name}: routed resource {recomputed['worst']} = "
            f"{recomputed['worst_fraction']} exceeds the contract's "
            f"constraints.{RESOURCE_FRACTION_KEY} = {limit} (recomputed from {report_name})")
    if limit is not None and measured_number(gate.get("limit")) is None:
        refusals.append(
            f"{name}: resource_gate.limit = {gate.get('limit')!r} is not a number")
    elif limit is not None and abs(measured_number(gate.get("limit")) - float(limit)) > TOLERANCE:
        refusals.append(
            f"{name}: resource_gate.limit = {gate.get('limit')!r} does not match the "
            f"contract's constraints.{RESOURCE_FRACTION_KEY} = {limit!r}")


def check_timing(name: str, timing, measured: dict, refusals: list[str]) -> dict | None:
    """Recompute the timing verdict from the routed timing report and compare it.

    `met` is a summary of slack, so it is checked as one: it must be true exactly when the
    recomputed worst setup and hold slack are both non-negative. A record that says `met`
    beside negative slack is the counterexample this rule exists for.
    """
    if timing is None:
        if name in TIMING_REQUIRED:
            refusals.append(
                f"{name}: records no timing block, so the routed timing verdict is missing")
        return None
    if not isinstance(timing, dict):
        refusals.append(
            f"{name}: timing is {type(timing).__name__}, expected an object carrying "
            f"wns_ns/whs_ns/met (found {timing!r})")
        return None
    wns, whs = measured.get("wns_ns"), measured.get("whs_ns")
    if wns is None or whs is None:
        refusals.append(
            f"{name}: {TIMING_REPORT} carries no readable WNS/WHS row, so the timing verdict "
            "cannot be recomputed")
        return None
    for key, expected in (("wns_ns", wns), ("whs_ns", whs)):
        recorded = measured_number(timing.get(key))
        if recorded is None:
            refusals.append(f"{name}: timing.{key} = {timing.get(key)!r} is not a number")
        elif abs(recorded - expected) > TOLERANCE:
            refusals.append(
                f"{name}: timing.{key} = {recorded} does not reproduce from {TIMING_REPORT} "
                f"({expected})")
    for key in ("tns_ns", "ths_ns"):
        if key in timing and measured.get(key) is not None:
            recorded = measured_number(timing.get(key))
            if recorded is None or abs(recorded - measured[key]) > TOLERANCE:
                refusals.append(
                    f"{name}: timing.{key} = {timing.get(key)!r} does not reproduce from "
                    f"{TIMING_REPORT} ({measured[key]})")
    slack_met = wns >= 0.0 and whs >= 0.0
    if timing.get("met") is not slack_met:
        refusals.append(
            f"{name}: timing.met is {timing.get('met')!r} but the recomputed slack is "
            f"WNS {wns} ns / WHS {whs} ns from {TIMING_REPORT}, so met must be {slack_met}")
    elif timing.get("met") is not True:
        refusals.append(
            f"{name}: timing is not met (WNS {wns} ns, WHS {whs} ns from {TIMING_REPORT})")
    return {"wns_ns": wns, "tns_ns": measured.get("tns_ns"), "whs_ns": whs,
            "ths_ns": measured.get("ths_ns"), "met": slack_met,
            "recomputed_from": TIMING_REPORT}


def verify_target_part(evidence: dict, wrapper: dict, contract_profile: dict,
                       refusals: list[str]) -> str | None:
    """The part the bitstream was built for must be the part the contract names."""
    declared = [wrapper.get("part"), (wrapper.get("board") or {}).get("part")]
    declared += [((contract_profile.get("fpga") or {}).get("part")
                  if isinstance(contract_profile.get("fpga"), dict) else None),
                 contract_profile.get("part")]
    declared = [value for value in declared if isinstance(value, str) and value]
    recorded = {}
    for name in HOST_STAGES:
        node = evidence.get(name)
        if isinstance(node, dict) and "part" in node:
            recorded[name] = node.get("part")
    for name, value in sorted(recorded.items()):
        if not isinstance(value, str) or not value:
            refusals.append(f"{name}: part = {value!r} is not a part number")
    distinct = set(recorded.values()) | set(declared)
    if len(distinct) > 1:
        refusals.append(
            f"the part is not consistent across the build records and the contract: "
            f"evidence {recorded}, wrapper/contract {sorted(set(declared))}")
        return None
    if not distinct:
        refusals.append(
            "no part number could be read from the build evidence, the wrapper or the "
            "contract's board profile")
        return None
    return distinct.pop()


def verify_convert_record(run_dir: Path, evidence: dict, refusals: list[str]) -> dict:
    """The conversion stage's record must not be an empty shell, and its config must match.

    `convert-evidence.json` carries no pass/fail by design, but it does carry which config
    was applied; a record that says nothing cannot support the package, and the config it
    names has to be the file that ships.
    """
    node = evidence.get("convert-evidence.json")
    if node is None:
        return {}
    if not isinstance(node, dict):
        refusals.append(
            f"convert-evidence.json is {type(node).__name__}, expected an object")
        return {}
    config_path = node.get("hls4ml_config_path")
    if not isinstance(config_path, str) or not config_path:
        refusals.append(
            f"convert-evidence.json records hls4ml_config_path = {config_path!r}; a "
            "conversion that does not name the config it applied cannot be packaged")
    recorded = node.get("hls4ml_config_sha256")
    if not isinstance(recorded, str) or not recorded:
        refusals.append(
            f"convert-evidence.json records hls4ml_config_sha256 = {recorded!r}, so the "
            "applied conversion config cannot be bound to the file that ships")
    else:
        shipped = run_dir / "p2-convert" / "project" / "hls4ml_config.yml"
        if not shipped.is_file():
            refusals.append(f"the hls4ml config the conversion applied is missing: {shipped}")
        elif sha256_file(shipped) != recorded:
            refusals.append(
                "hls4ml_config_sha256 does not bind the config that ships: the file is "
                f"{sha256_file(shipped)}, convert-evidence.json records {recorded}")
    return node


def verify_vivado_stages(run_dir: Path, contract: dict, evidence: dict, wrapper: dict,
                         contract_profile: dict, refusals: list[str]) -> dict:
    """Host synth/impl/bitstream: PASSED, resources and timing recomputed from the reports."""
    board = run_dir / HOST_BOARD
    project = run_dir / "p2-convert" / "project"
    limit = measured_number((contract.get("constraints") or {}).get(RESOURCE_FRACTION_KEY))
    if limit is None:
        refusals.append(
            f"the contract declares no numeric constraints.{RESOURCE_FRACTION_KEY}; the "
            "routed resource gate has no bound to check against")
    report: dict = {"stages": {}, "resource_gates": {}, "timing": {}}
    for name in HOST_STAGES:
        node = evidence.get(name)
        if node is None:
            continue
        if not isinstance(node, dict):
            refusals.append(f"{name} is {type(node).__name__}, expected an object")
            continue
        gate = node.get("resource_gate")
        recomputed = recompute_resource_gate(board, RESOURCE_REPORT[name], limit, refusals, name)
        check_resource_gate(name, gate, recomputed, limit, refusals)
        if recomputed is not None:
            report["resource_gates"][name] = recomputed
        timing_path = board / TIMING_REPORT
        measured = parse_timing(timing_path) if timing_path.is_file() else {}
        verdict = check_timing(name, node.get("timing"), measured, refusals)
        if verdict is not None:
            report["timing"][name] = verdict
        if name in TIMING_REQUIRED and not timing_path.is_file():
            refusals.append(
                f"{name}: the routed timing report is missing: {timing_path}")
        if not isinstance(node.get("artifacts"), dict) or not node.get("artifacts"):
            refusals.append(f"{name}: artifacts is empty, so nothing can be re-hashed")
        else:
            if "routed.dcp" not in node["artifacts"] and name != "host-vivado-synth-evidence.json":
                refusals.append(
                    f"{name}: artifacts does not record routed.dcp, so the routed checkpoint "
                    "cannot be reconciled with the bitstream build")
            report[name] = check_artifacts(node, refusals, name, (board, project))
        report["stages"][name] = node.get("status")
    report["part"] = verify_target_part(evidence, wrapper, contract_profile, refusals)
    return report


def verify_rtl_binding(board: Path, host_sim: dict, synth: dict, refusals: list[str]) -> dict:
    """Bind the value-checked RTL to the synthesised RTL from four independent sides.

    One recorded hash list is not a binding: a producer that rewrites the RTL can rewrite
    the records with it. So the same files are pinned from every independent side the run
    offers --

      1. the host simulation's `staged_rtl`,
      2. the synthesis stage's `staged_rtl.files`,
      3. the synthesis stage's `rtl_used.files` (the tool's own post-staging directory
         listing, written by the same evidence but never consulted before),
      4. the bytes on disk under `p3-build/host-board/rtl/`, and
      5. the wrapper's own `board/<name>` outputs, which the wrapper hashes as
         `generated_sources`.

    Sides 3 and 5 are what make a coordinated rewrite fail: the attack touches 1, 2 and the
    disk copy, and 3 and 5 catch it.
    """
    simulated = host_sim.get("staged_rtl")
    synthesised = (synth.get("staged_rtl") or {}).get("files")
    used = (synth.get("rtl_used") or {}).get("files")
    rtl_dir = board / "rtl"
    summary: dict = {"files": dict(simulated) if isinstance(simulated, dict) else {},
                     "count": len(simulated) if isinstance(simulated, dict) else 0,
                     "verified": False}
    if not isinstance(simulated, dict) or not simulated:
        refusals.append(
            "host-sim-evidence.json records no staged RTL hashes, so it cannot be shown "
            "which RTL the numbers describe")
        return summary
    if not isinstance(synthesised, dict) or not synthesised:
        refusals.append(
            "host-vivado-synth-evidence.json records no staged_rtl.files, so it cannot be "
            "shown that the value-checked RTL is the synthesised RTL")
        return summary
    if simulated != synthesised:
        differing = sorted(name for name in set(simulated) | set(synthesised)
                           if simulated.get(name) != synthesised.get(name))
        refusals.append(
            "the host simulation did not run the RTL that synthesis consumed; differing "
            f"files: {differing}")
        return summary
    if not isinstance(used, dict) or not used:
        refusals.append(
            "host-vivado-synth-evidence.json records no rtl_used.files, so the staged RTL "
            "is only pinned by the two records that a rewrite would change together")
        return summary
    for name, digest in sorted(simulated.items()):
        if used.get(name) != digest:
            refusals.append(
                f"rtl_used.files.{name} = {used.get(name)!r} disagrees with the staged RTL "
                f"the host simulation verified ({digest}); the synthesis stage's own "
                "directory listing does not contain the value-checked file")
    if not rtl_dir.is_dir():
        refusals.append(f"the staged RTL directory is missing: {rtl_dir}")
        return summary
    for name, digest in sorted(simulated.items()):
        candidate = rtl_dir / name
        if not candidate.is_file():
            refusals.append(f"staged RTL {name} is missing from {rtl_dir}")
            continue
        actual = sha256_file(candidate)
        if actual != digest:
            refusals.append(
                f"staged RTL {name} changed since the host simulation: the files on disk "
                "are not the ones that were value-checked")
        board_copy = board / name
        if board_copy.is_file() and sha256_file(board_copy) != actual:
            refusals.append(
                f"{name} differs between {board_copy} and {candidate}; the wrapper's own "
                "output and the RTL that was simulated and synthesised must be the same file")
    summary["verified"] = True
    return summary


def verify_numerical_gates(run_dir: Path, contract: dict, evidence: dict, wrapper: dict,
                           geometry, refusals: list[str]) -> dict:
    """Recompute every numerical gate from the raw artifacts.

    The recorded verdicts are compared against the recomputed ones field by field, so a
    number that drifted -- even by less than the tolerance -- is caught. This is the only
    place the host numbers are decided.
    """
    evidence_dir = run_dir / "evidence"
    board = run_dir / HOST_BOARD
    host_sim = evidence.get("host-sim/host-sim-evidence.json")
    if host_sim is None:
        return {}

    if host_sim.get("status") != "PASSED":
        refusals.append(
            f"host-sim-evidence.json: status={host_sim.get('status')!r} "
            f"(reasons: {host_sim.get('reasons')})")
    requested = host_sim.get("requested_samples")
    received = host_sim.get("received_samples")
    for field, value in (("requested_samples", requested), ("received_samples", received)):
        if isinstance(value, bool) or not isinstance(value, int):
            refusals.append(
                f"host-sim-evidence.json: {field} = {value!r} is not an integer sample "
                "count")
    if isinstance(requested, int) and not isinstance(requested, bool) and requested <= 0:
        refusals.append(
            f"host-sim-evidence.json: requested_samples = {requested!r} is not a positive "
            "sample count")
    if requested != received:
        refusals.append(
            f"host-sim-evidence.json: the simulation requested {requested} samples and "
            f"received {received}; coverage is a requirement, not a report")
    if host_sim.get("returncode") != 0:
        refusals.append(
            f"host-sim-evidence.json: the simulation exited "
            f"{host_sim.get('returncode')!r}, expected 0")
    if host_sim.get("timed_out") is not False:
        refusals.append(
            f"host-sim-evidence.json: timed_out is {host_sim.get('timed_out')!r}, expected "
            "False")
    for side in ("python", "hls"):
        gate = host_sim.get(side) or {}
        if gate.get("passed") is not True:
            refusals.append(
                f"host-sim-evidence.json: {side}.passed is {gate.get('passed')!r}, expected "
                f"True (reasons: {gate.get('reasons')})")
    if host_sim.get("hardware_verified") is not False:
        refusals.append(
            f"host-sim-evidence.json claims hardware_verified = "
            f"{host_sim.get('hardware_verified')!r}; no live board capture exists")
    if host_sim.get("on_board_numerical") != "NOT_RUN":
        refusals.append(
            f"host-sim-evidence.json records on_board_numerical = "
            f"{host_sim.get('on_board_numerical')!r}, expected 'NOT_RUN'")

    criterion = criterion_from_deployment(contract)
    expected_criterion = criterion.to_dict()
    for side in ("python", "hls"):
        recorded = (host_sim.get(side) or {}).get("criterion") or {}
        drift = {key: {"recorded": recorded.get(key), "recomputed": expected_criterion.get(key)}
                 for key in CRITERION_FIELDS if recorded.get(key) != expected_criterion.get(key)}
        if drift:
            refusals.append(
                f"host-sim-evidence.json: the contract's criterion changed since the "
                f"{side} gate ran: {drift}")

    # The RTL that was numerically verified has to be the RTL that synthesis consumed.
    rtl_binding = verify_rtl_binding(
        board, host_sim, evidence.get("host-vivado-synth-evidence.json") or {}, refusals)

    if geometry is None:
        return {"rtl_binding": rtl_binding}
    if host_sim.get("host_geometry") != geometry.to_dict():
        refusals.append(
            "host-sim-evidence.json was framed with a different geometry than the wrapper "
            "records; the frames cannot be attributed to this design")

    frames_files = sorted((board).rglob("response_frames.hex"))
    if not frames_files:
        refusals.append(
            f"the host response frames are missing under {board} "
            "(host-sim-project/**/response_frames.hex)")
        return {}
    if len(frames_files) > 1:
        refusals.append(
            "more than one response frame log is present under the host build, so the "
            f"packaged numbers would be ambiguous: {[str(p) for p in frames_files]}")
        return {}
    frames_path = frames_files[0]

    actual_frames_sha = sha256_file(frames_path)
    if host_sim.get("frames_sha256") != actual_frames_sha:
        refusals.append(
            "the host response frames changed since the simulation: recorded "
            f"{host_sim.get('frames_sha256')}, on disk {actual_frames_sha}")

    raw = [line.strip() for line in
           frames_path.read_text(encoding="utf-8", errors="replace").splitlines()
           if line.strip()]
    if isinstance(received, int) and len(raw) != received:
        refusals.append(
            f"the response frame log carries {len(raw)} frames but the evidence reports "
            f"{received} samples")
    decoded: list[list[float]] = []
    for index, line in enumerate(raw):
        try:
            frame = bytes.fromhex(line)
        except ValueError as exc:
            refusals.append(f"response frame {index} is not hex: {exc}")
            return {}
        try:
            _, values = __import__("protocol").decode_response(frame, index, geometry)
        except ValueError as exc:
            refusals.append(
                f"response frame {index} does not decode on the wrapper's geometry: {exc}")
            return {}
        decoded.append(values)
    if not decoded:
        refusals.append("the host response frame log is empty")
        return {}

    reference_json = evidence_dir / "python-output.json"
    csim_log = run_dir / "p3-build" / "csim" / "csim_results.log"
    if not reference_json.is_file():
        refusals.append(f"the python reference is missing: {reference_json}")
        return {}
    if not csim_log.is_file():
        refusals.append(f"the HLS C++ results log is missing: {csim_log}")
        return {}
    try:
        reference = json.loads(reference_json.read_text(encoding="utf-8"))
        cpp = read_results(csim_log)
    except (ValueError, OSError) as exc:
        refusals.append(f"the numerical reference artifacts cannot be read: {exc}")
        return {}

    recomputed = {
        "hls_cpp_vs_python": compare(
            "host_package_recheck_hls_cpp", reference, cpp, criterion,
            reference_source=str(reference_json), actual_source=str(csim_log)),
        "host_sim_vs_python": compare(
            "host_package_recheck_host_sim_python", reference, decoded, criterion,
            reference_source=str(reference_json), actual_source=str(frames_path)),
        "rtl_vs_hls_cpp": compare(
            "host_package_recheck_rtl", cpp, decoded, criterion,
            reference_source=str(csim_log), actual_source=str(frames_path)),
    }

    recorded_pairs = (
        ("hls_cpp_vs_python", (evidence.get("csim-evidence.json") or {}).get("numerical_gate") or {},
         "csim-evidence.json.numerical_gate"),
        ("host_sim_vs_python", (host_sim.get("python") or {}), "host-sim-evidence.json.python"),
        ("rtl_vs_hls_cpp", (host_sim.get("hls") or {}), "host-sim-evidence.json.hls"),
    )
    for key, recorded, label in recorded_pairs:
        gate = recomputed[key]
        same, differences = same_numbers(gate, recorded, NUMERIC_KEYS)
        if not same:
            refusals.append(
                f"the {label} gate does not reproduce from the raw artifacts: {differences}")
        if (gate.get("values") or {}).get("violations") != (recorded.get("values") or {}).get("violations"):
            refusals.append(
                f"the {label} gate violation count does not reproduce from the raw artifacts")
        if not gate.get("passed"):
            refusals.append(
                f"recomputed {label} gate FAILED: "
                f"{(gate.get('values') or {}).get('violations')}/"
                f"{(gate.get('values') or {}).get('total')} values out of tolerance "
                f"({gate.get('reasons')})")
    return {"gates": recomputed, "frames": str(frames_path),
            "frames_sha256": actual_frames_sha, "criterion": criterion,
            "requested_samples": requested, "rtl_binding": rtl_binding}


def verify_bitstream(run_dir: Path, evidence: dict, refusals: list[str]) -> dict:
    board = run_dir / HOST_BOARD
    record = ((evidence.get("host-vivado-bitstream-evidence.json") or {})
              .get("artifacts") or {}).get("bitstream") or {}
    expected_path = board / "board_top.bit"
    bit_path = Path(str(record["path"])).resolve() if record.get("path") else expected_path
    report = {"path": str(bit_path), "recorded_sha256": record.get("sha256")}
    if not record.get("path"):
        refusals.append("the bitstream evidence does not name the bitstream file")
    elif run_dir.resolve() not in bit_path.parents:
        refusals.append(f"the bitstream evidence points outside the run: {bit_path}")
    elif bit_path != expected_path.resolve():
        refusals.append(
            f"the bitstream evidence names {bit_path}, not this run's host bitstream "
            f"{expected_path}")
    if not record.get("sha256"):
        refusals.append("the bitstream evidence carries no sha256; the file cannot be bound "
                        "to the build")
    if not bit_path.is_file():
        refusals.append(f"the bitstream file is missing: {bit_path}")
        return report
    actual = sha256_file(bit_path)
    report["sha256"] = actual
    report["bytes"] = bit_path.stat().st_size
    if record.get("sha256") and actual != record["sha256"]:
        refusals.append(
            f"bitstream sha256 mismatch: file {actual} != evidence {record['sha256']}")
    bit_mtime = bit_path.stat().st_mtime
    sources = ("board_top.v", "board.xdc", "transport.v", "uart_rx.v", "uart_tx.v",
               "host_avld_adapter.v", "host_stream_adapter.v", "routed.dcp", "synth.dcp")
    for name in sources:
        candidate = board / name
        if candidate.is_file() and candidate.stat().st_mtime > bit_mtime:
            refusals.append(
                f"stale bitstream: {name} is newer than {bit_path.name}, so the bitstream "
                "was built from an earlier design")
    return report


def scan_honesty(value, label: str, refusals: list[str], trail: str = "") -> None:
    """Walk a record and refuse any field that reads as on-board verification.

    This is a machine-readable rule, not a substring scan: a package legitimately contains
    `hardware_verified: false`, so text matching would refuse this packager's own output.
    Only *values* decide, and only the fields whose meaning is known.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{trail}.{key}" if trail else key
            if key in HONESTY_FALSE_FIELDS:
                if item is not False:
                    refusals.append(
                        f"{label}: {path} is {item!r}; nothing was measured on hardware, so "
                        "it must be false")
            elif key in HONESTY_NOT_RUN_FIELDS:
                if not isinstance(item, str) or item.strip().upper() != "NOT_RUN":
                    refusals.append(
                        f"{label}: {path} is {item!r}; the stage was not run on hardware, so "
                        "it must be 'NOT_RUN'")
            elif key in HONESTY_SCOPE_FIELDS:
                if not isinstance(item, str) or item.strip().lower() not in UNVERIFIED_SCOPES:
                    refusals.append(
                        f"{label}: {path} is {item!r}, which reads as a verified scope; "
                        f"expected one of {sorted(UNVERIFIED_SCOPES)}")
            elif CLAIM_KEY.search(key) and isinstance(item, bool) and item:
                refusals.append(
                    f"{label}: {path} is true, which claims an on-board state this package "
                    "cannot support")
            elif CLAIM_KEY.search(key) and isinstance(item, str) \
                    and item.strip().upper() in POSITIVE_VERDICTS:
                refusals.append(
                    f"{label}: {path} = {item!r} claims an on-board state this package "
                    "cannot support")
            scan_honesty(item, label, refusals, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan_honesty(item, label, refusals, f"{trail}[{index}]")


def verify_copied_evidence_honesty(run_dir: Path, refusals: list[str]) -> None:
    """Every record that ships is checked, not only the three the gates read.

    The packager writes `hardware_verified: false` itself; the files it copies verbatim were
    the one carrier of an on-board claim that nothing inspected, so a later stage that
    started recording a capture would have had it shipped unremarked.
    """
    evidence_dir = run_dir / "evidence"
    for name in HONESTY_JSON_SOURCES:
        path = evidence_dir / name
        if not path.is_file():
            continue
        node = load_json(path, refusals)
        if node is None:
            continue
        scan_honesty(node, f"evidence/{name}", refusals)
    wrapper_path = run_dir / HOST_BOARD / "wrapper.json"
    if wrapper_path.is_file():
        wrapper = load_json(wrapper_path, refusals)
        if wrapper is not None:
            scan_honesty(wrapper, str(wrapper_path), refusals)
    for name in HONESTY_TEXT_SOURCES:
        path = run_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in TEXT_CLAIM_NEEDLES:
            found = needle.search(text)
            if found:
                refusals.append(
                    f"{name} carries the on-board claim {found.group(0)!r}; this package "
                    "ships it verbatim and nothing was measured on hardware")


def verify_hardware_capture(run_dir: Path, refusals: list[str]) -> dict | None:
    """A capture this packager cannot check must stop the run, not upgrade the claim."""
    summary = run_dir / "evidence" / "hardware-validation" / "summary.json"
    if not summary.is_file():
        return None
    refusals.append(
        f"{summary} exists: a live board capture is present, but this packager has no rule "
        "that can accept it; refusing rather than recording hardware verification it cannot "
        "check")
    return None


def verify_run(run_dir: Path, contract_path: Path, contract: dict) -> tuple[list[str], dict]:
    refusals: list[str] = []
    evidence_dir = run_dir / "evidence"
    evidence = {name: load_json(evidence_dir / name, refusals) for name in PASS_REQUIRED}
    evidence["convert-evidence.json"] = load_json(evidence_dir / "convert-evidence.json", refusals)
    evidence["host-sim/host-sim-evidence.json"] = load_json(
        evidence_dir / "host-sim" / "host-sim-evidence.json", refusals)
    for name, node in evidence.items():
        if node is not None and not isinstance(node, dict):
            refusals.append(
                f"evidence/{name} is a {type(node).__name__}, expected a JSON object")

    report: dict = {"stages": {}}
    for name in PASS_REQUIRED + ("convert-evidence.json",):
        node = evidence.get(name)
        if node is None:
            if not any(name in refusal for refusal in refusals):
                refusals.append(f"missing evidence file: evidence/{name}")
            report["stages"][name.removesuffix("-evidence.json")] = "MISSING"
            continue
        if not isinstance(node, dict):
            report["stages"][name.removesuffix("-evidence.json")] = "MALFORMED"
            continue
        if name == "convert-evidence.json":
            report["stages"]["convert"] = ("RECORDED" if node.get("hls4ml_config_path")
                                           else "INCOMPLETE")
            continue
        status = node.get("status")
        report["stages"][name.removesuffix("-evidence.json")] = status
        if status != "PASSED":
            refusals.append(f"{name}: status={status!r} (reasons: {node.get('reasons')})")
    host_sim = evidence.get("host-sim/host-sim-evidence.json")
    report["stages"]["host-sim"] = (host_sim.get("status")
                                    if isinstance(host_sim, dict) else "MISSING")
    if host_sim is None:
        refusals.append("missing evidence file: evidence/host-sim/host-sim-evidence.json")

    verify_host_layout(run_dir, refusals)
    wrapper = verify_wrapper(run_dir, contract_path, refusals)
    report["wrapper"] = wrapper

    board_profile_path = Path(str((wrapper.get("board") or {}).get("pins_profile") or ""))
    board_profile = None
    if not board_profile_path.is_file():
        refusals.append(
            f"the wrapper's board pin profile is missing: {board_profile_path}; the frame "
            "geometry cannot be checked against the board")
    else:
        board_profile = yaml.safe_load(board_profile_path.read_text(encoding="utf-8-sig"))
    geometry = load_geometry(contract_path, contract, wrapper,
                             run_dir / HOST_BOARD / "wrapper.json", board_profile, refusals)
    report["geometry"] = geometry.to_dict() if geometry else None

    profile_value = contract.get("board_profile")
    contract_profile = {}
    contract_profile_path = resolve(contract_path.parent, profile_value) if profile_value else None
    if contract_profile_path and contract_profile_path.is_file():
        contract_profile = yaml.safe_load(
            contract_profile_path.read_text(encoding="utf-8-sig")) or {}

    report["io_scope"] = verify_port_audit(run_dir, evidence, wrapper, refusals)
    report["convert"] = verify_convert_record(run_dir, evidence, refusals)
    report["artifacts"] = verify_vivado_stages(run_dir, contract, evidence, wrapper,
                                               contract_profile, refusals)
    if isinstance(host_sim, dict):
        report["numerical"] = verify_numerical_gates(run_dir, contract, evidence, wrapper,
                                                     geometry, refusals)
    else:
        report["numerical"] = {}
    report["bitstream"] = verify_bitstream(run_dir, evidence, refusals)
    report["hardware_capture"] = verify_hardware_capture(run_dir, refusals)
    report["rtl_binding"] = report["numerical"].get("rtl_binding")
    verify_copied_evidence_honesty(run_dir, refusals)

    if dig(evidence.get("csim-evidence.json") or {}, ("numerical_gate", "passed")) is not True:
        refusals.append(
            "csim-evidence.json: numerical_gate.passed is not True; the HLS C++ stage does "
            "not stand on its own")
    return refusals, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="assemble a deployment package from a verified host-I/O run")
    parser.add_argument("--run", type=Path, required=True, help="verified run directory")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true",
                        help="evaluate the gates, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing <release-root>/<model-id> directory")
    args = parser.parse_args()

    run_dir: Path = args.run.resolve()
    contract_path = find_contract(run_dir)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8-sig"))

    if not MODEL_ID_PATTERN.fullmatch(args.model_id or "") or args.model_id in {".", ".."}:
        print(json.dumps({
            "model": args.model_id, "variant": "host-io", "packaged": False,
            "refusals": [
                f"--model-id {args.model_id!r} is not a plain directory name; it becomes "
                "<release-root>/<model-id>, so it must match [A-Za-z0-9][A-Za-z0-9._-]* and "
                "must not be '.' or '..'"],
        }, indent=2, ensure_ascii=False))
        return 2

    refusals, report = verify_run(run_dir, contract_path, contract)
    if refusals:
        print(json.dumps({"model": args.model_id, "variant": "host-io", "packaged": False,
                          "refusals": refusals}, indent=2, ensure_ascii=False))
        return 2
    if args.check_only:
        print(json.dumps({"model": args.model_id, "variant": "host-io", "packaged": False,
                          "check": "OK", "report": report},
                         indent=2, ensure_ascii=False, default=str))
        return 0

    release_root = args.release_root.resolve()
    destination = release_root / args.model_id
    if destination.exists():
        if not args.force:
            raise SystemExit(
                f"a package already exists at {destination}. Re-packaging replaces it, so it "
                "has to be asked for explicitly: pass --force (the previous package is "
                "checked by --check-only first, not merged)."
            )
        # `--force` means "replace the package this tool wrote", not "delete whatever is at
        # that path". A typed model id can no longer escape the release root, but a stale or
        # mis-typed path can still point at somebody else's directory.
        existing_manifest = destination / "MANIFEST.json"
        if destination == release_root or not existing_manifest.is_file():
            raise SystemExit(
                f"refusing to --force replace {destination}: it does not contain a "
                "MANIFEST.json, so it is not a package this tool wrote. Move it aside "
                "yourself if it really is disposable."
            )
        if (load_json(existing_manifest, []) or {}).get("variant") != "host-io":
            raise SystemExit(
                f"refusing to --force replace {destination}: its MANIFEST.json is not a "
                "host-io package this packager wrote"
            )
        shutil.rmtree(destination)
    for sub in ("reports", "constraints", "evidence", "logs"):
        (destination / sub).mkdir(parents=True)

    board = run_dir / HOST_BOARD
    bitstream = report["bitstream"]
    shutil.copy2(Path(bitstream["path"]), destination / f"{args.model_id}.bit")

    for relative, target in REPORTS.items():
        copy_if_exists(run_dir / relative, destination / target)
    copy_if_exists(board / "board.xdc", destination / "constraints" / "board.xdc")
    copy_if_exists(board / "wrapper.json", destination / "board-wrapper.json")
    copy_if_exists(run_dir / AUDIT_DIR / "io-scope.json", destination / "io-scope.json")
    copy_if_exists(run_dir / "p2-convert" / "project" / "hls4ml_config.yml",
                   destination / "hls4ml_config.yaml")
    copy_if_exists(contract_path, destination / "deployment.yaml")
    profile_value = contract.get("board_profile")
    contract_profile_path = resolve(contract_path.parent, profile_value) if profile_value else None
    contract_profile = {}
    if copy_if_exists(contract_profile_path, destination / "board_profile.yaml"):
        contract_profile = yaml.safe_load(
            contract_profile_path.read_text(encoding="utf-8-sig")) or {}
    pins_path = pins_profile(SKILL_ROOT, contract_profile)
    copy_if_exists(pins_path, destination / "board_pins_profile.yaml")
    copy_if_exists(run_dir / "RUN.md", destination / "RUN.md")
    copy_if_exists(run_dir / "starting-state.json", destination / "starting-state.json")

    copy_if_exists(run_dir / "p3-build" / "csim" / "csim_results.log",
                   destination / "logs" / "csim_results.log")
    copy_if_exists(Path(report["numerical"]["frames"]),
                   destination / "logs" / "host_response_frames.hex")
    request_bytes = sorted((board).rglob("request_bytes.hex"))
    if request_bytes:
        copy_if_exists(request_bytes[0], destination / "logs" / "host_request_bytes.hex")
    copy_if_exists(run_dir / "evidence" / "host-sim" / "stdout.txt",
                   destination / "logs" / "host-sim-stdout.txt")
    for name in ("io-scope.json", "ports.tcl", "stdout.txt"):
        copy_if_exists(run_dir / AUDIT_DIR / name,
                       destination / "evidence" / "host-port-audit" / name)
    for name in COPIED_EVIDENCE:
        copy_if_exists(run_dir / "evidence" / name, destination / "evidence" / name)
    copy_if_exists(run_dir / "evidence" / "host-sim" / "host-sim-evidence.json",
                   destination / "evidence" / "host-sim" / "host-sim-evidence.json")
    # Ship the RTL the numbers were measured on, so the binding this package asserts can be
    # re-checked from the package alone instead of from the build directory it came from.
    staged_rtl = (report.get("rtl_binding") or {}).get("files") or {}
    staged_rtl_dir = board / "rtl"
    for name in sorted(staged_rtl):
        copy_if_exists(staged_rtl_dir / name, destination / "evidence" / "staged-rtl" / name)

    wrapper = report["wrapper"]
    geometry = report["geometry"]
    # The package documents the board it was actually built for. If the wrapper names no
    # readable profile, the port audit has already refused the package; the text then says
    # so rather than naming a board.
    try:
        facts = board_facts.facts_for_wrapper(wrapper)
    except ValueError:
        facts = None
    uart_text = (f"`uart_rx` {facts.uart_rx_pin}, `uart_tx` {facts.uart_tx_pin}, "
                 f"{facts.uart_iostandard}" if facts else "see board_profile.yaml")
    board_label = (facts.board_id if facts else
                   contract_profile.get("id") or contract_profile.get("board_id") or
                   "see board_profile.yaml")
    bitstream_evidence = evidence_for(run_dir, "host-vivado-bitstream-evidence.json")
    impl_evidence = evidence_for(run_dir, "host-vivado-impl-evidence.json")
    host_sim = evidence_for(run_dir, "host-sim/host-sim-evidence.json")
    csim_evidence = evidence_for(run_dir, "csim-evidence.json")
    gates = report["numerical"]["gates"]
    drc = drc_summary(board / "drc_routed.rpt")
    # The manifest carries the *recomputed* resource gate and timing, not the recorded ones:
    # they have been shown to be equal, and this way a record can never be the thing that
    # reaches the delivery directory.
    recomputed_gate = (report["artifacts"]["resource_gates"]
                       .get("host-vivado-impl-evidence.json")
                       or (report["artifacts"]["resource_gates"]
                           .get("host-vivado-bitstream-evidence.json")))
    recomputed_timing = (report["artifacts"]["timing"]
                         .get("host-vivado-impl-evidence.json")
                         or (report["artifacts"]["timing"]
                             .get("host-vivado-bitstream-evidence.json")))
    part = report["artifacts"].get("part")

    model_files: dict[str, dict] = {}
    references = [contract.get("model", {}).get("path")] + list(
        contract.get("model", {}).get("associated_files") or [])
    for relative in filter(None, references):
        candidate = resolve(contract_path.parent, relative)
        if candidate.is_file():
            model_files[relative] = {
                "sha256": sha256_file(candidate),
                "bytes": candidate.stat().st_size,
                "shipped_in_package": False,
                "why": "the model file lives in the frozen contract, which is packaged as "
                       "deployment.yaml; copying it here would duplicate that input",
            }

    deployment = {key: contract.get(key) for key in
                  ("precision", "reuse_factor", "strategy", "io_type", "top_dataflow",
                   "clock_period_ns", "backend", "layer_reuse_factors", "constraints")}

    criterion = report["numerical"]["criterion"]
    verification_lines = [
        f"# Verification card - {args.model_id} (host I/O variant)",
        "",
        "Generated by `bitstream-package/scripts/package_host_bitstream.py` from the run's",
        "own evidence files. Every number below is read from, or recomputed against, those",
        "files. The samples reach this design over UART, so the numerical gates are",
        "recomputed from the decoded response frames rather than from a sample ROM.",
        "",
        "| item | value |",
        "| --- | --- |",
        f"| run | `{run_dir.name}` |",
        f"| contract | `{contract_path.name}` (sha256 `{sha256_file(contract_path)}`) |",
        f"| deployment | {deployment.get('precision')}, rf={deployment.get('reuse_factor')}, "
        f"{deployment.get('io_type')}, {deployment.get('strategy')}, "
        f"clock {deployment.get('clock_period_ns')} ns |",
        f"| variant | host I/O over UART: {uart_text}, 115200 baud |",
        f"| bitstream | `{args.model_id}.bit`, {bitstream['bytes']:,} bytes |",
        f"| bitstream sha256 | `{bitstream['sha256']}` |",
        "",
        "## Gates (recomputed by the packager from the raw artifacts)",
        "",
        f"- criterion: `{criterion.rule}` - atol {criterion.atol} "
        f"(from {criterion.to_dict().get('atol_source')}), rtol {criterion.rtol}, "
        f"{criterion.samples} samples, per value",
    ]
    gate_titles = (
        ("hls_cpp_vs_python", "HLS C++ vs python reference"),
        ("host_sim_vs_python", "decoded UART response frames vs python reference"),
        ("rtl_vs_hls_cpp", "decoded UART response frames vs HLS C++"),
    )
    for key, title in gate_titles:
        gate = gates.get(key) or {}
        values = gate.get("values") or {}
        verification_lines.append(
            f"- {title}: {gate.get('passed')}, {values.get('violations')}/{values.get('total')} "
            f"violations, max error {gate.get('max_abs_error')}, margin "
            f"{gate.get('max_error_over_tolerance')}")
    verification_lines += [
        f"- routed resource gate: {recomputed_gate['passed']}, worst "
        f"{recomputed_gate['worst']} = "
        f"{recomputed_gate['worst_fraction']} "
        f"(limit {recomputed_gate['limit']}, from the contract), recomputed from "
        f"`{recomputed_gate['recomputed_from']}` |",
        f"- timing: WNS {recomputed_timing['wns_ns']} ns, "
        f"WHS {recomputed_timing['whs_ns']} ns, met={recomputed_timing['met']}, recomputed "
        f"from `{recomputed_timing['recomputed_from']}`",
        f"- DRC: {drc if drc else 'see reports/drc.rpt'}",
        "",
        "## What was NOT done",
        "",
        "- **not programmed**: no JTAG download, no on-board inference. `hardware_verified = false`.",
        "- **on-board numerical result**: `NOT_RUN`. The UART transport has been simulated",
        "  against the RTL that was staged for synthesis (the same files, by sha256, that",
        "  the synthesis stage consumed - this package refuses to assemble if those hashes",
        "  differ), but no capture exists from a physical board. A simulated transport is",
        "  not a board measurement, so the on-board numerical result stays `NOT_RUN`.",
        "- The bitstream is an artifact, not a correctness proof: the gates above are the proof.",
        "",
        "## How the host gate was fed",
        "",
        f"The {report['numerical'].get('requested_samples')} requests were framed with the geometry the",
        "wrapper materialised",
        f"(`host_geometry`, request {geometry['request_bytes']} B, response frame "
        f"{geometry['response_frame_bytes']} B), and the response frames were decoded with the",
        "same geometry. The geometry is never re-derived from the model's shape or the",
        "contract's precision: either the wrapper records it, or packaging is refused.",
        "",
        "## Routed port audit",
        "",
        "The routed top's ports were audited by opening the routed checkpoint under Vivado,",
        "and this package re-derives the verdict from that audit's recorded output: it",
        f"re-parses what the audit printed, recomputes the classification, and re-checks the",
        f"{board_label} pinout recorded in the board profile ({uart_text}). It does **not** "
        "re-run Vivado.",
        "",
    ]
    (destination / "VERIFICATION.md").write_text("\n".join(verification_lines), encoding="utf-8")

    readme_lines = [
        f"# {args.model_id} - {board_label} host-I/O bitstream package",
        "",
        "| item | value |",
        "| --- | --- |",
        f"| model | `{args.model_id}` |",
        f"| board / part | {contract_profile.get('id') or contract_profile.get('board_id') or 'see board_profile.yaml'} "
        f"/ `{bitstream_evidence.get('part')}` |",
        f"| top | `{bitstream_evidence.get('top')}` |",
        f"| variant | host I/O over UART ({uart_text}) |",
        f"| deployment | precision `{deployment.get('precision')}`, rf={deployment.get('reuse_factor')}, "
        f"{deployment.get('io_type')}, {deployment.get('strategy')}, "
        f"clock {deployment.get('clock_period_ns')} ns |",
        f"| resource gate | limit {recomputed_gate['limit']}, worst "
        f"{recomputed_gate['worst']} = {recomputed_gate['worst_fraction']} "
        f"(recomputed from `{recomputed_gate['recomputed_from']}`) |",
        f"| timing | WNS {recomputed_timing['wns_ns']} ns, "
        f"WHS {recomputed_timing['whs_ns']} ns, met={recomputed_timing['met']} "
        f"(recomputed from `{recomputed_timing['recomputed_from']}`) |",
        "",
        "## Numerical evidence (frozen criterion, recomputed at packaging time)",
        "",
        f"`{criterion.rule}`: atol {criterion.atol} "
        f"(source `{criterion.to_dict().get('atol_source')}`), rtol {criterion.rtol}, "
        f"{criterion.samples} samples, per-value.",
        "",
        "| gate | verdict | violations | max_abs_error | margin (max err / tol) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key, title in gate_titles:
        gate = gates.get(key) or {}
        values = gate.get("values") or {}
        readme_lines.append(
            f"| {title} | {gate.get('passed')} | {values.get('violations')} / "
            f"{values.get('total')} | {gate.get('max_abs_error')} | "
            f"{gate.get('max_error_over_tolerance')} |")
    readme_lines += [
        "",
        "## What was NOT done",
        "",
        "- **not programmed**: no JTAG download, no on-board inference. `hardware_verified = false`.",
        "- **on-board numerical result**: `NOT_RUN` - the transport is simulated, not measured.",
        "- The bitstream is an artifact, not a correctness proof: the gates above are the proof.",
        "",
        "## Content",
        "",
        "```",
        f"{destination.name}/",
        f"  {args.model_id}.bit          bitstream (sha256 {bitstream['sha256']})",
        "  MANIFEST.json                build metadata, gates, checksums",
        "  CHECKSUMS.sha256             sha256 of every file except MANIFEST.json itself",
        "  VERIFICATION.md              machine-generated verification card",
        "  deployment.yaml              frozen deployment contract",
        "  board_profile.yaml           the contract's board profile (part, clock source)",
        "  board_pins_profile.yaml      the skills' profile: board-facing pins + provenance",
        "  board-wrapper.json           host wrapper description, geometry and pin provenance",
        "  io-scope.json                routed port audit, re-derived at packaging time",
        "  hls4ml_config.yaml           conversion config actually applied",
        "  constraints/board.xdc        pin / clock constraints actually used",
        "  reports/                     timing, utilization, DRC, clocks, csynth",
        "  evidence/                    the gate evidence JSONs and the raw port audit",
        "  logs/                        csim log, host-simulation log and response frames",
        "```",
        "",
        "Reproduce: `RUN.md` (starting state, stage order, commands) and `starting-state.json`.",
        "",
    ]
    (destination / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")

    checksums = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "CHECKSUMS.sha256":
            checksums[path.relative_to(destination).as_posix()] = sha256_file(path)

    manifest = {
        "schema_version": 2,
        "package_version": "line-2026-09-27",
        "variant": "host-io",
        "build_date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "model": {"id": args.model_id, "format": contract.get("model", {}).get("format"),
                  "files": model_files},
        "target": {
            "board": contract_profile.get("id") or contract_profile.get("board_id"),
            "part": part or bitstream_evidence.get("part"),
            "top": bitstream_evidence.get("top"),
        },
        "deployment": deployment,
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "resources": {"used": impl_evidence.get("resources"),
                      "resource_gate": recomputed_gate,
                      "recorded_resource_gate": impl_evidence.get("resource_gate")},
        "timing": recomputed_timing,
        "staged_rtl": {"count": len(staged_rtl), "path": "evidence/staged-rtl",
                       "why": ("the RTL whose sha256 the numerical gate and synthesis "
                               "agree on ships here so the binding can be re-checked from "
                               "the package alone")},
        "routed_timing_anchor": {
            "report": TIMING_REPORT,
            "why": (f"the pad lines in this report are a second, hash-bound statement of "
                    f"{uart_text}; the routed port audit is cross-checked against them"),
        },
        "drc": drc,
        "host_geometry": {
            "source": str(board / "wrapper.json"),
            "source_sha256": sha256_file(board / "wrapper.json"),
            "recorded_by": "board-host-io/scripts/make_host_board.py (host_geometry)",
            "geometry": geometry,
        },
        "host_board_facts": (facts.to_record() if facts else
                             {"error": "the wrapper names no readable board profile"}),
        "host_transport": {
            "uart_pins": ({name: {"direction": direction,
                                  "pin": (facts.uart_rx_pin if name == "uart_rx"
                                          else facts.uart_tx_pin),
                                  "iostandard": facts.uart_iostandard}
                           for name, direction in sorted(HOST_PORT_DIRECTIONS.items())}
                          if facts else {}),
            "baudrate": geometry.get("baudrate"),
            "frames": {"path": report["numerical"].get("frames"),
                       "sha256": report["numerical"].get("frames_sha256"),
                       "count": report["numerical"].get("requested_samples")},
        },
        "validation": {
            "hls_csim": (evidence_for(run_dir, "csim-evidence.json") or {}).get("status"),
            "hls_csynth": (evidence_for(run_dir, "csynth-evidence.json") or {}).get("status"),
            "host_sim": (host_sim or {}).get("status"),
            "rtl_cosim": "NOT_RUN",
            "board_sim": "NOT_RUN",
            "board_inference": "NOT_RUN",
            "on_board_numerical": "NOT_RUN",
            "hardware_verified": False,
            "programmed": False,
            "verification_scope": "host-path-present-unverified",
            "notes": ("a bitstream is not hardware verification: nothing was programmed and no "
                      "on-board inference was run. The UART transport was simulated against the "
                      "RTL that was staged for synthesis, and the packager recomputed every "
                      "numerical gate from the raw artifacts before assembling this package."),
        },
        "gates": {
            "criterion": criterion.to_dict(),
            "hls_cpp_vs_python": {k: (gates.get("hls_cpp_vs_python") or {}).get(k) for k in
                                  ("passed", "max_abs_error", "max_error_over_tolerance",
                                   "reference_source", "actual_source")},
            "host_sim_vs_python": {k: (gates.get("host_sim_vs_python") or {}).get(k) for k in
                                   ("passed", "max_abs_error", "max_error_over_tolerance",
                                    "reference_source", "actual_source")},
            "rtl_vs_hls_cpp": {k: (gates.get("rtl_vs_hls_cpp") or {}).get(k) for k in
                               ("passed", "max_abs_error", "max_error_over_tolerance",
                                "reference_source", "actual_source")},
            "recomputed_by_packager": {
                "hls_cpp_vs_python": (gates.get("hls_cpp_vs_python") or {}).get("passed"),
                "rtl_vs_hls_cpp": (gates.get("rtl_vs_hls_cpp") or {}).get("passed"),
            },
            "evidence_statuses": report["stages"],
        },
        "checksums": checksums,
    }
    (destination / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (destination / "CHECKSUMS.sha256").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
        encoding="utf-8")

    print(json.dumps({
        "model": args.model_id,
        "variant": "host-io",
        "packaged": True,
        "destination": str(destination),
        "bitstream_sha256": bitstream["sha256"],
        "files_checksummed": len(checksums),
        "stages": report["stages"],
        "recomputed_gates": {
            key: {"passed": value.get("passed"),
                  "violations": (value.get("values") or {}).get("violations"),
                  "max_error_over_tolerance": value.get("max_error_over_tolerance")}
            for key, value in gates.items()
        },
        "hardware_verified": False,
        "on_board_numerical": "NOT_RUN",
    }, indent=2, ensure_ascii=False))
    return 0


def evidence_for(run_dir: Path, name: str) -> dict:
    path = run_dir / "evidence" / name
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
