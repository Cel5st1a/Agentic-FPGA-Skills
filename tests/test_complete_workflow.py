import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/line"))


def test_reference_graph_resolves_flatten_and_rejects_unknown_computation():
    from fixed_reference import parse_graph

    source = """
auto& flat = pooled;
nnet::pooling2d_cl<input_t, layer_t, config2>(input, pooled);
nnet::dense<layer_t, result_t, config3>(flat, output, w3, b3);
"""
    graph = parse_graph(source)
    assert graph[1]["input"] == "pooled"
    assert graph[1]["weights"] == ["w3", "b3"]
    with pytest.raises(ValueError, match="unsupported"):
        parse_graph("nnet::mystery<input_t, result_t, cfg>(input, output);")


def test_completion_rejects_missing_fourth_level_and_single_candidate():
    from workflow_verify import choose

    valid = {
        "id": "a",
        "configuration_sha256": "first",
        "clock_mhz": 100,
        "wns_ns": 0.1,
        "whs_ns": 0.01,
        "adapter_cycles": 50,
        "resources": {"dsp": 3, "lut": 100, "ff": 120, "bram": 2},
        "comparison": {"complete": True, "pass": True},
    }
    with pytest.raises(ValueError, match="two"):
        choose([valid])
    missing = {
        **valid,
        "id": "b",
        "configuration_sha256": "second",
        "comparison": {"complete": False},
    }
    with pytest.raises(ValueError, match="two"):
        choose([valid, missing])
    second = {**valid, "id": "b", "configuration_sha256": "second", "adapter_cycles": 40}
    result = choose([valid, second])
    assert result["selected"] == "b"
    assert result["ranking"] == ["b", "a"]
    with pytest.raises(ValueError, match="two"):
        choose([{**valid, "effective_sha256": "same"}, {**second, "effective_sha256": "same"}])


def test_four_level_comparison_rejects_short_capture_and_reports_the_first_bad_value():
    from workflow_verify import compare_levels

    levels = {key: [[1.0, 2.0]] for key in ("G1", "G2", "G3", "G4")}
    levels["G4"] = [[1.0]]
    with pytest.raises(ValueError, match="shape"):
        compare_levels(levels)
    levels["G4"] = [[1.0, 2.1]]
    result = compare_levels(levels)
    assert result["complete"] and not result["pass"]
    difference = result["pairs"]["G1-G4"]["first_differences"][0]
    assert difference["sample"] == 0 and difference["output"] == 1
