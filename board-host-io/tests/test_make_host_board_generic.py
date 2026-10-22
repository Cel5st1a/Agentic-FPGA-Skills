"""The host board generator must pick its adapter from the core's real interface and take
every number from the derived geometry.

The ap_vld branch used to wire the core straight to the transport (D-M01). These tests pin
the replacement, pin the fact that the widths are no longer literals, and pin the four
fail-closed integrity gates that must survive the change.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from geometry_fixtures import facts, synthetic_geometry, wrapper_record

import pytest


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ap_vld_record(elements=64, out_elements=4, element_bits=32, precision="ap_fixed<32,12>"):
    return {
        "top": "myproject",
        "precision": precision,
        "part": "xc7a200tfbg484-2",
        "clock": {"reference_mhz": 200.0, "core_mhz": 100.0,
                  "mmcm_mult": 5.0, "mmcm_divide": 10.0},
        "interface": {
            "style": "ap_vld_array_with_ap_ctrl_hs",
            "input": "ae_input", "valid": "ae_input_ap_vld",
            "input_bits": elements * element_bits, "elements": elements,
            "control": ["ap_done", "ap_idle", "ap_ready", "ap_start"],
            "result_outputs": [f"out_{index}" for index in range(out_elements)],
            "output_bits": {f"out_{index}": element_bits for index in range(out_elements)},
        },
    }


def ap_vld_ports(elements=64, out_elements=4, element_bits=32):
    ports = {
        "ap_clk": {"direction": "input", "bits": 1},
        "ap_start": {"direction": "input", "bits": 1},
        "ap_done": {"direction": "output", "bits": 1},
        "ap_idle": {"direction": "output", "bits": 1},
        "ap_ready": {"direction": "output", "bits": 1},
        "ap_rst": {"direction": "input", "bits": 1},
        "ae_input": {"direction": "input", "bits": elements * element_bits},
        "ae_input_ap_vld": {"direction": "input", "bits": 1},
    }
    for index in range(out_elements):
        ports[f"out_{index}"] = {"direction": "output", "bits": element_bits}
    return ports


def geometry_for(record, out_elements=4):
    interface = record["interface"]
    elements = interface.get("elements") or interface["stream"]["input_elements"]
    return synthetic_geometry(load("host_geometry"), 
        {"precision": record["precision"], "model": {"input_shape": [256, elements]}},
        references=[[0.0] * out_elements], interface=interface)


def test_ap_vld_top_uses_the_adapter_instead_of_wiring_the_core_to_the_transport():
    board = load("make_host_board")
    record = ap_vld_record()
    geometry = geometry_for(record)
    source = board.render_top(record, "parallel", geometry, facts())
    assert "host_avld_adapter" in source
    assert ".ae_input_ap_vld(1'b1)" not in source, "D-M01: ap_vld must not be tied high"
    assert ".ap_start(start)" not in source, "D-M01: ap_start must not come from the transport"
    assert ".ap_start(core_start)" in source
    assert ".ae_input(core_input)" in source
    assert ".ae_input_ap_vld(core_input_vld)" in source
    assert ".ap_idle(ap_idle)" in source


def test_both_modes_parameterise_the_transport_from_the_geometry():
    board = load("make_host_board")
    record = ap_vld_record()
    geometry = geometry_for(record)
    source = board.render_top(record, "parallel", geometry, facts())
    for expected in (".PAYLOAD_BYTES(256)", ".RESPONSE_BYTES(16)",
                     ".INPUT_BITS(2048)", ".OUTPUT_BITS(128)"):
        assert expected in source, f"transport is not parameterised: missing {expected}"


def test_synthetic_geometry_reaches_the_generated_top():
    board = load("make_host_board")
    record = ap_vld_record(element_bits=20, precision="ap_fixed<20,6>")
    geometry = geometry_for(record)
    assert (geometry.payload_bytes, geometry.response_bytes) == (160, 10)
    source = board.render_top(record, "parallel", geometry, facts())
    for expected in (".PAYLOAD_BYTES(160)", ".RESPONSE_BYTES(10)",
                     ".INPUT_BITS(1280)", ".OUTPUT_BITS(80)",
                     ".INPUT_WORDS_BITS(1280)", ".CORE_INPUT_BITS(1280)",
                     ".OUTPUT_WORDS_BITS(80)", ".CORE_OUTPUT_BITS(80)"):
        assert expected in source, f"missing {expected}"
    assert ".out_0(core_output[19:0])" in source
    assert ".out_3(core_output[79:60])" in source
    assert "wire [1279:0] input_words;" in source


def test_stream_mode_keeps_its_adapter_and_gains_geometry_parameters():
    board = load("make_host_board")
    record = {
        "top": "myproject", "precision": "ap_fixed<32,12>",
        "part": "xc7a200tfbg484-2",
        "clock": {"reference_mhz": 200.0, "core_mhz": 100.0,
                  "mmcm_mult": 5.0, "mmcm_divide": 10.0},
        "interface": {
            "style": "axis_stream_with_ap_ctrl_hs",
            "input_port": "waveform_input_TDATA", "input_valid": "waveform_input_TVALID",
            "input_ready": "waveform_input_TREADY", "output_port": "layer10_out_TDATA",
            "output_valid": "layer10_out_TVALID", "output_ready": "layer10_out_TREADY",
            "control": ["ap_done", "ap_idle", "ap_ready", "ap_start"],
            "stream": {"input_elements": 64, "output_elements": 4, "element_bits": 32,
                       "output_element_bits": 32, "tdata_bits": 2048,
                       "output_tdata_bits": 128,
                       "input_beats_per_sample": 1, "output_beats_per_sample": 1,
                       "input_lanes_per_beat": 64, "output_lanes_per_beat": 4,
                       "lane_bits": 32},
            "element_types": {"input": {"total_bits": 32, "integer_bits": 12},
                              "output": {"total_bits": 32, "integer_bits": 12}},
        },
    }
    geometry = geometry_for(record)
    source = board.render_top(record, "stream", geometry, facts())
    assert "host_stream_adapter" in source
    assert ".INPUT_BITS(2048)" in source and ".OUTPUT_BITS(128)" in source
    assert ".ap_rst_n(core_rst_n)" in source
    assert "host_avld_adapter" not in source


def test_input_bus_width_is_derived_not_pinned():
    board = load("make_host_board")
    record = ap_vld_record()
    ports = ap_vld_ports()
    ports["ae_input"] = {"direction": "input", "bits": 4096}
    geometry = geometry_for(record)
    with pytest.raises(ValueError, match="element|ae_input"):
        board.validate_interface(record, ports, geometry)


def test_result_port_width_is_derived_not_pinned():
    board = load("make_host_board")
    record = ap_vld_record()
    ports = ap_vld_ports()
    ports["out_2"] = {"direction": "output", "bits": 16}
    with pytest.raises(ValueError, match="out_2"):
        board.validate_interface(record, ports, geometry_for(record))


def test_a_core_without_ap_idle_is_refused_for_the_ap_vld_path():
    """The adapter waits for ap_idle; a core that cannot report it must fail closed."""
    board = load("make_host_board")
    record = ap_vld_record()
    ports = ap_vld_ports()
    del ports["ap_idle"]
    record["interface"]["control"] = ["ap_done", "ap_ready", "ap_start"]
    with pytest.raises(ValueError, match="ap_idle"):
        board.validate_interface(record, ports, geometry_for(record))


def test_a_result_grid_that_is_not_byte_aligned_pads_the_bus_not_the_element_stride():
    """3 x 20-bit results = 60 bits, but the response carries ceil(60/8) = 8 bytes.

    The transport extracts response bytes from its output bus, so that bus must be the
    byte-padded width (64), not the padding-free core width (60); a 60-bit bus would make
    the last byte's part-select index bits that do not exist.
    """
    board = load("make_host_board")
    record = ap_vld_record(elements=3, out_elements=3, element_bits=20,
                           precision="ap_fixed<20,6>")
    geometry = geometry_for(record, out_elements=3)
    assert (geometry.in_bits, geometry.out_bits) == (60, 60)
    assert (geometry.payload_bytes, geometry.response_bytes) == (8, 8)
    assert (geometry.in_words_bits, geometry.out_words_bits) == (64, 64)
    source = board.render_top(record, "parallel", geometry, facts())
    assert ".INPUT_BITS(64)" in source and ".OUTPUT_BITS(64)" in source
    assert ".CORE_INPUT_BITS(60)" in source and ".CORE_OUTPUT_BITS(60)" in source
    assert "wire [63:0] input_words;" in source and "wire [63:0] output_words;" in source
    assert ".out_0(core_output[19:0])" in source
    assert ".out_2(core_output[59:40])" in source


def test_an_axi_stream_grid_that_is_not_byte_aligned_is_refused():
    """AXI-Stream TDATA is a whole number of bytes, so such a grid cannot be real."""
    module = load("host_geometry")
    interface = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "stream": {"input_elements": 3, "output_elements": 3, "element_bits": 20,
                   "output_element_bits": 20, "tdata_bits": 60, "output_tdata_bits": 60,
                   "input_beats_per_sample": 1, "output_beats_per_sample": 1,
                   "input_lanes_per_beat": 3, "output_lanes_per_beat": 3, "lane_bits": 20},
        "element_types": {"input": {"total_bits": 20, "integer_bits": 6},
                          "output": {"total_bits": 20, "integer_bits": 6}},
    }
    with pytest.raises(ValueError, match="byte"):
        synthetic_geometry(module, {"precision": "ap_fixed<20,6>",
                                       "model": {"input_shape": [4, 3]}},
                                      references=[[0.0] * 3], interface=interface)


# The integrity gates used to be "tested" here by grepping their message strings. An
# independent review mutation-tested that: neutering all ten fail-closed conditions while
# keeping the messages left this test green. It was removed rather than left in place,
# because a test that cannot fail is worse than no test 鈥?it reports coverage that does not
# exist. The gates are now driven behaviourally in `tests/test_failclosed_gates.py`, where
# each one is required to refuse a mutated input, with a positive control so a refusal
# cannot pass for an unrelated reason.


def test_every_staged_asset_is_hash_pinned_including_the_new_adapter():
    board = load("make_host_board")
    import hashlib
    for name in ("transport.v", "uart_rx.v", "uart_tx.v", "host_stream_adapter.v",
                 "host_avld_adapter.v"):
        assert name in board.SOURCE_HASHES, f"{name} is not hash-pinned"
        digest = hashlib.sha256((SKILL / "assets" / name).read_bytes()).hexdigest()
        assert board.SOURCE_HASHES[name] == digest, f"{name} pin is stale"


def test_the_frozen_transport_is_not_staged_into_a_build():
    board = load("make_host_board")
    assert all("frozen" not in name for name in board.SOURCE_HASHES)
