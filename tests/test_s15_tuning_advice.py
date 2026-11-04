"""Advice must describe actual firmware knobs, not claim measured hardware results."""

import hashlib
import json
import subprocess
import sys

from test_s15_numeric_policy import ROOT, load


def test_softmax_only_cli_reports_schedule_choices_without_eda_or_overwrite(tmp_path):
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text("precision: ap_fixed<24,8>\nreuse_factor: 1\n")
    parameters = tmp_path / "parameters.h"
    parameters.write_text("""// probabilities
struct softmax_config4 : nnet::activ_config {
 static const unsigned n_in = 8;
 static const unsigned exp_table_size = 32768;
 static const unsigned inv_table_size = 32768;
};
""")
    before = hashlib.sha256(parameters.read_bytes()).hexdigest()
    report = tmp_path / "advice.json"
    command = [
        sys.executable,
        str(ROOT / "reuse-factor-resource-tuning/scripts/tune_reuse_factor.py"),
        "--deployment",
        str(deployment),
        "--parameters",
        str(parameters),
        "--out",
        str(report),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text())
    advice = data["softmax_scheduling"][0]
    assert advice["layer"] == "probabilities" and advice["exp_table_size"] == 32768
    assert advice["recommended_factor"] is None
    assert [x["softmax_exp_read_factor"] for x in advice["choices"]] == [None, 1, 2, 4]
    assert advice["measured_resources"] is advice["measured_latency_cycles"] is None
    assert data["eda_executed"] is False and data["hardware_verified"] is False
    assert hashlib.sha256(parameters.read_bytes()).hexdigest() == before
    previous = report.read_bytes()
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode != 0 and report.read_bytes() == previous


def test_reuse_advice_does_not_invent_multiplier_floor_or_latency(tmp_path):
    tool = load("reuse-factor-resource-tuning/scripts/tune_reuse_factor.py", "honest_rf_advice")
    assert tool.multiplier_count(256, 4, 1024) == 1
    bounds = tool.candidate_costs(256, 4, [1, 256, 1024])
    assert bounds[-1]["predicted_multipliers"] == 1
    assert bounds[-1]["measured_latency_cycles"] is None
    assert bounds[-1]["reuse_factor"] == 1024


def test_dense_advice_uses_emitted_factor_and_labels_request_difference(tmp_path):
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text("precision: ap_fixed<24,8>\nreuse_factor: 1\n")
    parameters = tmp_path / "parameters.h"
    parameters.write_text("""// dense
struct config1 : nnet::dense_config {
 static const unsigned n_in = 256;
 static const unsigned n_out = 4;
 static const unsigned reuse_factor = 256;
};
""")
    report = tmp_path / "advice.json"
    run = subprocess.run(
        [
            sys.executable,
            str(ROOT / "reuse-factor-resource-tuning/scripts/tune_reuse_factor.py"),
            "--deployment",
            str(deployment),
            "--parameters",
            str(parameters),
            "--out",
            str(report),
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    layer = json.loads(report.read_text())["layers"][0]
    assert layer["factor_in_firmware"] == 256 and layer["requested_factor"] == 1
    assert layer["request_matches_firmware"] is False
    assert layer["predicted_multipliers_now"] == 4
    assert layer["minimum_predicted_multipliers"] == 1
    assert layer["recommended_factor"] is None
