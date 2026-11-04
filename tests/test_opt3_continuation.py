import hashlib
import json

import pytest
from test_s15_numeric_policy import load


def test_continuation_clones_only_verified_same_run_checkpoint(tmp_path, monkeypatch):
    mod = load("board-host-io/scripts/build_host.py", "opt3_continue")
    run = tmp_path / "run"
    deployment = run / "contract/model/deployment.yaml"
    deployment.parent.mkdir(parents=True)
    deployment.write_text("original contract")
    board = run / "p3-build/host-board"
    board.mkdir(parents=True)
    rtl = board / "rtl"
    rtl.mkdir()
    (rtl / "core.v").write_text("source")
    (board / "synth.dcp").write_bytes(b"checkpoint")
    (board / "failed.log").write_text("preserved failure")
    (board / "routed.dcp").write_bytes(b"partial failed implementation")

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    files = {"core.v": sha(rtl / "core.v")}
    identity = {
        "schema_version": 1,
        "kind": "reuse_identity",
        "missing": {"core": [], "wrapper": []},
        "fingerprints": {
            group: "a" * 64
            for group in [
                "model_files",
                "contract_arithmetic",
                "arithmetic_config",
                "core_tools",
                "arithmetic_files",
                "core_files",
                "wrapper_files",
                "wrapper_config",
                "wrapper_tools",
            ]
        },
    }
    monkeypatch.setattr(
        mod, "stage_rtl", lambda *args: {"files": files, "reuse_identity": identity}
    )
    evidence = run / "evidence.json"
    record = {
        "status": "PASSED",
        "stage": "vivado_synth",
        "board_dir": str(board),
        "contract": {"sha256": sha(deployment)},
        "rtl_used": {"files": files},
        "staged_rtl": {"reuse_identity": identity},
        "artifacts": {"synth.dcp": {"sha256": sha(board / "synth.dcp")}},
    }
    evidence.write_text(json.dumps(record))
    (board / "wrapper.json").write_text("{}")
    target = board.with_name("continuation-02")
    assert mod.prepare_continuation(deployment, board, target, "impl", evidence) == target
    assert (target / "synth.dcp").read_bytes() == b"checkpoint"
    assert (board / "failed.log").read_text() == "preserved failure"
    assert not (target / "routed.dcp").exists()
    assert (board / "routed.dcp").read_bytes() == b"partial failed implementation"
    assert json.loads((target / "continuation.json").read_text())["hardware_verified"] is False
    with pytest.raises(FileExistsError):
        mod.prepare_continuation(deployment, board, target, "impl", evidence)
    (board / "synth.dcp").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checkpoint"):
        mod.prepare_continuation(deployment, board, board.with_name("bad"), "impl", evidence)
    assert not board.with_name("bad").exists()


def test_selected_stage_suffix_stops_on_real_failure():
    mod = load("board-host-io/scripts/build_host.py", "opt3_stage_suffix")
    calls = []

    def stage(name):
        calls.append(name)
        return {"status": "FAILED"}

    with pytest.raises(ValueError):
        mod.execute_stages(stage, ("impl", "bitstream"))
    assert calls == ["impl"]


@pytest.mark.parametrize("change", ["none", "weights", "tool", "missing_identity"])
def test_continuation_reads_real_associated_weights_and_declared_tools(tmp_path, change):
    from test_s15_reuse_identity import fixture

    mod = load("board-host-io/scripts/build_host.py", "opt3_real_identity")
    deployment, request = fixture(tmp_path / "run/contract/model")
    board = tmp_path / "run/build/board"
    board.mkdir(parents=True)
    (board / "board_top.v").write_text("module board_top; endmodule")
    (board / "board.xdc").write_text("# synthetic constraints")
    core = deployment.parent / "generated/core.v"
    core.parent.mkdir()
    core.write_text((deployment.parent / "core.v").read_text())
    wrapper = {"core_verilog": str(core), "contract": str(deployment), "reuse_inputs": request}
    (board / "wrapper.json").write_text(json.dumps(wrapper, default=str))
    staged = mod.stage_rtl(board, wrapper)
    checkpoint = board / "synth.dcp"
    checkpoint.write_bytes(b"synthetic checkpoint, no EDA")

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    record = {
        "status": "PASSED",
        "stage": "vivado_synth",
        "board_dir": str(board),
        "contract": {"sha256": sha(deployment)},
        "rtl_used": {"files": staged["files"]},
        "staged_rtl": staged,
        "artifacts": {"synth.dcp": {"sha256": sha(checkpoint)}},
    }
    if change == "weights":
        (deployment.parent / "weights.bin").write_text("changed weights")
    if change == "tool":
        wrapper["reuse_inputs"]["tool_versions"]["vivado"] = "changed declared version"
        (board / "wrapper.json").write_text(json.dumps(wrapper, default=str))
    if change == "missing_identity":
        record.pop("staged_rtl")
    evidence = tmp_path / "run/previous.json"
    evidence.write_text(json.dumps(record))
    target = board.with_name("continuation")
    if change == "none":
        mod.prepare_continuation(deployment, board, target, "impl", evidence)
    else:
        with pytest.raises(ValueError, match="reuse identity"):
            mod.prepare_continuation(deployment, board, target, "impl", evidence)
    assert checkpoint.read_bytes() == b"synthetic checkpoint, no EDA"
