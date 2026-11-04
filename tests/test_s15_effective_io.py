"""Actual emitted I/O types, not a requested common precision, determine wire scaling."""

import hashlib
import json
from pathlib import Path

import pytest
from test_s15_numeric_policy import ROOT, load


def firmware(tmp_path, stream=False):
    project = tmp_path / "project"
    fw = project / "firmware"
    fw.mkdir(parents=True)
    (fw / "defines.h").write_text(
        "typedef ap_fixed<20,6> input_t;\ntypedef ap_fixed<24,5> result_t;\n"
    )
    header = "void myproject(input_t x[5],result_t y[3]);\n"
    if stream:
        (fw / "defines.h").write_text(
            (fw / "defines.h").read_text() + "typedef nnet::array<ap_fixed<20,6>,5> input_pack;\n"
            "typedef nnet::array<ap_fixed<24,5>,3> result_pack;\n"
        )
        header = "void myproject(hls::stream<input_pack> &x,hls::stream<result_pack> &y);\n"
    (fw / "myproject.h").write_text(header)
    in_type, out_type = ("input_pack", "result_pack") if stream else ("input_t", "result_t")
    (project / "myproject_bridge.cpp").write_text(
        f"nnet::convert_data<float,{in_type},5>(x,x_ap);\n"
        f"nnet::convert_data<{out_type},float,3>(y_ap,y);\n"
    )
    rtl = project / "myproject_prj/solution1/syn/verilog"
    rtl.mkdir(parents=True)
    (rtl / "myproject.v").write_text("// synthetic interface fixture; no model or board")
    return rtl


def test_parallel_types_are_read_from_generated_header_and_bridge(tmp_path):
    helper = load("scripts/line/generated_geometry.py", "effective_parallel_facts")
    facts = helper.parallel_facts(firmware(tmp_path), "myproject")
    assert facts["input"]["elements"] == 5 and facts["output"]["elements"] == 3
    assert facts["input"]["grid"]["total_bits"] == 20
    assert facts["output"]["grid"]["integer_bits"] == 5


def test_host_allows_distinct_proven_output_grid_but_refuses_changed_source(tmp_path):
    helper = load("scripts/line/generated_geometry.py", "proven_parallel_facts")
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    rtl = firmware(tmp_path)
    facts = helper.parallel_facts(rtl, "myproject")
    wrapper = {
        "top": "myproject",
        "core_verilog": str(rtl / "myproject.v"),
        "geometry_files": facts["files"],
        "input_layout": {"flatten": "c_order", "elements": 5},
        "output_layout": {"flatten": "c_order", "elements": 3},
        "rom_image": {"grid": facts["input"]["grid"]},
        "output_grid": facts["output"]["grid"],
        "interface": {
            "style": "ap_vld_array_with_ap_ctrl_hs",
            "input_bits": 100,
            "elements": 5,
            "result_outputs": ["y_0", "y_1", "y_2"],
            "output_bits": {"y_0": 24, "y_1": 24, "y_2": 24},
        },
    }
    result = geo.geometry_from_contract({"precision": "ap_fixed<20,6>"}, wrapper=wrapper)
    assert (result.in_element_bits, result.out_element_bits, result.out_integer_bits) == (20, 24, 5)
    path = Path(next(iter(facts["files"])))
    path.write_bytes(path.read_bytes() + b"\n// changed")
    assert hashlib.sha256(path.read_bytes()).hexdigest() != facts["files"][str(path)]
    with pytest.raises(ValueError, match="geometry.*(changed|hash)"):
        geo.geometry_from_contract({"precision": "ap_fixed<20,6>"}, wrapper=wrapper)


def test_stream_widths_and_whole_beat_padding_are_independent(tmp_path):
    producer = load("vivado-manager/scripts/make_board_wrapper.py", "effective_stream_producer")
    # Current real Vitis HLS pads 20/24-bit fields to separate 32-bit slots.
    facts = producer.axis_lane_geometry(
        firmware(tmp_path, True), "myproject", 160, 96, None, None, 20
    )
    assert facts["input_beat_bits"] == 100 and facts["output_beat_bits"] == 72
    assert facts["input_beats_per_sample"] == facts["output_beats_per_sample"] == 1
    # An independently packed output port must not force the input to that width.
    separate = producer.axis_lane_geometry(
        tmp_path / "project/myproject_prj/solution1/syn/verilog",
        "myproject",
        160,
        72,
        None,
        None,
        20,
    )
    assert separate["lane_bits"] == 32 and separate["output_lane_bits"] == 24


def test_real_rtl_repackages_padded_slots_with_stalls_gaps_and_two_requests(tmp_path):
    runner = load("board-host-io/scripts/run_rtl_tb.py", "padded_slot_runner")
    result = runner.run_testbench(
        "host_stream_padding_tb",
        [
            ROOT / "board-host-io/assets/host_stream_adapter.v",
            ROOT / "board-host-io/tests/host_stream_padding_tb.v",
        ],
        tmp_path,
    )
    (tmp_path / "simulation-result.json").write_text(json.dumps(result, indent=2))
    assert result["returncode"] == 0, result["stdout"]


def test_host_sim_hls_reference_is_diagnostic_not_an_extra_numeric_gate():
    simulator = load("board-host-io/scripts/simulate_host.py", "optional_sim_hls")
    verify = load("board-host-io/scripts/verify_board.py", "sim_original_criterion")
    criterion = verify.original_model_criterion()
    reference = [[0.0]] * 256
    for hls in (None, [[1.0]] * 256):
        result = simulator.evaluate_simulation(reference, hls, reference, criterion)
        assert result["status"] == "PASSED" and result["python"]["passed"]
        assert result["hls"]["passed"] in (None, False)
    failed = simulator.evaluate_simulation(reference, None, [[1.0]] * 256, criterion)
    assert failed["status"] == "FAILED"


def test_development_simulation_uses_original_model_numerical_rule():
    simulator = load("board-host-io/scripts/simulate_host.py", "sim_standard")
    criterion = simulator.simulation_criterion()
    assert criterion.atol == criterion.rtol == 0.001
    assert criterion.samples == 256
