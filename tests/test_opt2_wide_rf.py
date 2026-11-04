import pytest
from test_s15_numeric_policy import load


def tool():
    return load("reuse-factor-resource-tuning/scripts/tune_reuse_factor.py", "wide_rf")


@pytest.mark.parametrize(
    "layer", ["conv2d", "branched_dense", "dense100", "keras3", "qdense", "k1"]
)
def test_explicit_layer_config_not_misreported_as_global_rf(layer):
    config = {"reuse_factor": 1, "hls_config": {"LayerName": {layer: {"ReuseFactor": 8}}}}
    assert tool().effective_factor(layer, config) == 8
    assert tool().factor_request(layer, config)["source"] == "hls_config.LayerName"


def test_case_insensitive_layer_name_and_consistent_duplicate_sources():
    config = {
        "reuse_factor": 1,
        "layer_reuse_factors": {"Dense": 8},
        "hls_config": {"LayerName": {"dense": {"ReuseFactor": 8}}},
    }
    assert tool().effective_factor("DENSE", config) == 8


def test_unknown_layer_type_does_not_create_false_mismatch():
    config = {"reuse_factor": 1, "hls_config": {"LayerType": {"QDense": {"ReuseFactor": 8}}}}
    assert tool().effective_factor("hidden", config) is None
    assert tool().factor_request("hidden", config)["source"] == "unknown_layer_type"


def test_conflicting_or_ambiguous_requests_stay_unknown():
    config = {
        "reuse_factor": 1,
        "layer_reuse_factors": {"dense": 4},
        "hls_config": {"LayerName": {"dense": {"ReuseFactor": 8}}},
    }
    assert tool().effective_factor("dense", config) is None
    assert tool().factor_request("dense", config)["conflict"] is True


def test_global_only_still_works():
    assert tool().effective_factor("dense", {"reuse_factor": 32}) == 32


@pytest.mark.parametrize(
    "section,block",
    [
        ("Model", {"ReuseFactor": None}),
        ("LayerType", {"Dense": {"ReuseFactor": None}}),
        ("LayerName", {"dense": None}),
    ],
)
def test_null_config_fields_are_unset_like_converter(section, block):
    assert (
        tool().effective_factor("dense", {"reuse_factor": 8, "hls_config": {section: block}}) == 8
    )
