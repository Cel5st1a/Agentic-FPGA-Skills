"""Every RTL testbench in this skill must actually be executed, not merely stored.

`host_stream_adapter_tb.v` shipped with the stream adapter but no runner ever invoked it,
so it had never run. This module gives the skill one way to compile and run a Verilog
testbench with xsim, and the tests below are the gate that keeps the testbenches honest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from geometry_fixtures import synthetic_geometry, wrapper_record

import pytest


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "run_rtl_tb.py"


def load():
    spec = importlib.util.spec_from_file_location("run_rtl_tb", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_rtl_tb"] = module
    spec.loader.exec_module(module)
    return module


def test_existing_stream_adapter_testbench_actually_runs(tmp_path):
    result = load().run_testbench(
        top="host_stream_adapter_tb",
        sources=[SKILL / "assets" / "host_stream_adapter.v",
                 SKILL / "tests" / "host_stream_adapter_tb.v"],
        workdir=tmp_path)
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "HOST_STREAM_ADAPTER_PASS"


def test_stream_adapter_is_width_generic_at_a_synthetic_geometry(tmp_path):
    """2048/128 must only be defaults: the adapter has to work at another grid."""
    result = load().run_testbench(
        top="host_stream_adapter_2006_tb",
        sources=[SKILL / "assets" / "host_stream_adapter.v",
                 SKILL / "tests" / "host_stream_adapter_2006_tb.v"],
        workdir=tmp_path)
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "HOST_STREAM_ADAPTER_WIDE_PASS"


def test_transport_frames_a_synthetic_geometry_round_trip(tmp_path):
    """The equivalence run only covers 64x4; the transport must also *work* elsewhere.

    A 160-byte payload / 10-byte response / 1280- and 80-bit bus is a different grid and a
    different frame length. The testbench emits the response bytes it produced, and the
    host-side decoder has to accept them -- so the RTL framing and the Python protocol are
    checked against each other at a geometry neither was written for.
    """
    sys.path.insert(0, str(SKILL / "scripts"))
    geometry_module = importlib.util.spec_from_file_location(
        "host_geometry", SKILL / "scripts" / "host_geometry.py")
    geo = importlib.util.module_from_spec(geometry_module)
    sys.modules["host_geometry"] = geo
    geometry_module.loader.exec_module(geo)
    protocol_module = importlib.util.spec_from_file_location(
        "protocol", SKILL / "scripts" / "protocol.py")
    protocol = importlib.util.module_from_spec(protocol_module)
    sys.modules["protocol"] = protocol
    protocol_module.loader.exec_module(protocol)

    grid = synthetic_geometry(geo, 
        {"precision": "ap_fixed<20,6>", "model": {"input_shape": [4, 64]}},
        references=[[0.0] * 4])
    assert (grid.payload_bytes, grid.response_bytes) == (160, 10)
    assert (grid.request_bytes, grid.response_frame_bytes) == (168, 22)

    values = [((index * 13) % 47 - 23) / 4.0 for index in range(64)]
    request = protocol.encode_request(values, 5, grid)
    assert len(request) == 168
    expected_input = protocol.pack_payload(protocol.fixed_words(values, grid), grid)

    (tmp_path / "stimulus_bytes.hex").write_text(
        "\n".join(f"{byte:02x}" for byte in request) + "\n", encoding="ascii")
    gaps = ["4"] + ["2"] * (len(request) - 1)
    (tmp_path / "stimulus_gaps.hex").write_text("\n".join(gaps) + "\n", encoding="ascii")
    # `$readmemh` reads the string as one big-endian number, so the byte order is reversed
    # here to land payload byte 0 in the register's low byte, which is what the transport
    # assembles (`input_words[payload_index*8 +: 8]`).
    (tmp_path / "expected_input.hex").write_text(expected_input[::-1].hex() + "\n",
                                                 encoding="ascii")

    result = load().run_testbench(
        top="transport_geometry_tb",
        sources=[SKILL / "assets" / "transport.v", SKILL / "tests" / "transport_geometry_tb.v"],
        data_files=[tmp_path / "stimulus_bytes.hex", tmp_path / "stimulus_gaps.hex",
                    tmp_path / "expected_input.hex"],
        workdir=tmp_path)
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "TRANSPORT_GEOMETRY_PASS"

    frames = [line.strip() for line in (tmp_path / "response_frames.hex").read_text().splitlines()
              if line.strip()]
    assert len(frames) == 1, f"expected exactly one response frame, got {len(frames)}"
    frame = bytes.fromhex(frames[0])
    assert len(frame) == grid.response_frame_bytes == 22
    cycles, outputs = protocol.decode_response(frame, 5, grid)
    assert cycles > 0
    lo = int.from_bytes(expected_input[:10], "little")
    mask = (1 << 20) - 1
    inverted = lo ^ ((1 << 80) - 1)          # the testbench XORs with 80 bits of ones
    expected_values = [geo.unpack_field((inverted >> (20 * index)) & mask, 20, 6)
                       for index in range(4)]
    assert outputs == expected_values


def test_a_failing_testbench_is_reported_as_a_failure(tmp_path):
    """A runner that cannot fail is not a gate.

    Measured: `xsim` exits 0 even after `$fatal`, so the tool's exit code alone would let
    a failing testbench through. The effective status must come from the output.
    """
    tb = tmp_path / "always_fails_tb.v"
    tb.write_text(
        "`timescale 1ns/1ps\n"
        "module always_fails_tb;\n"
        "  initial begin $fatal(1, \"deliberate\"); end\n"
        "endmodule\n", encoding="ascii")
    result = load().run_testbench(top="always_fails_tb", sources=[tb], workdir=tmp_path)
    assert result["tool_returncode"] == 0, "xsim really does exit zero here"
    assert result["returncode"] != 0
    assert result["marker"] is None


def test_a_missing_marker_is_not_treated_as_success(tmp_path):
    tb = tmp_path / "no_marker_tb.v"
    tb.write_text(
        "`timescale 1ns/1ps\n"
        "module no_marker_tb;\n"
        "  initial begin $display(\"nothing to see\"); $finish; end\n"
        "endmodule\n", encoding="ascii")
    result = load().run_testbench(top="no_marker_tb", sources=[tb], workdir=tmp_path)
    assert result["tool_returncode"] == 0
    assert result["returncode"] != 0, "a clean exit with no PASS marker is not a pass"
    assert result["marker"] is None


def test_the_work_directory_is_used_and_left_clean_of_sources(tmp_path):
    """xsim must not litter the skill tree; it runs in the caller's workdir."""
    load().run_testbench(top="host_stream_adapter_tb",
                         sources=[SKILL / "assets" / "host_stream_adapter.v",
                                  SKILL / "tests" / "host_stream_adapter_tb.v"],
                         workdir=tmp_path)
    assert (tmp_path / "xsim.dir").is_dir()
    assert not list((SKILL / "tests").glob("xsim.dir"))


def test_a_missing_source_is_refused_before_starting_a_tool(tmp_path):
    with pytest.raises(FileNotFoundError):
        load().run_testbench(top="nope_tb", sources=[tmp_path / "does_not_exist.v"],
                             workdir=tmp_path)
