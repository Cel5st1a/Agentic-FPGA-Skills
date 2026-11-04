"""Regressions for real emitted Softmax fields and stage selection."""

from test_s15_numeric_policy import load


def audit(tmp_path, body, options, parent="nnet::softmax_config", base=""):
    fw = tmp_path / "firmware"
    fw.mkdir()
    (fw / "parameters.h").write_text(
        base + f"// probabilities\nstruct config5 : {parent} {{\n{body}\n}};\n"
    )
    mod = load("hls4ml-convert/scripts/contract_config.py", "s15_audit")
    config = {"LayerName": {"probabilities": options}}
    return mod.firmware_config_audit(
        tmp_path, config, config, [{"name": "probabilities", "class_name": "Softmax"}]
    )


def test_stable_softmax_dual_fields_are_not_reported_missing(tmp_path):
    record = audit(
        tmp_path,
        "static const unsigned exp_table_size=32768;\nstatic const unsigned inv_table_size=32768;",
        {"TableSize": 32768},
    )
    assert record["status"] == "PASSED"
    assert record["layer_checks"][0]["TableSize"]["emitted"] == {
        "exp_table_size": 32768,
        "inv_table_size": 32768,
    }


def test_distinct_exp_and_inv_sizes_are_preserved(tmp_path):
    record = audit(
        tmp_path,
        "static const unsigned exp_table_size=8192;\nstatic const unsigned inv_table_size=32768;",
        {"ExpTableSize": 8192, "InvTableSize": 32768},
    )
    assert record["status"] == "PASSED"
    check = record["layer_checks"][0]
    assert check["ExpTableSize"]["emitted"] == 8192
    assert check["InvTableSize"]["emitted"] == 32768


def test_native_snake_case_configuration_fields_are_audited(tmp_path):
    record = audit(tmp_path, 'static const unsigned exp_table_size=8192;\n'
                   'static const unsigned inv_table_size=32768;',
                   {'exp_table_size': 8192, 'inv_table_size': 32768})
    assert record['status'] == 'PASSED'
    assert record['layer_checks'][0]['InvTableSize']['emitted'] == 32768


def test_conflicting_spellings_are_reported(tmp_path):
    record = audit(tmp_path, 'static const unsigned exp_table_size=8192;',
                   {'ExpTableSize': 32768, 'exp_table_size': 8192})
    assert record['status'] == 'FAILED'
    assert any('conflict' in x for x in record['mismatches'])


def test_missing_inverse_field_is_not_accepted(tmp_path):
    record = audit(tmp_path, "static const unsigned exp_table_size=8192;", {"TableSize": 8192})
    assert record["status"] == "FAILED"


def test_local_inheritance_and_alias_are_resolved(tmp_path):
    record = audit(
        tmp_path,
        "static const unsigned exp_table_size=table_size;",
        {"TableSize": 8192},
        parent="local_base",
        base="struct local_base {\nstatic const unsigned table_size=8192;\n"
        "static const unsigned inv_table_size=table_size;\n};\n",
    )
    assert record["status"] == "PASSED"
    assert record["layer_checks"][0]["TableSize"]["emitted"] == {
        "exp_table_size": 8192,
        "inv_table_size": 8192,
    }


def test_unresolved_cpp_expression_is_unknown_not_guessed(tmp_path):
    record = audit(tmp_path, "static const unsigned table_size=1 << 13;", {"TableSize": 1})
    assert record["status"] == "FAILED"
    assert record["layer_checks"][0]["TableSize"]["emitted"] is None


def test_csim_options_disable_other_stages(tmp_path):
    mod = load("vitis-build-agent/scripts/run_stage.py", "s15_stage_flags")
    options = mod.write_options(tmp_path, mod.STAGE_FLAGS["csim"])
    assert options == {
        "reset": 0,
        "csim": 1,
        "synth": 0,
        "cosim": 0,
        "validation": 0,
        "export": 0,
        "vsynth": 0,
    }


def test_automatic_output_selection_keeps_complete_model():
    mod = load("hls4ml-convert/scripts/contract_config.py", "s15_output")
    assert mod.original_output_mode({}) == "probabilities"
    assert mod.original_output_mode({"gate_output": "auto"}) == "probabilities"
    import pytest

    with pytest.raises(ValueError, match="complete original"):
        mod.original_output_mode({"gate_output": "logits"})
