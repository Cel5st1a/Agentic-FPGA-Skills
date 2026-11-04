"""Development regressions for multiple output beats; no board operation."""

import hashlib
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_s15_numeric_policy import ROOT, load

sys.path.insert(0, str(ROOT / "board-host-io/tests"))
from geometry_fixtures import facts, wrapper_record  # noqa: E402


def test_host_build_uses_empty_distinct_stage_work_directories(tmp_path, monkeypatch):
    build = load("board-host-io/scripts/build_host.py", "host_build_work")
    run = tmp_path / "run"
    deployment = run / "contract/model/deployment.yaml"
    deployment.parent.mkdir(parents=True)
    deployment.write_text("development command wiring test")
    board = run / "p3-build/host-board"
    board.mkdir(parents=True)
    wrapper = {"skill": "board-host-io", "host_input_output_available": True}
    for key in ("source_wrapper", "core_verilog", "model_path"):
        source = run / (key + ".txt")
        source.write_text(key)
        wrapper[key] = str(source)
        hash_key = "model_sha256" if key == "model_path" else key + "_sha256"
        wrapper[hash_key] = hashlib.sha256(source.read_bytes()).hexdigest()
    wrapper["contract_sha256"] = hashlib.sha256(deployment.read_bytes()).hexdigest()
    (board / "wrapper.json").write_text(json.dumps(wrapper))
    monkeypatch.setattr(build, "stage_rtl", lambda *args: {"files": {}})
    monkeypatch.setattr(build, "Lock", lambda *args: nullcontext())
    work_paths = []

    def fake_stage(command, cwd):
        from pathlib import Path

        work = Path(command[command.index("--work") + 1])
        assert work != board
        assert not work.exists() or not any(work.iterdir())
        assert work not in work_paths
        work_paths.append(work)
        evidence = Path(command[command.index("--evidence") + 1])
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"status": "PASSED"}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", fake_stage)
    results = build.build(deployment, board, sys.executable)
    assert len(results) == 3
    assert all(r["host_sim_diagnostic"]["status"] == "NOT_RUN" for r in results)
    assert len(work_paths) == 3


def stream_record():
    grid = {"total_bits": 16, "integer_bits": 6}
    interface = {
        "style": "axis_stream_with_ap_ctrl_hs",
        "input_port": "x_TDATA",
        "input_valid": "x_TVALID",
        "input_ready": "x_TREADY",
        "output_port": "y_TDATA",
        "output_valid": "y_TVALID",
        "output_ready": "y_TREADY",
        "element_types": {"input": grid, "output": grid},
        "control": ["ap_start", "ap_done"],
        "stream": {
            "input_elements": 3,
            "output_elements": 4,
            "input_lanes_per_beat": 1,
            "output_lanes_per_beat": 2,
            "input_beats_per_sample": 3,
            "output_beats_per_sample": 2,
            "lane_bits": 16,
            "element_bits": 16,
            "output_element_bits": 16,
            "tdata_bits": 16,
            "output_tdata_bits": 32,
        },
    }
    record = wrapper_record(3, 4, "ap_fixed<16,6>", interface)
    record.update(
        {
            "top": "toy",
            "clock": {"core_mhz": 100, "reference_mhz": 200, "mmcm_mult": 5, "mmcm_divide": 10},
        }
    )
    return record


def test_geometry_and_generator_keep_port_width_separate_from_sample_width():
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    board = load("board-host-io/scripts/make_host_board.py", "multiout_board")
    record = stream_record()
    geometry = geo.geometry_from_contract({"precision": "ap_fixed<16,6>"}, wrapper=record)
    assert geometry.out_bits == 64
    ports = {
        name: {"direction": direction, "bits": bits}
        for name, direction, bits in [
            ("ap_clk", "input", 1),
            ("ap_rst_n", "input", 1),
            ("ap_start", "input", 1),
            ("ap_done", "output", 1),
            ("x_TDATA", "input", 16),
            ("x_TVALID", "input", 1),
            ("x_TREADY", "output", 1),
            ("y_TDATA", "output", 32),
            ("y_TVALID", "output", 1),
            ("y_TREADY", "input", 1),
        ]
    }
    assert board.validate_interface(record, ports, geometry) == "stream"
    source = board.render_top(record, "stream", geometry, facts())
    assert "wire [31:0] s_tdata;" in source
    assert ".OUTPUT_PORT_BITS(32)" in source and ".OUTPUT_BEATS(2)" in source
    record["interface"]["stream"]["output_beats_per_sample"] = 3
    with pytest.raises(ValueError, match="output_beats_per_sample"):
        geo.geometry_from_contract({"precision": "ap_fixed<16,6>"}, wrapper=record)


@pytest.mark.parametrize("side", ["input", "output"])
def test_partial_lanes_are_not_silently_zero_padded_without_core_semantics(side):
    geo = load("board-host-io/scripts/host_geometry.py", "partial_geometry")
    record = stream_record()
    stream = record["interface"]["stream"]
    stream[side + "_lanes_per_beat"] = 2 if side == "input" else 3
    with pytest.raises(ValueError, match="exact division"):
        geo.geometry_from_contract({"precision": "ap_fixed<16,6>"}, wrapper=record)


@pytest.mark.parametrize(
    "beats,gaps,early,lanes",
    [
        (2, 0, 0, 1),
        (3, 3, 0, 1),
        (32, 3, 0, 1),
        (2, 0, 1, 1),
        (3, 2, 1, 1),
        (3, 2, 0, 2),
        (3, 2, 1, 4),
    ],
)
def test_output_beat_aggregation_with_stalls_and_repeated_requests(
    tmp_path, beats, gaps, early, lanes
):
    runner = load("board-host-io/scripts/run_rtl_tb.py", "multiout_runner")
    source = (ROOT / "board-host-io/tests/host_stream_output_beats_tb.v").read_text()
    source = source.replace(
        "OUTPUT_BEATS=2, GAP_CYCLES=0, EARLY_OUTPUT=0, OUTPUT_LANES=1",
        f"OUTPUT_BEATS={beats}, GAP_CYCLES={gaps}, EARLY_OUTPUT={early}, OUTPUT_LANES={lanes}",
    )
    bench = tmp_path / "host_stream_output_beats_tb.v"
    bench.write_text(source)
    result = runner.run_testbench(
        "host_stream_output_beats_tb",
        [ROOT / "board-host-io/assets/host_stream_adapter.v", bench],
        tmp_path,
    )
    (tmp_path / "simulation-result.json").write_text(json.dumps(result, indent=2))
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "HOST_STREAM_OUTPUT_BEATS_PASS"
