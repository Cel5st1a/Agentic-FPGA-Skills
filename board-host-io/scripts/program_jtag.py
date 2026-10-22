"""Volatile JTAG configuration with pinned bitstream and port evidence.

The cable and the chain device name come from the board profile the bitstream's
wrapper was built for; the routed pins are checked against that same profile. On a
bench with more than one cable attached, the target is selected *under the named
cable* (`jtag_cable_name`), never by die name across the whole chain: two Artix-7
boards report the same `xc7a200t`, and the AX7A200B's `R4`/`L14`/`L15` pinout has
nothing to do with the Nexys Video's `R4`/`V18`/`AA19`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "scripts" / "line"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from attempt_evidence import CableLock, attempt_output  # noqa: E402
import board_facts  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_program_stdout(stdout: str, cable_serial: str, target_name: str) -> None:
    if cable_serial not in stdout:
        raise ValueError("expected JTAG cable serial absent from programming log")
    if target_name not in stdout:
        raise ValueError(f"expected {target_name} absent from programming log")
    if not re.search(r"AFD_AFTER FPGA is configured", stdout):
        raise ValueError("FPGA is not configured after JTAG download")
    required = {"CRC ERROR": "0", "END OF STARTUP (EOS) STATUS": "1",
                "DONE PIN": "1", "IDCODE ERROR": "0", "BAD PACKET ERROR": "0"}
    for name, expected in required.items():
        pattern = rf"{re.escape(name)}\s*(?:\([^\n]*?\))?\s*:\s*{expected}(?:\s|$)"
        if not re.search(pattern, stdout, re.M):
            raise ValueError(f"JTAG status {name} did not equal {expected}")


def validate_inputs(bitstream: Path, scope_path: Path, bit_evidence_path: Path) -> dict:
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    evidence = json.loads(bit_evidence_path.read_text(encoding="utf-8"))
    if not scope.get("host_input_output_available") or not scope.get("wrapper_ports_match"):
        raise ValueError("routed host port audit did not pass")
    if (scope.get("verification_scope") != "host-path-present-unverified" or
            scope.get("on_board_numerical") != "NOT_RUN" or
            scope.get("hardware_verified") is not False):
        raise ValueError("port audit has an invalid pre-program verification scope")
    details = scope.get("ports") or {}
    for name, direction in (("uart_rx", "IN"), ("uart_tx", "OUT")):
        if (details.get(name) or {}).get("direction") != direction:
            raise ValueError(f"routed {name} must be an {direction} port")
    if scope.get("checkpoint_sha256") != sha256_file(Path(scope["checkpoint"])):
        raise ValueError("routed checkpoint changed since port audit")
    wrapper = Path(scope["wrapper"])
    if wrapper.resolve() != (bitstream.parent / "wrapper.json").resolve():
        raise ValueError("port audit wrapper is not the bitstream board wrapper")
    if Path(scope["checkpoint"]).resolve() != (bitstream.parent / "routed.dcp").resolve():
        raise ValueError("port audit checkpoint is not the bitstream routed checkpoint")
    if scope.get("wrapper_sha256") != sha256_file(wrapper):
        raise ValueError("board wrapper changed since port audit")
    if bitstream.resolve() != (wrapper.parent / "board_top.bit").resolve():
        raise ValueError("bitstream is not from the audited host board directory")
    if evidence.get("status") != "PASSED":
        raise ValueError("bitstream stage did not pass")
    artifacts = evidence.get("artifacts") or {}
    if artifacts.get("bitstream", {}).get("sha256") != sha256_file(bitstream):
        raise ValueError("bitstream hash disagrees with build evidence")
    if artifacts.get("routed.dcp", {}).get("sha256") != scope["checkpoint_sha256"]:
        raise ValueError("port audit checkpoint differs from bitstream build checkpoint")
    rtl = evidence.get("rtl_used", {}).get("files", {})
    wrapper_record = json.loads(wrapper.read_text(encoding="utf-8"))
    for name in ("board_top.v", "board.xdc", *wrapper_record.get("extra_rtl", [])):
        if rtl.get(name) != sha256_file(wrapper.parent / name):
            raise ValueError(f"built RTL differs from audited host board: {name}")
    # The routed pins must match the profile this wrapper was built for. Checked here as
    # well as at port-audit time so that a scope file carried over from another board
    # cannot program this bitstream.
    facts = board_facts.facts_for_wrapper(wrapper_record)
    board_facts.require_routed_uart(details, facts)
    return {"bitstream": str(bitstream.resolve()), "bitstream_sha256": sha256_file(bitstream),
            "routed_checkpoint_sha256": scope["checkpoint_sha256"],
            "io_scope_sha256": sha256_file(scope_path),
            "bitstream_evidence_sha256": sha256_file(bit_evidence_path),
            "board": facts.to_record()}


def tcl_program(bitstream: Path, cable_serial: str, target_name: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z]+", cable_serial):
        raise ValueError("JTAG cable serial contains unsupported characters")
    if not re.fullmatch(r"[0-9A-Za-z_]+", target_name):
        raise ValueError("JTAG target name contains unsupported characters")
    return f"""connect -url tcp:127.0.0.1:3121
after 1000
set chain [jtag targets]
puts $chain
if {{[string first "{cable_serial}" $chain] < 0}} {{error "Expected JTAG cable absent"}}
jtag targets -set -filter {{jtag_cable_name =~ "*{cable_serial}*" && name == "{target_name}"}}
set selected [jtag targets]
puts "AFD_SELECTED $selected"
if {{[regexp -all {{\\*}} $selected] != 1}} {{error "Expected exactly one {target_name} under cable {cable_serial}"}}
if {{[string first "{target_name}" $selected] < 0}} {{error "Expected {target_name} under the named cable"}}
puts "AFD_BEFORE [fpga -state]"
fpga -file {{{bitstream.resolve().as_posix()}}}
puts "AFD_AFTER [fpga -state]"
puts [fpga -config-status]
disconnect
exit
"""


def run_xsdb(command: list[str], output: Path, timeout: float = 120):
    """Reap the entire launcher/tool tree before the caller releases its cable lock."""
    process_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == "nt" else {"start_new_session": True})
    with (output / "stdout.txt").open("w", encoding="utf-8") as out, \
            (output / "stderr.txt").open("w", encoding="utf-8") as err:
        process = subprocess.Popen(command, cwd=output, stdout=out, stderr=err,
                                   **process_options)
        try:
            returncode = process.wait(timeout=timeout)
        except BaseException:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            raise
    return subprocess.CompletedProcess(command, returncode,
        (output / "stdout.txt").read_text(encoding="utf-8", errors="replace"),
        (output / "stderr.txt").read_text(encoding="utf-8", errors="replace"))


def program(bitstream: Path, scope: Path, bit_evidence: Path, output: Path,
            cable_serial: str | None, xsdb: Path) -> dict:
    bitstream, scope, bit_evidence, output = map(Path, (bitstream, scope, bit_evidence, output))
    proof = validate_inputs(bitstream, scope, bit_evidence)
    facts = proof["board"]
    declared_cable = (facts.get("jtag") or {}).get("cable_serial")
    target_name = (facts.get("jtag") or {}).get("target_name")
    if not target_name:
        raise ValueError(f"board profile {facts['pins_profile']} declares no jtag.target_name; "
                         f"the chain device is never guessed")
    # A session may name a different cable than the profile's verified one (the AX7A200B's
    # cable is an accessory, not part of the board); the record says which it was.
    cable_source = "command line" if cable_serial else "board profile"
    cable_serial = cable_serial or declared_cable
    if not cable_serial:
        raise ValueError(f"board profile {facts['pins_profile']} declares no jtag.cable_serial "
                         f"and no --cable-serial was given")
    script = output / "program.tcl"
    command = (["cmd", "/c", str(xsdb), str(script)] if xsdb.suffix.lower() in {".bat", ".cmd"}
               else [str(xsdb), str(script)])
    summary = {**proof, "programmed": False, "mode": "volatile JTAG",
               "cable_serial": cable_serial, "cable_source": cable_source,
               "declared_cable_serial": declared_cable, "target_name": target_name,
               "command": command}
    with attempt_output(output, summary):
        try:
            script.write_text(tcl_program(bitstream, cable_serial, target_name), encoding="utf-8")
            with CableLock(cable_serial):
                result = run_xsdb(command, output, timeout=120)
            (output / "stdout.txt").write_text(result.stdout, encoding="utf-8")
            (output / "stderr.txt").write_text(result.stderr, encoding="utf-8")
            summary["returncode"] = result.returncode
            if result.returncode:
                raise ValueError(f"XSDB exited {result.returncode}")
            validate_program_stdout(result.stdout, cable_serial, target_name)
            summary["programmed"] = True
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            summary["error"] = str(exc)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitstream", type=Path, required=True)
    parser.add_argument("--io-scope", type=Path, required=True)
    parser.add_argument("--bitstream-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cable-serial", default=None,
                        help="defaults to the board profile's jtag.cable_serial")
    parser.add_argument("--xsdb", type=Path,
                        default=Path(r"D:\vitis\2025.2\Vivado\bin\xsdb.bat"))
    args = parser.parse_args()
    summary = program(args.bitstream, args.io_scope, args.bitstream_evidence,
                      args.output, args.cable_serial, args.xsdb)
    print(json.dumps(summary, indent=2))
    return 0 if summary["programmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
