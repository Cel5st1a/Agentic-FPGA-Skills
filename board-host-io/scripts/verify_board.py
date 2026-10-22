"""Capture live UART results and compare them using the deployment's frozen criterion."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import sys
import time
from pathlib import Path

import yaml


SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from line.geometry import flatten_sample as flatten, sample_rows
from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from host_geometry import geometry_for_wrapper  # noqa: E402
from protocol import encode_request, exchange  # noqa: E402
import board_facts  # noqa: E402
from attempt_evidence import attempt_output, write_json_atomic  # noqa: E402
from cycle_metrics import cycle_counter_record  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    write_json_atomic(path, value)


def read_hls_results(path: Path) -> list[list[float]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append([float(value) for value in line.split()])
    return rows


def provenance_sources(deployment: Path, inputs_path: Path, python_path: Path, hls_path: Path,
                       model_path: Path, bitstream: Path, wrapper: Path, io_scope: Path,
                       program_summary: Path) -> list[Path]:
    """Every file the on-board verdict depends on, in one place so it is testable.

    `host_geometry.py` is included because it now holds the frame geometry — overheads,
    payload and response lengths, scale factors — and therefore determines the bytes that go
    on the wire. Hashing `protocol.py` alone would leave the audit record not covering the
    derivation it depends on.
    """
    here = Path(__file__)
    return [deployment, inputs_path, python_path, hls_path, model_path, bitstream.resolve(),
            wrapper, io_scope.resolve(), program_summary.resolve(), here,
            here.with_name("protocol.py"), here.with_name("host_geometry.py"),
            SKILLS / "scripts" / "line" / "geometry.py"]


def evaluate_capture(deployment: Path, python_reference, hls_reference, outputs: list[list[float]]) -> dict:
    contract = yaml.safe_load(Path(deployment).read_text(encoding="utf-8-sig"))
    criterion = criterion_from_deployment(contract)
    python_gate = compare("on_board_vs_python", python_reference, outputs, criterion)
    hls_gate = compare("on_board_vs_hls", hls_reference, outputs, criterion)
    return {"status": ("HARDWARE_NUMERICAL_PASSED"
                       if python_gate["passed"] and hls_gate["passed"] else "FAILED"),
            "python": python_gate, "hls": hls_gate}


def preflight(deployment: Path, board: Path, bitstream: Path,
              io_scope: Path, program_summary: Path) -> dict:
    deployment, board, bitstream = map(lambda p: Path(p).resolve(),
                                       (deployment, board, bitstream))
    run_dir = deployment.parents[2]
    for path in (board, bitstream, io_scope.resolve(), program_summary.resolve()):
        if not path.is_relative_to(run_dir):
            raise ValueError(f"hardware artifact is outside this one-contract run: {path}")
    wrapper_path = board / "wrapper.json"
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    scope = json.loads(io_scope.read_text(encoding="utf-8"))
    program = json.loads(program_summary.read_text(encoding="utf-8"))
    if wrapper.get("contract_sha256") != sha256_file(deployment):
        raise ValueError("host wrapper does not belong to this frozen deployment")
    for key in ("source_wrapper", "core_verilog", "model_path"):
        if not Path(wrapper.get(key, "")).resolve().is_relative_to(run_dir):
            raise ValueError(f"host {key} came from a different run")
    if wrapper.get("model_sha256") != sha256_file(Path(wrapper["model_path"])):
        raise ValueError("model file changed after host board generation")
    if not wrapper.get("host_input_output_available"):
        raise ValueError("host wrapper has no declared input/output path")
    if not scope.get("host_input_output_available") or not scope.get("wrapper_ports_match"):
        raise ValueError("routed checkpoint host port audit did not pass")
    if scope.get("wrapper_sha256") != sha256_file(wrapper_path):
        raise ValueError("host wrapper changed after routed port audit")
    if scope.get("checkpoint_sha256") != sha256_file(Path(scope["checkpoint"])):
        raise ValueError("routed checkpoint changed after port audit")
    if program.get("programmed") is not True:
        raise ValueError("JTAG configuration was not verified")
    if program.get("bitstream_sha256") != sha256_file(bitstream):
        raise ValueError("programmed bitstream hash differs from requested bitstream")
    if program.get("io_scope_sha256") != sha256_file(io_scope):
        raise ValueError("JTAG configuration used a different port audit")
    if bitstream != (board / "board_top.bit"):
        raise ValueError("bitstream is not from this host board")
    return {"run": run_dir, "wrapper": wrapper_path, "scope": scope,
            "program": program, "bitstream": bitstream}


def require_selected_bridge(wrapper_path: Path, port: str, devices=None) -> str:
    """The port must carry *this* board's UART bridge; returns the measured serial number.

    The AX7A200B reaches a CP2102 (10c4:ea60) and the Nexys Video an FT232R (0403:6001), so
    accepting whichever bridge is present could capture the wrong board's traffic. The
    expectation comes from the profile the wrapper names -- and the wrapper is a *record on
    disk*, read here rather than passed in as an already-parsed dict (the first version of
    this check passed `preflight`'s Path straight to the reader and died with
    `AttributeError: 'WindowsPath' object has no attribute 'get'`, after a real board had
    already been configured for it).
    """
    from serial.tools import list_ports

    found = list(devices if devices is not None else
                 [device for device in list_ports.comports() if device.device == port])
    if len(found) != 1:
        raise ValueError(f"no single serial device on {port}")
    wrapper = json.loads(Path(wrapper_path).read_text(encoding="utf-8"))
    board_facts.require_usb_bridge(found[0], board_facts.facts_for_wrapper(wrapper))
    return found[0].serial_number


def capture(deployment: Path, board: Path, bitstream: Path, io_scope: Path,
            program_summary: Path, port: str, serial_number: str,
            output: Path, timeout_s: float = 2.0) -> dict:
    deployment = Path(deployment).resolve()
    board, bitstream, io_scope, program_summary, output = map(Path,
        (board, bitstream, io_scope, program_summary, output))
    proof = preflight(deployment, board, bitstream, io_scope, program_summary)
    run_dir = proof["run"]
    if not output.resolve().is_relative_to(run_dir):
        raise ValueError("capture output must live in this one-contract run")
    contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
    inputs_path = (deployment.parent / contract["verification"]["inputs"]).resolve()
    payload = json.loads(inputs_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = next((payload[key] for key in ("inputs", "samples", "data") if key in payload), None)
    if not isinstance(payload, list) or not payload:
        raise ValueError("contract input file has no samples")
    inputs = sample_rows(payload, "inputs")
    # Same helper and inputs as make_host_board.py and simulate_host.py.
    wrapper = json.loads((board / "wrapper.json").read_text(encoding="utf-8"))
    geometry = geometry_for_wrapper(deployment, contract, wrapper)
    if any(len(row) != geometry.in_elements for row in inputs) or len(inputs) > 65535:
        raise ValueError(f"UART transport requires 1..65535 samples of "
                         f"{geometry.in_elements} inputs")
    criterion = criterion_from_deployment(contract)
    if criterion.samples_source != "code_default" and len(inputs) != criterion.samples:
        raise ValueError("input file count differs from pinned verification.samples")
    python_path = run_dir / "evidence" / "python-output.json"
    hls_path = run_dir / "p3-build" / "csim" / "csim_results.log"
    python_reference = json.loads(python_path.read_text(encoding="utf-8"))
    hls_reference = read_hls_results(hls_path)
    model_path = (deployment.parent / contract["model"]["path"]).resolve()
    sources = provenance_sources(deployment, inputs_path, python_path, hls_path, model_path,
                                 bitstream, proof["wrapper"], io_scope, program_summary)
    for path in sources:
        if not path.is_file():
            raise ValueError(f"missing provenance source: {path}")

    from serial import Serial

    # The bridge is a board fact: the AX7A200B reaches a CP2102 (10c4:ea60) and the Nexys
    # Video an FT232R (0403:6001), so accepting whichever bridge is present could capture
    # the wrong board's traffic. Checked against the profile this wrapper was built for.
    # Record what was actually on the port rather than what the caller expected.
    serial_number = require_selected_bridge(proof["wrapper"], port) or serial_number

    summary = {"status": "FAILED", "hardware_numerical_verified": False,
               "requested_samples": len(inputs), "received_samples": 0,
               "port": port, "serial_number": serial_number, "baudrate": geometry.baudrate,
               "bitstream_sha256": sha256_file(bitstream), "compute_cycles": [],
               "cycle_counter": cycle_counter_record(wrapper),
               "host_geometry": geometry.to_dict(),
               "criterion": criterion.to_dict()}
    outputs, cycles = [], []
    base = secrets.randbelow(65536 - len(inputs) + 1)
    with attempt_output(output, summary):
        write_json(output / "provenance.json", {str(path): sha256_file(path) for path in sources})
        shutil.copyfile(Path(__file__), output / "host-source.py")
        shutil.copyfile(Path(__file__).with_name("protocol.py"), output / "protocol-source.py")
        shutil.copyfile(Path(__file__).with_name("host_geometry.py"),
                        output / "host-geometry-source.py")
        shutil.copyfile(Path(__file__).with_name("cycle_metrics.py"),
                        output / "cycle-metrics-source.py")
        try:
            with (output / "events.jsonl").open("w", encoding="utf-8") as events:
                def record(event):
                    events.write(json.dumps(event, ensure_ascii=False) + "\n")
                    events.flush()
        
                connection = Serial()
                connection.port, connection.baudrate = port, geometry.baudrate
                connection.timeout, connection.write_timeout = 0.1, timeout_s
                connection.dtr = connection.rts = False
                try:
                    connection.open()
                    if connection.in_waiting:
                        startup = connection.read(connection.in_waiting)
                        record({"kind": "unexpected_startup", "rx_hex": startup.hex()})
                        raise ValueError("unexpected UART bytes before first request")
                    for index, row in enumerate(inputs):
                        sequence = base + index
                        tx = encode_request(row, sequence, geometry)
                        record({"kind": "request", "sample": index, "sequence": sequence,
                                "tx_hex": tx.hex()})
                        start = time.monotonic()
                        _, rx, count, values = exchange(
                            connection, row, sequence, geometry, timeout_s,
                            on_receive=lambda chunk: record({"kind": "response_chunk", "sample": index,
                                                             "rx_hex": chunk.hex()}))
                        record({"kind": "validated_response", "sample": index, "sequence": sequence,
                                "rx_hex": rx.hex(), "cycles": count,
                                "elapsed_s": time.monotonic() - start})
                        outputs.append(values)
                        cycles.append(count)
                    gates = evaluate_capture(deployment, python_reference, hls_reference, outputs)
                    write_json(output / "python-evidence.json", gates["python"])
                    write_json(output / "hls-evidence.json", gates["hls"])
                    summary["status"] = gates["status"]
                    summary["hardware_numerical_verified"] = gates["status"] == "HARDWARE_NUMERICAL_PASSED"
                    summary["comparisons"] = {"python": gates["python"], "hls": gates["hls"]}
                except (OSError, ValueError, TimeoutError) as exc:
                    summary["error"] = str(exc)
                    record({"kind": "error", "error": str(exc)})
                finally:
                    connection.close()
        finally:
            summary["received_samples"] = len(outputs)
            summary["compute_cycles"] = cycles
            write_json(output / "fpga-output.json", outputs)
    manifest = [{"path": path.name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for path in sorted(output.iterdir()) if path.is_file()]
    write_json(output / "manifest.json", manifest)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--bitstream", type=Path, required=True)
    parser.add_argument("--io-scope", type=Path, required=True)
    parser.add_argument("--program-summary", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--serial-number", default=None,
                        help="defaults to the board profile's uart.host_bridge serial")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    args = parser.parse_args()
    summary = capture(args.deployment, args.board, args.bitstream, args.io_scope,
                      args.program_summary, args.port, args.serial_number,
                      args.output, args.timeout_s)
    print(json.dumps(summary, indent=2))
    return 0 if summary["hardware_numerical_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
