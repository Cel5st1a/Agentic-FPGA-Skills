"""The geometry derivation is the single source of truth for the host UART framing.

Every length, width and scale the host path uses must be reproducible from the contract
plus the generated core's own interface record. These tests pin the verified 64x4
geometry against the constants it replaces, then change the grid and the frame length
together with a synthetic geometry so a pinned implementation cannot pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from geometry_fixtures import synthetic_geometry, wrapper_record

import pytest


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "host_geometry.py"


def load():
    """Load the module the way an import would.

    Registering it in `sys.modules` is not optional: dataclasses resolves string
    annotations through `sys.modules[cls.__module__]`, so an unregistered module makes
    `@dataclass` raise `AttributeError: 'NoneType' object has no attribute '__dict__'`.
    """
    spec = importlib.util.spec_from_file_location("host_geometry", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["host_geometry"] = module
    spec.loader.exec_module(module)
    return module


def contract(precision="ap_fixed<32,12>", elements=64):
    return {"precision": precision, "model": {"path": "encoder.keras",
                                              "input_shape": [256, elements]},
            "clock_period_ns": 10.0}


def references(width=4, rows=3):
    return [[0.0] * width for _ in range(rows)]


def test_verified_geometry_reproduces_every_pinned_constant():
    geometry = synthetic_geometry(load(), contract(), references=references())
    assert (geometry.total_bits, geometry.integer_bits) == (32, 12)
    assert (geometry.in_elements, geometry.out_elements) == (64, 4)
    assert geometry.in_bits == 2048 and geometry.out_bits == 128
    assert geometry.payload_bytes == 256 and geometry.response_bytes == 16
    assert geometry.request_bytes == 264 and geometry.response_frame_bytes == 28
    assert geometry.in_words_bits == 2048 and geometry.out_words_bits == 128
    assert geometry.scale_in == 1 << 20 and geometry.scale_out == 1 << 20
    assert geometry.request_length_field == 256 and geometry.response_length_field == 20
    assert geometry.baudrate == 115200


def test_synthetic_20_bit_grid_changes_the_grid_and_the_frame_length_together():
    geometry = synthetic_geometry(load(), contract("ap_fixed<20,6>"), references=references())
    assert geometry.in_bits == 1280 and geometry.payload_bytes == 160
    assert geometry.request_bytes == 168
    assert geometry.out_bits == 80 and geometry.response_bytes == 10
    assert geometry.response_frame_bytes == 22 and geometry.response_length_field == 14
    assert geometry.scale_in == 1 << 14
    assert geometry.in_words_bits == 1280 and geometry.out_words_bits == 80


def test_ceil_remainder_pads_the_bus_but_not_the_element_stride():
    geometry = synthetic_geometry(load(), contract("ap_fixed<20,6>", elements=3),
                                             references=references(width=1))
    assert geometry.in_bits == 60 and geometry.payload_bytes == 8
    assert geometry.in_words_bits == 64
    assert geometry.out_bits == 20 and geometry.response_bytes == 3
    assert geometry.out_words_bits == 24


def test_single_element_and_single_output_are_allowed():
    geometry = synthetic_geometry(load(), contract("ap_fixed<8,4>", elements=1),
                                             references=references(width=1))
    assert geometry.in_bits == 8 and geometry.payload_bytes == 1
    assert geometry.request_bytes == 9
    assert geometry.out_words_bits == 8 and geometry.response_frame_bytes == 13
    assert geometry.scale_in == 1 << 4


@pytest.mark.parametrize("text", ["ap_fixed<32>", "ap_fixed<8,12>", "", "ap_fixed<0,0>",
                                  "32,12", "ap_ufixed<32,12>", "ap_fixed<32,12,4>"])
def test_malformed_precision_is_refused(text):
    with pytest.raises(ValueError):
        load().parse_precision(text)


def test_missing_or_absurd_contract_fields_are_refused():
    module = load()
    with pytest.raises(ValueError, match="precision"):
        synthetic_geometry(module, {"model": {"input_shape": [256, 64]}},
                                      references=references())
    with pytest.raises(ValueError, match="input_layout.elements"):
        synthetic_geometry(module, {"precision": "ap_fixed<32,12>", "model": {}},
                                      references=references())
    with pytest.raises(ValueError, match="element"):
        synthetic_geometry(module, contract(elements=0), references=references())
    with pytest.raises(ValueError, match="reference"):
        synthetic_geometry(module, contract(), references=[])
    with pytest.raises(ValueError, match="ragged"):
        synthetic_geometry(module, contract(), references=[[0.0, 0.0], [0.0]])


def test_hardware_interface_disagreeing_with_the_contract_is_refused():
    module = load()
    interface = {"style": "ap_vld_array_with_ap_ctrl_hs", "input_bits": 4096, "elements": 128,
                 "result_outputs": ["a", "b", "c", "d"]}
    with pytest.raises(ValueError, match="interface.input_bits"):
        synthetic_geometry(module, contract(), references=references(), interface=interface)


def test_reference_row_width_disagreeing_with_the_hardware_is_refused():
    module = load()
    interface = {"style": "ap_vld_array_with_ap_ctrl_hs", "input_bits": 2048, "elements": 64,
                 "result_outputs": ["a", "b", "c", "d"]}
    with pytest.raises(ValueError, match="result"):
        synthetic_geometry(module, contract(), references=references(width=3),
                                      interface=interface)


def test_axis_interface_takes_its_grid_from_the_generated_element_types():
    module = load()
    interface = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "stream": {"input_elements": 64, "output_elements": 4, "element_bits": 32,
                   "output_element_bits": 32, "tdata_bits": 2048, "output_tdata_bits": 128,
                   "input_beats_per_sample": 1, "output_beats_per_sample": 1,
                   "input_lanes_per_beat": 64, "output_lanes_per_beat": 4, "lane_bits": 32},
        "element_types": {"input": {"total_bits": 32, "integer_bits": 12},
                          "output": {"total_bits": 32, "integer_bits": 12}},
    }
    geometry = synthetic_geometry(module, contract(), references=references(),
                                             interface=interface)
    assert (geometry.in_element_bits, geometry.out_element_bits) == (32, 32)
    assert geometry.request_bytes == 264 and geometry.response_frame_bytes == 28


def test_axis_element_types_that_differ_from_the_contract_precision_are_refused():
    """Restores the pre-change gate: the host path may not silently run on a different grid
    than the contract declares, because the numerical criterion's LSB term comes from the
    contract while the wire scale would come from the core."""
    module = load()
    interface = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "stream": {"input_elements": 64, "output_elements": 4, "element_bits": 20,
                   "output_element_bits": 20, "tdata_bits": 1280, "output_tdata_bits": 80,
                   "input_beats_per_sample": 1, "output_beats_per_sample": 1,
                   "input_lanes_per_beat": 64, "output_lanes_per_beat": 4, "lane_bits": 20},
        "element_types": {"input": {"total_bits": 20, "integer_bits": 6},
                          "output": {"total_bits": 20, "integer_bits": 6}},
    }
    with pytest.raises(ValueError, match="precision"):
        synthetic_geometry(module, contract(), references=references(),
                                      interface=interface)
    matching = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "stream": {"input_elements": 64, "output_elements": 4, "element_bits": 20,
                   "output_element_bits": 20, "tdata_bits": 1280, "output_tdata_bits": 80,
                   "input_beats_per_sample": 1, "output_beats_per_sample": 1,
                   "input_lanes_per_beat": 64, "output_lanes_per_beat": 4, "lane_bits": 20},
        "element_types": {"input": {"total_bits": 20, "integer_bits": 6},
                          "output": {"total_bits": 20, "integer_bits": 6}},
    }
    geometry = synthetic_geometry(module, contract("ap_fixed<20,6>"),
                                             references=references(), interface=matching)
    assert geometry.payload_bytes == 160 and geometry.response_bytes == 10


def test_axis_records_that_would_need_several_beats_per_sample_are_refused():
    """Multi-beat input is supported only with consistent port/beat/lane facts."""
    module = load()
    base = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "element_types": {"input": {"total_bits": 32, "integer_bits": 12},
                          "output": {"total_bits": 32, "integer_bits": 12}},
    }
    for broken in (
        {"input_elements": 64, "output_elements": 4, "element_bits": 32,
         "output_element_bits": 32, "tdata_bits": 2048, "output_tdata_bits": 128,
         "input_beats_per_sample": 2, "output_beats_per_sample": 1,
         "input_lanes_per_beat": 32, "output_lanes_per_beat": 4, "lane_bits": 32},
        {"input_elements": 64, "output_elements": 4, "element_bits": 32,
         "output_element_bits": 32, "tdata_bits": 2048, "output_tdata_bits": 128,
         "input_beats_per_sample": 1, "output_beats_per_sample": 1,
         "input_lanes_per_beat": 32, "output_lanes_per_beat": 4, "lane_bits": 32},
        {"input_elements": 64, "output_elements": 4, "element_bits": 32,
         "output_element_bits": 32, "tdata_bits": 2048, "output_tdata_bits": 128,
         "input_beats_per_sample": 1, "output_beats_per_sample": 1,
         "input_lanes_per_beat": 64, "output_lanes_per_beat": 4, "lane_bits": 16},
    ):
        with pytest.raises(ValueError, match="beat|lane|tdata_bits"):
            synthetic_geometry(module, contract(), references=references(),
                                          interface=dict(base, stream=broken))


def test_payload_length_beyond_the_uint16_length_field_is_refused():
    module = load()
    with pytest.raises(ValueError, match="65535"):
        synthetic_geometry(module, contract(elements=70000), references=references())


def test_baudrate_source_is_recorded_not_assumed():
    module = load()
    declared = dict(contract(), host_transport={"channel": "uart", "baudrate": 921600})
    assert synthetic_geometry(module, declared, references=references()).baudrate == 921600
    from_contract = synthetic_geometry(module, declared, references=references())
    assert from_contract.baudrate_source == "contract.host_transport.baudrate"
    profile = {"peripherals": {"uart": {"baudrate": 230400}}}
    from_profile = synthetic_geometry(module, contract(), references=references(),
                                                 board_profile=profile)
    assert (from_profile.baudrate, from_profile.baudrate_source) == (
        230400, "board_profile.peripherals.uart.baudrate")
    fallback = synthetic_geometry(module, contract(), references=references())
    assert (fallback.baudrate, fallback.baudrate_source) == (115200, "verified_default")


def test_precision_parser_agrees_with_the_frozen_criterion_parser():
    sys.path.insert(0, str(SKILL.parent / "scripts"))
    from line.contract import effective_lsb

    module = load()
    for text in ("ap_fixed<32,12>", "ap_fixed<20,6>", "ap_fixed<8,4>", "ap_fixed<24,6>"):
        total, integer = module.parse_precision(text)
        assert 2.0 ** -(total - integer) == effective_lsb(text)


def test_quantisation_matches_the_rom_packers_floor_and_clamp_rule():
    module = load()
    assert module.quantise(1.0, 32, 12) == 1 << 20
    assert module.quantise(1.0 / 3, 32, 12) == 349525
    assert module.quantise(-1.0 / 3, 32, 12) == -349526
    assert module.quantise(2.0 ** 40, 32, 12) == 2 ** 31 - 1
    assert module.quantise(-(2.0 ** 40), 32, 12) == -(2 ** 31)
    assert module.quantise(0.0, 20, 6) == 0
