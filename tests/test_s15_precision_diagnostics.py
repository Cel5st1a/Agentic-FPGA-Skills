from types import SimpleNamespace

from test_s15_numeric_policy import load


def test_small_weights_and_overflow_are_distinct_diagnostics():
    mod = load("hls4ml-convert/scripts/numeric_diagnostics.py", "s15_precision")
    result = mod.fixed_grid_summary([1e-6, -1e-6, 0.0, 40.0], "ap_fixed<18,6>")
    assert result["step"] == 2**-12
    assert result["sub_lsb_nonzero_count"] == 2
    assert result["out_of_range_count"] == 1
    assert result["hardware_verified"] is False
    assert "zeroed_count" not in result  # Rounding mode is not inferred.


def test_float_and_unknown_types_are_not_coerced_to_fixed():
    mod = load("hls4ml-convert/scripts/numeric_diagnostics.py", "s15_precision_unknown")
    assert mod.fixed_grid_summary([1.0], "float")["status"] == "UNKNOWN_TYPE"


def test_diagnostics_bind_to_actual_layer_and_parameter_without_mutation():
    mod = load("hls4ml-convert/scripts/numeric_diagnostics.py", "s15_precision_model")
    values = [1e-6, 0.0]
    weight = SimpleNamespace(
        name="w7", data=values, type=SimpleNamespace(precision="ap_fixed<18,6,AP_RND,AP_SAT>")
    )
    node = SimpleNamespace(name="dense", get_weights=lambda: [weight])
    model = SimpleNamespace(get_layers=lambda: [node])
    result = mod.parameter_grid_diagnostics(model)
    assert result[0]["layer"] == "dense" and result[0]["parameter"] == "w7"
    assert result[0]["sub_lsb_nonzero_count"] == 1
    assert values == [1e-6, 0.0]
