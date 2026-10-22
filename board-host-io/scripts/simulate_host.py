"""Exercise host framing through the generated board top and real HLS RTL.

The verified UART macros are bypassed at their byte interface to keep a full
contract run tractable in xsim. The board top, transport, stream adapter and
actual HLS core are elaborated together; no stub accelerator is substituted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml


SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "scripts"))
sys.path.insert(0, str(SKILLS / "vivado-manager" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from line.geometry import flatten_sample as flatten, sample_rows
from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from line.run_line import Lock  # noqa: E402
from host_geometry import Geometry, geometry_for_wrapper  # noqa: E402
import board_facts  # noqa: E402
from protocol import decode_response, encode_request  # noqa: E402
from run_board_sim import DEFAULT_VIVADO, run_tool  # noqa: E402
from stage_rtl import stage_rtl  # noqa: E402
from cycle_metrics import cycle_counter_record  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[list[float]]:
    return [[float(value) for value in line.split()]
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render_testbench(sample_count: int, geometry: Geometry,
                     facts: board_facts.BoardFacts) -> str:
    if not 1 <= sample_count <= 65536:
        raise ValueError("sample count must fit the uint16 sequence space")
    # The testbench forces the generated top's internal clock and reset nets, so the clock
    # input port is only here to satisfy the port list; its shape still has to be the shape
    # this board's top actually declares (one single-ended pin, or a differential pair).
    clock_ports = facts.clock_ports
    if facts.clock_kind == "differential":
        clock_ties = f".{clock_ports[0]}(1'b0), .{clock_ports[1]}(1'b1)"
    else:
        clock_ties = f".{clock_ports[0]}(1'b0)"
    return f"""`timescale 1ns/1ps
module board_top_tb;
    localparam integer SAMPLE_COUNT = {sample_count};
    localparam integer REQUEST_BYTES = {geometry.request_bytes};
    localparam integer RESPONSE_BYTES = {geometry.response_frame_bytes};
    reg clk = 0, rst_n = 0;
    reg rx_valid_i = 0, tx_ready_i = 1;
    reg [7:0] rx_data_i = 0;
    reg [7:0] request_bytes [0:SAMPLE_COUNT*REQUEST_BYTES-1];
    reg [7:0] response_bytes [0:SAMPLE_COUNT*RESPONSE_BYTES-1];
    wire uart_tx;
    integer result_file, sample, byte_index, output_index, waited, tx_count = 0;
    always #5 clk = ~clk;
    board_top dut({clock_ties}, .rst_n(rst_n),
                  .uart_rx(1'b1), .uart_tx(uart_tx));

    // Force only the byte-level UART boundary and core clock/reset. This speeds
    // serial time while preserving the generated top's real transport/core wiring.
    initial begin
        force dut.clk = clk;
        force dut.internal_rst_n = rst_n;
        force dut.rx_data = rx_data_i;
        force dut.rx_valid = rx_valid_i;
        force dut.tx_ready = tx_ready_i;
    end

    always @(posedge clk) begin
        if (rst_n && dut.tx_valid && tx_ready_i) begin
            if (tx_count >= SAMPLE_COUNT*RESPONSE_BYTES)
                $fatal(1, "extra response byte");
            response_bytes[tx_count] <= dut.tx_data;
            tx_count <= tx_count + 1;
            tx_ready_i <= 0;
        end else if (!tx_ready_i) tx_ready_i <= 1;
    end

    initial begin
        $readmemh("request_bytes.hex", request_bytes);
        result_file = $fopen("response_frames.hex", "w");
        if (result_file == 0) $fatal(1, "cannot open response_frames.hex");
        repeat (12) @(negedge clk);
        rst_n = 1;
        repeat (12) @(negedge clk);
        for (sample = 0; sample < SAMPLE_COUNT; sample = sample + 1) begin
            for (byte_index = 0; byte_index < REQUEST_BYTES; byte_index = byte_index + 1) begin
                @(negedge clk);
                rx_data_i = request_bytes[sample*REQUEST_BYTES + byte_index];
                rx_valid_i = 1;
                @(negedge clk);
                rx_valid_i = 0;
            end
            waited = 0;
            while (tx_count < (sample+1)*RESPONSE_BYTES && waited < 100000) begin
                @(negedge clk);
                waited = waited + 1;
            end
            if (tx_count != (sample+1)*RESPONSE_BYTES)
                $fatal(1, "partial or missing response for sample %0d: %0d bytes", sample,
                       tx_count - sample*RESPONSE_BYTES);
            for (output_index = 0; output_index < RESPONSE_BYTES; output_index = output_index + 1)
                $fwrite(result_file, "%02x", response_bytes[sample*RESPONSE_BYTES+output_index]);
            $fwrite(result_file, "\\n");
            $fflush(result_file);
            repeat (8) @(negedge clk);
        end
        repeat (8) @(negedge clk);
        if (tx_count != SAMPLE_COUNT*RESPONSE_BYTES)
            $fatal(1, "unexpected response byte count %0d", tx_count);
        $fclose(result_file);
        $display("HOST_INTEGRATION_PASS samples=%0d", SAMPLE_COUNT);
        $finish;
    end
endmodule
"""


def decode_frames(lines: list[str], sequences: list[int],
                  geometry: Geometry) -> tuple[list[int], list[list[float]]]:
    if len(lines) != len(sequences):
        raise ValueError(f"response count {len(lines)} != request count {len(sequences)}")
    cycles, outputs = [], []
    for line, sequence in zip(lines, sequences):
        frame = bytes.fromhex(line.strip())
        count, values = decode_response(frame, sequence, geometry)
        cycles.append(count)
        outputs.append(values)
    return cycles, outputs


def simulation_completed(stdout: str, sample_count: int) -> bool:
    marker = rf"(?m)^HOST_INTEGRATION_PASS samples={sample_count}$"
    return (re.search(marker, stdout) is not None
            and "Fatal:" not in stdout and "ERROR:" not in stdout)


def simulate(deployment: Path, board: Path, output: Path, vivado: Path = DEFAULT_VIVADO) -> dict:
    deployment, board, output = (Path(p).resolve() for p in (deployment, board, output))
    run_dir = deployment.parents[2]
    if not board.is_relative_to(run_dir) or not output.is_relative_to(run_dir):
        raise ValueError("host simulation must stay inside one contract run")
    if output.exists() and any(output.iterdir()):
        raise ValueError("host simulation output already exists; use a fresh run")
    wrapper = json.loads((board / "wrapper.json").read_text(encoding="utf-8"))
    if wrapper.get("skill") != "board-host-io" or not wrapper.get("host_input_output_available"):
        raise ValueError("not a host board generated by this skill")
    if wrapper.get("contract_sha256") != sha256_file(deployment):
        raise ValueError("host board contract hash differs from run contract")
    for key in ("core_verilog", "source_wrapper", "model_path"):
        path = Path(wrapper[key]).resolve()
        if not path.is_relative_to(run_dir) or not path.is_file():
            raise ValueError(f"{key} is not a file in the same run")
    if sha256_file(Path(wrapper["core_verilog"])) != wrapper["core_verilog_sha256"]:
        raise ValueError("core Verilog changed after host board generation")
    if sha256_file(Path(wrapper["model_path"])) != wrapper["model_sha256"]:
        raise ValueError("model changed after host board generation")

    contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
    criterion = criterion_from_deployment(contract)
    # Same helper, same inputs as make_host_board.py and verify_board.py: the builder and the
    # verifier cannot disagree about the frame, including the baudrate from the board profile.
    geometry = geometry_for_wrapper(deployment, contract, wrapper)
    inputs_path = (deployment.parent / contract["verification"]["inputs"]).resolve()
    payload = json.loads(inputs_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = next((payload[key] for key in ("inputs", "samples", "data") if key in payload), None)
    if not isinstance(payload, list) or not payload:
        raise ValueError("contract input file has no samples")
    inputs = sample_rows(payload, "inputs")
    if any(len(row) != geometry.in_elements for row in inputs):
        raise ValueError(f"the derived geometry needs {geometry.in_elements} input elements "
                         "per sample")
    if criterion.samples_source != "code_default" and len(inputs) != criterion.samples:
        raise ValueError("input count differs from the contract's verification.samples")
    if len(inputs) > 65536:
        raise ValueError("sample count exceeds uint16 sequence space")
    python_path = run_dir / "evidence" / "python-output.json"
    hls_path = run_dir / "p3-build" / "csim" / "csim_results.log"
    python_reference = json.loads(python_path.read_text(encoding="utf-8"))
    hls_reference = read_rows(hls_path)
    if len(python_reference) != len(inputs) or len(hls_reference) != len(inputs):
        raise ValueError("reference count differs from host request count")

    output.mkdir(parents=True, exist_ok=True)
    staged = stage_rtl(board, wrapper)
    rtl = board / "rtl"
    sequences = list(range(len(inputs)))
    request_bytes = b"".join(encode_request(row, sequence, geometry)
                             for row, sequence in zip(inputs, sequences))
    request_file = rtl / "request_bytes.hex"
    request_file.write_text("\n".join(f"{byte:02x}" for byte in request_bytes) + "\n",
                            encoding="ascii")
    testbench = rtl / "board_top_tb.v"
    facts = board_facts.facts_for_wrapper(wrapper)
    testbench.write_text(render_testbench(len(inputs), geometry, facts), encoding="ascii")
    hook = board / "host_sim_run.tcl"
    hook.write_text(f'set rtl_dir "{rtl.as_posix()}"\n'
                    'foreach data_file [glob -nocomplain -directory $rtl_dir *.dat] {\n'
                    '    file copy -force $data_file [file tail $data_file]\n}\n'
                    'file copy -force "$rtl_dir/request_bytes.hex" request_bytes.hex\n'
                    'run all\n', encoding="utf-8")
    template = SKILLS / "vivado-manager" / "templates" / "simulate.tcl"
    values = {"THREADS": "4", "PROJECT_NAME": f"{wrapper['top']}_host",
              "PART": wrapper["part"], "TOP": "board_top", "TB": "board_top_tb.v",
              "TB_TOP": "board_top_tb", "RTL_DIR": rtl.as_posix(),
              "XDC": (rtl / "board.xdc").as_posix(),
              "SIM_RUN_TCL": hook.as_posix()}
    script = board / "host_simulate.tcl"
    content = template.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    content = content.replace("$root/sim", "$root/host-sim-project")
    script.write_text(content, encoding="utf-8")
    with Lock(False):
        execution = run_tool(vivado, script, output, timeout=3600)
    frame_files = list((board / "host-sim-project").rglob("response_frames.hex"))
    result = {"status": "FAILED", "stage": "host_sim", "returncode": execution["returncode"],
              "cycle_counter": cycle_counter_record(wrapper, simulation=True),
              "timed_out": execution["timed_out"], "requested_samples": len(inputs),
              "received_samples": 0, "hardware_verified": False,
              "on_board_numerical": "NOT_RUN", "contract_sha256": sha256_file(deployment),
              "model_sha256": wrapper["model_sha256"], "core_verilog_sha256": wrapper["core_verilog_sha256"],
              "staged_rtl": staged["files"], "testbench_sha256": sha256_file(testbench),
              "request_bytes_sha256": sha256_file(request_file),
              "vivado_stdout": str(output / "stdout.txt")}
    if (execution["returncode"] == 0 and not execution["timed_out"]
            and simulation_completed(execution["stdout"], len(inputs))
            and len(frame_files) == 1):
        frames = frame_files[0].read_text(encoding="ascii").splitlines()
        try:
            cycles, outputs = decode_frames(frames, sequences, geometry)
            python_gate = compare("host_sim_vs_python", python_reference, outputs, criterion)
            hls_gate = compare("host_sim_vs_hls", hls_reference, outputs, criterion)
            result.update({"received_samples": len(outputs), "compute_cycles": cycles,
                           "python": python_gate, "hls": hls_gate,
                           "host_geometry": geometry.to_dict(),
                           "status": "PASSED" if python_gate["passed"] and hls_gate["passed"]
                           else "FAILED", "frames_sha256": sha256_file(frame_files[0])})
        except (ValueError, UnicodeError) as exc:
            result["error"] = str(exc)
    else:
        result["error"] = "Vivado host testbench did not complete or did not write one response file"
    (output / "host-sim-evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", type=Path, default=DEFAULT_VIVADO)
    args = parser.parse_args()
    result = simulate(args.deployment, args.board, args.output, args.vivado)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
