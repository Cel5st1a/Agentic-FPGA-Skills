"""Tests for reuse-factor-resource-tuning.

Two kinds of case:

* **Fixture-based** — the generated `parameters.h` and contract shapes are embedded below,
  so these run on any machine and pin the parser's structural rules (a convolution is two
  structs whose names correlate by *struct*, not by layer; a `_mult` struct carries a layer
  comment too and must not be mistaken for a Dense layer).
* **Real-report** — skip when the artefacts are absent, and assert the numbers the skill
  quotes in its cost-model table against what the runs actually produced.

Run:
    pytest C:/Users/Administrator/Agentic_FPGA_Deployment_skills/reuse-factor-resource-tuning/scripts/test_tune_reuse_factor.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tr = _load("tune_reuse_factor", HERE / "tune_reuse_factor.py")

# Deliberately a copy of the real generator's shape, including the two traps:
#   * `config3_mult` (a convolution's multiply block) carries the layer comment
#     `// encoder_conv_0`, so "has a comment" does not mean "is a Dense layer";
#   * `config3` (the conv itself) carries NO comment, and `config3_mult` is named after
#     the struct `config3`, not after the layer `encoder_conv_0`.
PARAMETERS_FIXTURE = """\
// hls-fpga-machine-learning insert layer-config
// zp1d_encoder_conv_0
struct config12 : nnet::padding1d_config {
    static const unsigned in_width = 64;
    static const unsigned n_chan = 1;
};

// encoder_conv_0
struct config3_mult : nnet::dense_config {
    static const unsigned n_in = 5;
    static const unsigned n_out = 8;
    static const unsigned reuse_factor = 5;
    static const unsigned n_zeros = 0;
    static const unsigned multiplier_limit = DIV_ROUNDUP(n_in * n_out, reuse_factor) - n_zeros / reuse_factor;
    template<class data_T, class res_T, class CONFIG_T>
    using kernel = nnet::DenseResource_rf_leq_nin<data_T, res_T, CONFIG_T>;
};

struct config3 : nnet::conv1d_config {
    static const unsigned n_chan = 1;
    static const unsigned n_filt = 8;
    static const unsigned filt_width = 5;
    static const unsigned reuse_factor = 5;
};

// encoder_conv_1
struct config6_mult : nnet::dense_config {
    static const unsigned n_in = 24;
    static const unsigned n_out = 16;
    static const unsigned reuse_factor = 24;
    static const unsigned n_zeros = 0;
    static const unsigned multiplier_limit = DIV_ROUNDUP(n_in * n_out, reuse_factor) - n_zeros / reuse_factor;
    template<class data_T, class res_T, class CONFIG_T>
    using kernel = nnet::DenseResource_rf_leq_nin<data_T, res_T, CONFIG_T>;
};

struct config6 : nnet::conv1d_config {
    static const unsigned n_chan = 8;
    static const unsigned n_filt = 16;
    static const unsigned filt_width = 3;
    static const unsigned reuse_factor = 24;
};

// latent
struct config10 : nnet::dense_config {
    static const unsigned n_in = 256;
    static const unsigned n_out = 4;
    static const unsigned reuse_factor = 1;
    static const unsigned n_zeros = 0;
    static const unsigned multiplier_limit = DIV_ROUNDUP(n_in * n_out, reuse_factor) - n_zeros / reuse_factor;
    template<class data_T, class res_T, class CONFIG_T>
    using kernel = nnet::DenseResource_rf_leq_nin<data_T, res_T, CONFIG_T>;
};
"""

CONTRACT_FIXTURE = {
    "precision": "ap_fixed<32,12>",
    "reuse_factor": 1,
    "layer_reuse_factors": {"encoder_conv_0": 5, "encoder_conv_1": 24},
    "io_type": "io_stream",
    "constraints": {"resource_fraction_max": 0.8},
}


@pytest.fixture()
def parsed(tmp_path):
    path = tmp_path / "parameters.h"
    path.write_text(PARAMETERS_FIXTURE, encoding="utf-8")
    return tr.parse_parameters(path)


# --------------------------------------------------------------------------------------
# Parser structure
# --------------------------------------------------------------------------------------
def test_each_layer_reported_exactly_once(parsed):
    """The double-counting regression: one entry per layer, keyed by the HLS layer name."""
    assert sorted(parsed) == ["encoder_conv_0", "encoder_conv_1", "latent"]


def test_conv_mult_block_is_not_reported_as_a_dense_layer(parsed):
    """`config3_mult` carries `// encoder_conv_0`; that must not make it a Dense layer."""
    assert parsed["encoder_conv_0"]["kind"] == "conv"
    assert parsed["encoder_conv_1"]["kind"] == "conv"
    assert parsed["latent"]["kind"] == "dense"


def test_conv_dims_come_from_its_multiply_block(parsed):
    """The multiply block gives (kernel*n_chan, n_filt); the conv gives n_chan/n_filt."""
    assert tr.lever_dims(parsed["encoder_conv_0"]) == (5, 8)
    assert tr.lever_dims(parsed["encoder_conv_1"]) == (24, 16)
    assert tr.lever_dims(parsed["latent"]) == (256, 4)


def test_conv_records_the_factor_its_kernel_uses(parsed):
    assert parsed["encoder_conv_0"]["reuse_factor"] == 5
    assert parsed["encoder_conv_1"]["reuse_factor"] == 24
    assert parsed["latent"]["reuse_factor"] == 1


# --------------------------------------------------------------------------------------
# The cost model
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "n_in,n_out,rf,expected",
    [
        (64, 64, 64, 64),    # mlp enc_dense_0
        (64, 32, 64, 32),    # mlp enc_dense_1
        (32, 4, 64, 2),      # mlp latent -- rf > n_in, still /rf
        (5, 8, 5, 8),        # conv_0
        (24, 16, 24, 16),    # conv_1
        (256, 4, 2, 512),    # latent, the rejected intermediate
        (256, 4, 256, 4),    # latent, the shipped factor
    ],
)
def test_cost_model_matches_measured_rtl(n_in, n_out, rf, expected):
    """Every value here was counted from generated RTL; see the skill's table."""
    assert tr.multiplier_count(n_in, n_out, rf) == expected


def test_cost_model_is_an_upper_bound_at_rf_one():
    """Measured 426 against a prediction of 1024 -- never quote the prediction as a result."""
    assert tr.multiplier_count(256, 4, 1) == 1024


def test_emitted_multiplier_limit_is_read_and_agrees(parsed, tmp_path):
    """The firmware states its own limit; the prediction must agree with it."""
    for layer, rec in parsed.items():
        emitted = rec.get("emitted_multiplier_limit")
        assert emitted is not None, layer
        predicted = tr.multiplier_count(*tr.lever_dims(rec), rec["reuse_factor"])
        assert emitted == predicted, layer


# --------------------------------------------------------------------------------------
# Validity
# --------------------------------------------------------------------------------------
def test_valid_factor_sets_match_the_measured_sets():
    valid, _provenance = tr.hls4ml_valid_reuse_factors()
    assert tr.valid_factors(5, 8, valid) == [1, 5, 10, 20, 40]
    assert tr.valid_factors(256, 4, valid) == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
    conv1 = tr.valid_factors(24, 16, valid)
    assert conv1[:6] == [1, 2, 3, 4, 6, 8] and 24 in conv1


def test_the_two_convs_intersect_only_at_one():
    """Why a single global factor cannot satisfy several layers."""
    valid, _ = tr.hls4ml_valid_reuse_factors()
    assert set(tr.valid_factors(5, 8, valid)) & set(tr.valid_factors(24, 16, valid)) == {1}


def test_four_is_not_a_compromise_factor():
    """4 looks plausible and is invalid for conv_0 -- a real trap."""
    valid, _ = tr.hls4ml_valid_reuse_factors()
    assert 4 in tr.valid_factors(24, 16, valid)
    assert 4 in tr.valid_factors(256, 4, valid)
    assert 4 not in tr.valid_factors(5, 8, valid)


def test_predicate_provenance_is_recorded():
    _valid, provenance = tr.hls4ml_valid_reuse_factors()
    assert provenance, "the report must say where the predicate came from"


# --------------------------------------------------------------------------------------
# Behaviour that the skill promises
# --------------------------------------------------------------------------------------
def test_inherited_factor_is_flagged(tmp_path):
    """A layer with no override inherits the global; the report must say so."""
    params = tmp_path / "parameters.h"
    params.write_text(PARAMETERS_FIXTURE, encoding="utf-8")
    contract = tmp_path / "deployment.yaml"
    import yaml

    contract.write_text(yaml.safe_dump(CONTRACT_FIXTURE), encoding="utf-8")
    out = tmp_path / "report.json"

    argv = sys.argv
    try:
        sys.argv = ["tune_reuse_factor.py",
                    "--deployment", str(contract),
                    "--parameters", str(params),
                    "--out", str(out)]
        assert tr.main() == 0
    finally:
        sys.argv = argv

    report = json.loads(out.read_text(encoding="utf-8"))
    by_layer = {row["layer"]: row for row in report["layers"]}
    assert by_layer["latent"]["factor_inherited_from_global"] is True
    assert by_layer["encoder_conv_0"].get("factor_inherited_from_global") is None
    # The Dense layer dominates, and the recommendation must reach the target rather than
    # settle for the smallest increase (rf=2 is measured insufficient).
    assert report["area_dominating_layer"] == "latent"
    rec = report["recommendation"]
    assert rec["factor_proposed"] == 256
    assert rec["layer_reuse_factors"]["latent"] == 256
    # And it must never propose touching the global factor.
    assert "reuse_factor" not in rec["layer_reuse_factors"]


def test_report_never_proposes_lowering_the_global_factor(tmp_path):
    params = tmp_path / "parameters.h"
    params.write_text(PARAMETERS_FIXTURE, encoding="utf-8")
    import yaml

    contract = tmp_path / "deployment.yaml"
    contract.write_text(yaml.safe_dump(CONTRACT_FIXTURE), encoding="utf-8")
    out = tmp_path / "report.json"

    argv = sys.argv
    try:
        sys.argv = ["tune_reuse_factor.py", "--deployment", str(contract),
                    "--parameters", str(params), "--out", str(out)]
        tr.main()
    finally:
        sys.argv = argv

    report = json.loads(out.read_text(encoding="utf-8"))
    # Every refusal the skill documents must be recorded in the artefact.
    joined = " ".join(report["refusals"]).lower()
    assert "never proposes lowering" in joined
    assert "precision" in joined
    assert "resource_fraction_max" in joined
    # The contract's global factor is reported, not rewritten.
    assert report["contract"]["reuse_factor_global"] == 1


# --------------------------------------------------------------------------------------
# Real artefacts, when present
# --------------------------------------------------------------------------------------
REAL = {
    "asymmetric_cnn_fc/cnn04 (blocked: latent inherits global rf=1)": (
        Path(r"D:\ML_models\_line\runs\asymmetric_cnn_fc\20260928-cnn04"),
        Path(r"D:\ML_models\_line\runs\asymmetric_cnn_fc\20260928-cnn04"
             r"\contract\asymmetric_cnn_fc\deployment.yaml"),
        "asymmetric_cnn_fc"),
    "mlp_64_32/mlp02 (delivered, global rf=64)": (
        Path(r"D:\ML_models\_line\runs\mlp_64_32\20260928-mlp02"),
        Path(r"D:\ML_models\_line\runs\mlp_64_32\20260928-mlp02"
             r"\contract\mlp_64_32\deployment.yaml"),
        "mlp_64_32"),
}


@pytest.mark.parametrize("label,run,contract", [(k, v[0], v[1]) for k, v in REAL.items()])
def test_real_firmware_parses_with_layer_names(label, run, contract):
    params = run / "p2-convert" / "project" / "firmware" / "parameters.h"
    if not params.is_file():
        pytest.skip(f"artefact not on this machine: {params}")
    layers = tr.parse_parameters(params)
    if "mlp" in label:
        assert sorted(layers) == ["enc_dense_0", "enc_dense_1", "latent"]
    else:
        assert sorted(layers) == ["encoder_conv_0", "encoder_conv_1", "latent"]
    for name, rec in layers.items():
        assert tr.lever_dims(rec) is not None, name
