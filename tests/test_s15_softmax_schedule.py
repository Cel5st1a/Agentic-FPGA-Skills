import hashlib
from pathlib import Path

import pytest
from test_s15_numeric_policy import load

TEMPLATE = Path(
    "D:/python_envs/hls4ml_conv/Lib/site-packages/hls4ml/templates/"
    "vivado/nnet_utils/nnet_activation.h"
)


@pytest.mark.parametrize("factor", [1, 2, 4])
def test_only_stable_exp_read_pragma_changes(tmp_path, factor):
    mod = load("hls4ml-convert/scripts/generated_compat.py", "s15_schedule")
    target = tmp_path / "firmware/nnet_utils/nnet_activation.h"
    target.parent.mkdir(parents=True)
    target.write_bytes(TEMPLATE.read_bytes())
    before = target.read_bytes()
    result = mod.configure_softmax_exp_reads(tmp_path, "1.3.0", factor)
    assert result["status"] == "APPLIED"
    assert result["hardware_verified"] is False
    changed = target.read_bytes()
    assert (
        changed.replace(f"#pragma HLS unroll factor={factor}".encode(), b"#pragma HLS unroll", 1)
        == before
    )
    assert mod.configure_softmax_exp_reads(tmp_path, "1.3.0", factor)["status"] == "ALREADY_APPLIED"
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == result["original_sha256"]


def test_unknown_template_never_gets_patched(tmp_path):
    mod = load("hls4ml-convert/scripts/generated_compat.py", "s15_schedule_unknown")
    target = tmp_path / "firmware/nnet_utils/nnet_activation.h"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unknown template")
    with pytest.raises(ValueError, match="unrecognized"):
        mod.configure_softmax_exp_reads(tmp_path, "1.3.0", 1)
    assert target.read_bytes() == b"unknown template"


@pytest.mark.parametrize("factor", [True, 0, -1, 1.5, "1"])
def test_invalid_schedule_is_rejected_before_writing(tmp_path, factor):
    mod = load("hls4ml-convert/scripts/generated_compat.py", "s15_schedule_invalid")
    with pytest.raises(ValueError, match="positive integer"):
        mod.configure_softmax_exp_reads(tmp_path, "1.3.0", factor)
