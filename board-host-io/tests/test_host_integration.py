"""Regression gates for a host frame traversing the generated board and real core."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from geometry_fixtures import facts, synthetic_geometry, wrapper_record

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "simulate_host.py"


def load_script():
    spec = importlib.util.spec_from_file_location("simulate_host", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulate_host"] = module
    spec.loader.exec_module(module)
    return module


def geometry(precision="ap_fixed<32,12>", elements=64, width=4):
    spec = importlib.util.spec_from_file_location(
        "host_geometry", SCRIPT.with_name("host_geometry.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["host_geometry"] = module
    spec.loader.exec_module(module)
    return synthetic_geometry(module, 
        {"precision": precision, "model": {"input_shape": [256, elements]}},
        references=[[0.0] * width])


def test_host_sim_testbench_drives_bytes_and_checks_complete_responses():
    sim = load_script()
    source = sim.render_testbench(sample_count=2, geometry=geometry(), facts=facts())
    assert "board_top dut" in source
    assert "request_bytes" in source
    assert "force dut.rx_data" in source
    assert "force dut.rx_valid" in source
    assert "force dut.tx_ready" in source
    assert "response_frames.hex" in source
    assert "localparam integer REQUEST_BYTES = 264;" in source
    assert "localparam integer RESPONSE_BYTES = 28;" in source
    assert "$fatal" in source
    # The AX7A200B's top is driven from a differential pair; the tie-off must match the
    # ports this board's top actually declares.
    assert "board_top dut(.sys_clk_p(1'b0), .sys_clk_n(1'b1), .rst_n(rst_n)" in source


def test_host_sim_testbench_follows_the_single_ended_board():
    """The Nexys Video's top has one clock port, and the testbench must tie off that."""
    sim = load_script()
    source = sim.render_testbench(sample_count=2, geometry=geometry(), facts=facts("single_ended"))
    assert "board_top dut(.sys_clk(1'b0), .rst_n(rst_n)" in source
    assert "sys_clk_n" not in source


def test_host_sim_testbench_follows_a_synthetic_geometry():
    sim = load_script()
    source = sim.render_testbench(sample_count=2, geometry=geometry("ap_fixed<20,6>"),
                                  facts=facts())
    assert "localparam integer REQUEST_BYTES = 168;" in source
    assert "localparam integer RESPONSE_BYTES = 22;" in source
    assert "264" not in source and " 28;" not in source


def test_stream_board_core_reset_is_adapter_controlled():
    board_path = SCRIPT.with_name("make_host_board.py")
    spec = importlib.util.spec_from_file_location("make_host_board", board_path)
    board = importlib.util.module_from_spec(spec)
    sys.modules["make_host_board"] = board
    spec.loader.exec_module(board)
    record = {"interface": {"style": "axis_stream_with_ap_ctrl_hs",
                            "input_port": "in_TDATA", "input_valid": "in_TVALID",
                            "input_ready": "in_TREADY", "output_port": "out_TDATA",
                            "output_valid": "out_TVALID", "output_ready": "out_TREADY",
                            "stream": {"input_elements": 64, "output_elements": 4,
                                       "element_bits": 32, "output_element_bits": 32,
                                       "tdata_bits": 2048, "output_tdata_bits": 128,
                                       "input_beats_per_sample": 1,
                                       "output_beats_per_sample": 1,
                                       "input_lanes_per_beat": 64,
                                       "output_lanes_per_beat": 4, "lane_bits": 32},
                            "element_types": {"input": {"total_bits": 32, "integer_bits": 12},
                                              "output": {"total_bits": 32, "integer_bits": 12}},
                            "control": []},
              "clock": {"reference_mhz": 200.0, "core_mhz": 100.0,
                        "mmcm_mult": 5.0, "mmcm_divide": 10.0},
              "top": "some_generated_core"}
    source = board.render_top(record, "stream", geometry(), facts())
    assert ".ap_rst_n(core_rst_n)" in source
    assert ".core_rst_n(core_rst_n)" in source


def test_host_sim_refuses_partial_or_extra_frames():
    sim = load_script()
    verified = geometry()
    with pytest.raises(ValueError, match="response count"):
        sim.decode_frames(["00" * 28], [7, 8], verified)
    with pytest.raises(ValueError, match="response.*length"):
        sim.decode_frames(["00" * 27], [7], verified)


def test_host_sim_requires_testbench_completion_marker():
    sim = load_script()
    assert sim.simulation_completed("HOST_INTEGRATION_PASS samples=256\n", 256)
    assert not sim.simulation_completed("Fatal: first frame missing\n", 256)
    assert not sim.simulation_completed("HOST_INTEGRATION_PASS samples=2\n", 256)


def test_host_build_requires_passed_simulation_for_same_contract_and_core(tmp_path: Path):
    build_path = SCRIPT.with_name("build_host.py")
    spec = importlib.util.spec_from_file_location("build_host", build_path)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    record = {"status": "PASSED", "contract_sha256": "contract",
              "core_verilog_sha256": "core", "model_sha256": "model",
              "requested_samples": 256, "received_samples": 256,
              "staged_rtl": {"board_top.v": "rtl"},
              "python": {"passed": True}, "hls": {"passed": True}}
    evidence = tmp_path / "host-sim-evidence.json"
    with pytest.raises(ValueError, match="host simulation"):
        build.require_host_sim(evidence, "contract", "core", "model", {"board_top.v": "rtl"})
    evidence.write_text(__import__("json").dumps(record), encoding="utf-8")
    build.require_host_sim(evidence, "contract", "core", "model", {"board_top.v": "rtl"})
    with pytest.raises(ValueError, match="host simulation"):
        build.require_host_sim(evidence, "contract", "core", "model", {"board_top.v": "changed"})
    record["received_samples"] = 255
    evidence.write_text(__import__("json").dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="host simulation"):
        build.require_host_sim(evidence, "contract", "core", "model", {"board_top.v": "rtl"})
