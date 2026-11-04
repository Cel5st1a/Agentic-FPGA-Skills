"""Reproductions from the final independent review; no hardware or EDA."""

import json

import pytest
from test_s15_numeric_policy import load


@pytest.mark.parametrize("hls", [None, {"passed": False}])
def test_explicit_sim_diagnostic_does_not_require_hls_reference(tmp_path, hls):
    build = load("board-host-io/scripts/build_host.py", "optional_build_hls")
    record = {
        "status": "PASSED",
        "contract_sha256": "c",
        "core_verilog_sha256": "v",
        "model_sha256": "m",
        "requested_samples": 256,
        "received_samples": 256,
        "staged_rtl": {"core.v": "hash"},
        "python": {"passed": True},
        "hls": hls,
    }
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(record))
    build.require_host_sim(path, "c", "v", "m", {"core.v": "hash"})


@pytest.mark.parametrize("section,name", [("LayerName", "probabilities"), ("LayerType", "Softmax")])
def test_skip_configuration_cannot_remove_complete_original_nonlinear(section, name):
    config = load("hls4ml-convert/scripts/contract_config.py", "no_skip_nonlinear")
    contract = {
        "precision": "ap_fixed<24,8>",
        "reuse_factor": 1,
        "strategy": "Resource",
        "hls_config": {section: {name: {"skip": True}}},
    }
    with pytest.raises(ValueError, match="(skip|complete|original)"):
        config.apply_contract_config({}, contract, [])


def test_generated_multiplier_literal_or_unknown_expression_is_not_recomputed_as_fact():
    tool = load("reuse-factor-resource-tuning/scripts/tune_reuse_factor.py", "emitted_expr")
    fields = """static const unsigned n_in=8;
static const unsigned n_out=4;
static const unsigned reuse_factor=2;
static const unsigned n_zeros=0;
"""
    assert tool.emitted_multiplier_limit(fields + "static const unsigned multiplier_limit=7;") == 7
    assert (
        tool.emitted_multiplier_limit(
            fields + "static const unsigned multiplier_limit=OTHER_MACRO(n_in);"
        )
        is None
    )


@pytest.mark.parametrize(
    "section,name", [("LayerName", "probabilities"), ("LayerType", "Softmax"), ("Model", None)]
)
@pytest.mark.parametrize("option", [{"Implementation": "argmax"}, {"skip": True}])
def test_full_model_cannot_be_bypassed_by_argmax_or_model_defaults(section, name, option):
    config = load("hls4ml-convert/scripts/contract_config.py", "no_argmax")
    override = {section: {name: option}} if name else {section: option}
    contract = {
        "precision": "ap_fixed<24,8>",
        "reuse_factor": 1,
        "strategy": "Resource",
        "hls_config": override,
    }
    with pytest.raises(ValueError, match="(skip|argmax|complete|original)"):
        config.apply_contract_config({}, contract, [])


def test_actual_softmax_roles_follow_selected_implementation(tmp_path):
    import os
    import subprocess

    from test_s15_numeric_policy import ROOT

    probe = r"""
import sys,json
from pathlib import Path
import hls4ml
import tensorflow as tf
sys.path.insert(0,str(Path(sys.argv[1])/'hls4ml-convert/scripts'))
from local_precision_search import role_precision
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(3,),name='logits'),
    tf.keras.layers.Activation('softmax',name='probabilities')])
records={}
for impl in ['stable','latency','legacy']:
    graph=hls4ml.converters.convert_from_keras_model(model,
        hls_config={'Model':{'Precision':'ap_fixed<24,8>','ReuseFactor':1},
                    'LayerName':{'probabilities':{'Implementation':impl}}},
        output_dir=str(Path(sys.argv[2])/impl),backend='Vitis',
        part='xc7a200tfbg484-2',io_type='io_parallel')
    node=next(n for n in graph.get_layers() if n.name=='probabilities')
    used=['table'] if impl=='legacy' else ['exp_table','inv_table','inv_inp','accum']
    unused=['exp_table','inv_table','inv_inp','accum'] if impl=='legacy' else ['table']
    for role in used:
        assert role_precision(node,role)
    for role in unused:
        try:
            role_precision(node,role)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{impl} accepted unused role {role}')
    records[impl]={'used':used,'unused':unused}
print('IMPLEMENTATION_ROLES_PASS '+json.dumps(records))
"""
    run = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-B",
            "-c",
            probe,
            str(ROOT),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TF_CPP_MIN_LOG_LEVEL": "2"},
    )
    (tmp_path / "probe.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (tmp_path / "probe.stderr.txt").write_text(run.stderr, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "IMPLEMENTATION_ROLES_PASS" in run.stdout


def test_program_status_diagnostics_do_not_block_confirmed_configuration(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    from types import SimpleNamespace

    program = load("board-host-io/scripts/program_jtag.py", "numeric_program_status")
    monkeypatch.setattr(
        program,
        "validate_inputs",
        lambda *a: {
            "board": {
                "pins_profile": "software fixture",
                "jtag": {"cable_serial": "TEST123", "target_name": "xc7a200t"},
            }
        },
    )
    monkeypatch.setattr(program, "CableLock", lambda *a: nullcontext())
    calls = []

    def fake_xsdb(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0, stdout="TEST123 xc7a200t\nAFD_AFTER FPGA is configured\n", stderr=""
        )

    monkeypatch.setattr(program, "run_xsdb", fake_xsdb)
    result = program.program(
        tmp_path / "a.bit",
        tmp_path / "scope",
        tmp_path / "evidence",
        tmp_path / "attempt",
        None,
        Path("xsdb"),
    )
    assert result["programmed"] is True and result["protocol_diagnostics"]
    assert result["hardware_numerical_verified"] is False
    assert calls[0]["timeout"] is None
    with pytest.raises(ValueError, match="configured"):
        program.validate_program_stdout(
            "TEST123 xc7a200t", "TEST123", "xc7a200t", strict_status=False
        )
