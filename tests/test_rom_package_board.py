"""Render package documents from pinned board facts, with gate execution isolated."""
import importlib.util
import json
from pathlib import Path
import sys
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rom_packager", ROOT/"bitstream-package/scripts/package_bitstream.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def package_fixture(tmp_path, board):
    run = tmp_path / "run"
    pkg = run/"contract/model"
    pkg.mkdir(parents=True)
    deployment = {"board_profile": "board.yaml", "model": {}, "clock_period_ns": 10}
    (pkg/"deployment.yaml").write_text(yaml.safe_dump(deployment))
    profile = ROOT/"board-profiles"/board/"board_profile.yaml"
    facts = yaml.safe_load(profile.read_text(encoding="utf-8"))
    (pkg/"board.yaml").write_text(yaml.safe_dump({"id": board, "part": facts["fpga"]["part"]}))
    build = run/"p3-build/board"
    build.mkdir(parents=True)
    bit = build/"board_top.bit"
    bit.write_bytes(b"test bitstream")
    wrapper = {"board": {"pins_profile": str(profile)}}
    (build/"wrapper.json").write_text(json.dumps(wrapper))
    evidence = run/"evidence"
    evidence.mkdir()
    gate = {"passed": True, "criterion": {"rule": "fixture", "atol": 0.001, "rtol": 0.01, "samples": 1},
            "values": {"violations": 0, "total": 1}, "max_abs_error": 0, "max_error_over_tolerance": 0}
    records = {"vivado-bitstream-evidence.json": {"part": facts["fpga"]["part"], "top": "board_top"},
               "vivado-impl-evidence.json": {"resource_gate": {"passed": True, "worst": "DSP", "worst_fraction": 0.1, "limit": 0.8},
                  "timing": {"wns_ns": 1, "whs_ns": 0.1, "met": True}},
               "csim-evidence.json": {"numerical_gate": gate}, "board-sim-evidence.json": {"board_gate": gate},
               "csynth-evidence.json": {}}
    for name, record in records.items():
        (evidence/name).write_text(json.dumps({"status": "PASSED", **record}))
    report = {"bitstream": {"path": str(bit), "bytes": 14, "sha256": "fixture"},
              "artifacts": {}, "wrapper": wrapper, "recomputed_gates": {}, "stages": {},
              "io_scope": {"verification_scope": "configure-only"}}
    return run, report


@pytest.mark.parametrize("board,clock", [("NexysVideo", "100 MHz single_ended"), ("AX7A200B", "200 MHz differential")])
def test_rom_package_labels_and_clock_prose_use_recorded_profile(monkeypatch, tmp_path, board, clock):
    run, report = package_fixture(tmp_path, board)
    # Presentation fixture only: existing full gate tests remain the acceptance proof.
    monkeypatch.setattr(packager, "verify_run", lambda *a: ([], report))
    monkeypatch.setattr(sys, "argv", ["package", "--run", str(run), "--model-id", "model", "--release-root", str(tmp_path/"release")])
    assert packager.main() == 0
    package = tmp_path/"release/model"
    assert (package/"README.md").read_text(encoding="utf-8").splitlines()[0] == f"# model - {board} bitstream package"
    verification = (package/"VERIFICATION.md").read_text(encoding="utf-8")
    assert clock in verification
    assert (package/"board_pins_profile.yaml").read_bytes() == (ROOT/"board-profiles"/board/"board_profile.yaml").read_bytes()
    if board == "NexysVideo":
        assert "D-019" not in verification
        assert "human sign-off" not in verification


def test_rom_package_refuses_missing_pinned_profile_before_creating_release(monkeypatch, tmp_path):
    run, report = package_fixture(tmp_path, "NexysVideo")
    report["wrapper"] = {}
    monkeypatch.setattr(packager, "verify_run", lambda *a: ([], report))
    monkeypatch.setattr(sys, "argv", ["package", "--run", str(run), "--model-id", "model", "--release-root", str(tmp_path/"release")])
    with pytest.raises(ValueError, match="pins_profile"):
        packager.main()
    assert not (tmp_path/"release").exists()


def test_recorded_board_identity_refuses_profile_changed_after_build(tmp_path):
    import hashlib
    import shutil
    run, report = package_fixture(tmp_path, "NexysVideo")
    pinned = tmp_path/"pins.yaml"
    shutil.copyfile(ROOT/"board-profiles/NexysVideo/board_profile.yaml", pinned)
    report["wrapper"]["board"]["pins_profile"] = str(pinned)
    deployment = run/"contract/model/deployment.yaml"
    board_file = deployment.parent/"board.yaml"
    record = {"contract_sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
              "board_profile_sha256": hashlib.sha256(board_file.read_bytes()).hexdigest(),
              "board_pins_profile_sha256": hashlib.sha256(pinned.read_bytes()).hexdigest()}
    assert packager.verify_board_identity(deployment, report["wrapper"], [("build", record)]) == []
    facts = yaml.safe_load(pinned.read_text(encoding="utf-8"))
    facts["board_id"] = "another-board"
    pinned.write_text(yaml.safe_dump(facts))
    errors = packager.verify_board_identity(deployment, report["wrapper"], [("build", record)])
    assert any("board_pins_profile_sha256" in error for error in errors)
    board_file.write_text("id: AX7A200B\n")
    errors = packager.verify_board_identity(deployment, report["wrapper"], [("build", record)])
    assert any("board_profile_sha256" in error for error in errors)
