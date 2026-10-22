"""A response counter must never be presented as isolated HLS latency or II."""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import simulate_host as sim
import verify_board as verify
from test_attempt_persistence import capture_fixture


@pytest.mark.parametrize("style,completion", [
    ("axis_stream_with_ap_ctrl_hs", "matched_output_beat"),
    ("ap_vld_array_with_ap_ctrl_hs", "core_ap_done_via_adapter"),
])
def test_response_counter_labels_adapter_wait_for_both_interfaces(style, completion):
    wrapper = {"interface": {"style": style}, "clock": {"core_mhz": 80.0}}
    record = sim.cycle_counter_record(wrapper)
    assert record["field"] == "compute_cycles"
    assert record["metric"] == "host_adapter_round_trip"
    assert record["unit"] == "core_clock_cycles"
    assert record["clock_mhz"] == 80.0
    assert record["start_event"] == "adapter_samples_transport_start"
    assert record["stop_event"] == "transport_samples_adapter_done"
    assert record["completion"] == completion
    assert record["includes_adapter_wait"] is True
    assert record["is_kernel_latency"] is False
    assert record["is_initiation_interval"] is False
    assert record["includes_uart_wire_time"] is False


def test_simulation_counter_uses_actual_testbench_clock():
    wrapper = {"interface": {"style": "axis_stream_with_ap_ctrl_hs"},
               "clock": {"core_mhz": 80.0}}
    assert sim.cycle_counter_record(wrapper, simulation=True)["clock_mhz"] == 100.0


@pytest.mark.parametrize("wrapper", [
    {}, {"interface": {"style": "unmeasured"}, "clock": {"core_mhz": 100}},
    {"interface": {"style": "axis_stream_with_ap_ctrl_hs"}, "clock": {"core_mhz": 0}},
    {"interface": {"style": "axis_stream_with_ap_ctrl_hs"}, "clock": {"core_mhz": float("nan")}},
])
def test_counter_definition_refuses_missing_or_invalid_facts(wrapper):
    with pytest.raises(ValueError):
        sim.cycle_counter_record(wrapper)


def test_failed_capture_already_persists_counter_definition(monkeypatch, tmp_path):
    import serial
    deployment, board, bit, scope, programmed, output = capture_fixture(monkeypatch, tmp_path)
    (board / "wrapper.json").write_text(json.dumps({
        "interface": {"style": "axis_stream_with_ap_ctrl_hs"},
        "clock": {"core_mhz": 100.0}}))
    class Port:
        def open(self):
            persisted = json.loads((output / "summary.json").read_text())
            assert persisted["cycle_counter"]["is_kernel_latency"] is False
            raise OSError("fixture has no hardware")
        def close(self):
            pass
    monkeypatch.setattr(serial, "Serial", Port)
    result = verify.capture(deployment, board, bit, scope, programmed,
                            "COM9", "TESTSERIAL", output)
    assert result["status"] == "FAILED"
    assert result["compute_cycles"] == []
    assert result["cycle_counter"]["completion"] == "matched_output_beat"
