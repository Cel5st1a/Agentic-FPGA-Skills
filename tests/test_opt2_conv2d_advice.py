import os
from pathlib import Path

import pytest

from test_s15_numeric_policy import load


def development_header():
    root = Path(os.environ.get("S15_DEVELOPMENT_EVIDENCE", Path(__file__).parents[2] / "evidence"))
    header = root / "20261026-07/p2-convert/project/firmware/parameters.h"
    if not header.is_file():
        pytest.skip("optional 20261026 archived conversion fixture is unavailable")
    return header


def tool():
    return load("reuse-factor-resource-tuning/scripts/tune_reuse_factor.py", "opt2_rf")


def test_actual_conv2d_is_not_misclassified_as_dense():
    header = development_header()
    layers = tool().parse_parameters(header)
    conv = layers["conv2d_1"]
    assert conv["kind"] == "conv"
    assert conv["spatial_dimensions"] == 2
    assert conv["filt_height"] == 3
    assert tool().lever_dims(conv) == (9, 2)
    assert len(layers) == 2  # one convolution, one dense; no duplicate multiply block


def test_conv2d_fallback_includes_height_and_never_guesses_missing_height(tmp_path):
    header = tmp_path / "parameters.h"
    text = """// image_conv
struct config1 : nnet::conv2d_config {
 static const unsigned n_chan = 2;
 static const unsigned n_filt = 5;
 static const unsigned filt_width = 3;
 static const unsigned filt_height = 7;
 static const unsigned reuse_factor = 6;
};
"""
    header.write_text(text)
    rec = tool().parse_parameters(header)["image_conv"]
    assert tool().lever_dims(rec) == (42, 5)
    header.write_text(text.replace(" static const unsigned filt_height = 7;", ""))
    assert tool().lever_dims(tool().parse_parameters(header)["image_conv"]) is None


def test_conv1d_existing_geometry_stays_unchanged(tmp_path):
    header = tmp_path / "parameters.h"
    header.write_text("""// wave_conv
struct config1 : nnet::conv1d_config {
 static const unsigned n_chan = 2;
 static const unsigned n_filt = 5;
 static const unsigned filt_width = 3;
};
""")
    assert tool().lever_dims(tool().parse_parameters(header)["wave_conv"]) == (6, 5)


def test_latency_kernel_never_claims_rf_predicts_physical_sharing():
    header = development_header()
    layers = tool().parse_parameters(header)
    for rec in layers.values():
        assert rec["kernel"] == "DenseLatency"
        diagnostic = tool().sharing_diagnostic(rec)
        assert diagnostic["rf_sharing_estimate_applicable"] is False
        assert diagnostic["measured_multiplier_count"] is None


def test_unknown_kernel_is_unknown_not_resource():
    assert tool().sharing_diagnostic({"kernel": None})["rf_sharing_estimate_applicable"] is None
