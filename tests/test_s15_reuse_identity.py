"""Identity comparison of development files, not numerical/hardware qualification."""

import copy
import json
import subprocess
import sys

import pytest
import yaml
from test_s15_numeric_policy import ROOT, load


def fixture(root):
    root.mkdir(parents=True)
    for name, text in {
        "model.bin": "original-model",
        "weights.bin": "original-weights",
        "kernel.cpp": "kernel-source",
        "core.v": "module core; endmodule",
        "board_top.v": "module board_top; endmodule",
        "inputs.json": "[[0.0]]",
    }.items():
        (root / name).write_text(text)
    (root / "board.yaml").write_text(yaml.safe_dump({"part": "xc7a200tfbg484-2"}))
    contract = {
        "model": {
            "path": "model.bin",
            "associated_files": ["weights.bin"],
            "frontend": "development",
            "input_shape": [1, 1],
        },
        "board_profile": "board.yaml",
        "precision": "ap_fixed<18,6>",
        "clock_period_ns": 10,
        "io_type": "io_parallel",
        "verification": {"inputs": "inputs.json"},
    }
    deployment = root / "deployment.yaml"
    deployment.write_text(yaml.safe_dump(contract))
    return deployment, {
        "arithmetic_config": {
            "part": "xc7a200tfbg484-2",
            "clock_period_ns": 10,
            "io_type": "io_parallel",
            "hls_config": {"Model": {"Precision": "ap_fixed<18,6>"}},
        },
        "tool_versions": {
            "python": "fixture-python",
            "frontend_runtime": "fixture-runtime",
            "hls4ml": "fixture-hls4ml",
            "hls": "fixture-hls",
            "vivado": "fixture-vivado",
        },
        "arithmetic_files": {"kernel": root / "kernel.cpp"},
        "core_files": {"core": root / "core.v"},
        "wrapper_files": {"top": root / "board_top.v"},
    }


def test_relocation_is_not_change_and_wrapper_only_change_preserves_core(tmp_path):
    mod = load("scripts/line/identity.py", "reuse_identity")
    deployment, args = fixture(tmp_path / "first")
    other, relocated = fixture(tmp_path / "second")
    first = mod.capture_reuse_identity(deployment, **args)
    same = mod.capture_reuse_identity(other, **relocated)
    assert mod.compare_reuse_identity(first, same)["core_identity_matches"] is True
    (other.parent / "board_top.v").write_text("wrapper changed")
    changed = mod.capture_reuse_identity(other, **relocated)
    result = mod.compare_reuse_identity(first, changed)
    assert result["core_identity_matches"] is True and result["wrapper_identity_matches"] is False
    assert "core_build" not in result["affected_stages"]
    assert result["hardware_verified"] is False


@pytest.mark.parametrize(
    "kind", ["model", "weights", "arithmetic", "core", "config", "tool", "part"]
)
def test_real_dependency_changes_invalidate_core_even_when_top_rtl_unchanged(tmp_path, kind):
    mod = load("scripts/line/identity.py", "reuse_changed")
    deployment, args = fixture(tmp_path / "run")
    first = mod.capture_reuse_identity(deployment, **args)
    files = {
        "model": "model.bin",
        "weights": "weights.bin",
        "arithmetic": "kernel.cpp",
        "core": "core.v",
        "part": "board.yaml",
    }
    if kind in files:
        value = "changed" if kind != "part" else yaml.safe_dump({"part": "different-part"})
        (deployment.parent / files[kind]).write_text(value)
        if kind == "part":
            args["arithmetic_config"]["part"] = "different-part"
    elif kind == "config":
        args["arithmetic_config"]["new_option"] = "changed"
    else:
        args["tool_versions"]["hls"] = "different-hls"
    current = mod.capture_reuse_identity(deployment, **args)
    result = mod.compare_reuse_identity(first, current)
    assert result["core_identity_matches"] is False
    assert "core_build" in result["affected_stages"]


def test_missing_files_or_unknown_tools_are_unknown_not_a_matching_empty_digest(tmp_path):
    mod = load("scripts/line/identity.py", "reuse_unknown")
    deployment, args = fixture(tmp_path / "run")
    first = mod.capture_reuse_identity(deployment, **args)
    args["tool_versions"]["hls"] = None
    (deployment.parent / "weights.bin").unlink()
    current = mod.capture_reuse_identity(deployment, **args)
    result = mod.compare_reuse_identity(first, current)
    assert result["core_identity_matches"] is None
    assert result["affected_stages"] is None
    assert current["missing"]["core"]
    assert mod.compare_reuse_identity({}, {})["core_identity_matches"] is None


def test_vivado_change_is_wrapper_scope_and_missing_input_does_not_invalidate_core(tmp_path):
    mod = load("scripts/line/identity.py", "reuse_wrapper")
    deployment, args = fixture(tmp_path / "run")
    first = mod.capture_reuse_identity(deployment, **args)
    args["tool_versions"]["vivado"] = "changed-vivado"
    current = mod.capture_reuse_identity(deployment, **args)
    result = mod.compare_reuse_identity(first, current)
    assert result["core_identity_matches"] is True
    assert result["wrapper_identity_matches"] is False
    (deployment.parent / "inputs.json").unlink()
    current = mod.capture_reuse_identity(deployment, **args)
    result = mod.compare_reuse_identity(first, current)
    assert result["core_identity_matches"] is True
    assert result["wrapper_identity_matches"] is None


@pytest.mark.parametrize("unknown", [None, "UNKNOWN", "MISSING"])
def test_absent_wrapper_sources_and_additional_unknown_tool_are_not_implicitly_known(
    tmp_path, unknown
):
    mod = load("scripts/line/identity.py", "reuse_incomplete")
    deployment, args = fixture(tmp_path / "run")
    first = mod.capture_reuse_identity(deployment, **args)
    args["wrapper_files"] = {}
    current = mod.capture_reuse_identity(deployment, **args)
    assert mod.compare_reuse_identity(first, current)["wrapper_identity_matches"] is None
    args["tool_versions"]["custom_generator"] = unknown
    current = mod.capture_reuse_identity(deployment, **args)
    assert mod.compare_reuse_identity(first, current)["core_identity_matches"] is None


def test_stage_delta_does_not_treat_unchanged_rtl_as_unchanged_model(tmp_path):
    stage = load("vivado-manager/scripts/stage_rtl.py", "reuse_stage")
    deployment, args = fixture(tmp_path / "run")
    request = {
        k: copy.deepcopy(v) for k, v in args.items() if k not in {"core_files", "wrapper_files"}
    }
    board = deployment.parent / "board"
    board.mkdir()
    (board / "board_top.v").write_text("module board_top; endmodule")
    core = deployment.parent / "generated/core.v"
    core.parent.mkdir()
    core.write_bytes((deployment.parent / "core.v").read_bytes())
    wrapper = {"contract": str(deployment), "core_verilog": str(core), "reuse_inputs": request}
    before = stage.stage_rtl(board, wrapper)
    (deployment.parent / "model.bin").write_text("changed model but stale RTL")
    after = stage.stage_rtl(board, wrapper)
    result = stage.dependency_delta(before, after)
    assert result["staged_core_content_unchanged"] is True
    assert result["reuse_identity_comparison"]["core_identity_matches"] is False
    assert "core_build" in result["reuse_identity_comparison"]["affected_stages"]
    # JSON transport must remain usable without an LLM.
    json.dumps(before)


def test_cli_capture_compare_and_existing_output_preservation(tmp_path):
    deployment, args = fixture(tmp_path / "run")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"deployment": str(deployment), **args}, default=str))
    snapshot, report = tmp_path / "snapshot.json", tmp_path / "report.json"
    program = ROOT / "scripts/line/identity.py"
    for command in (
        ["--capture", str(request), "--output", str(snapshot)],
        ["--compare", str(snapshot), str(snapshot), "--output", str(report)],
    ):
        run = subprocess.run(
            [sys.executable, str(program), *command], capture_output=True, text=True
        )
        assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(report.read_text())
    assert result["core_identity_matches"] is True
    assert result["hardware_verified"] is False
    old = report.read_bytes()
    failed = subprocess.run(
        [
            sys.executable,
            str(program),
            "--compare",
            str(snapshot),
            str(snapshot),
            "--output",
            str(report),
        ],
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0 and report.read_bytes() == old
    (tmp_path / "refused-overwrite.stderr.txt").write_text(failed.stderr)
