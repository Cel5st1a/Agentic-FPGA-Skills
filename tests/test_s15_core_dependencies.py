"""Content dependency regressions; no EDA, checkpoint or hardware success fixtures."""

import hashlib

import pytest
from test_s15_numeric_policy import load


def manifest(core):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in core.parent.iterdir()
        if p.suffix in {".v", ".mem"}
    }


def fixture(tmp_path):
    mod = load("vivado-manager/scripts/stage_rtl.py", "s15_core_dependencies")
    core = tmp_path / "core/top.v"
    core.parent.mkdir()
    core.write_text("module top; endmodule")
    (core.parent / "weights.mem").write_text("0001\n0002")
    board = tmp_path / "board"
    board.mkdir()
    (board / "board_top.v").write_text("module board_top; endmodule")
    wrapper = {"core_verilog": str(core), "core_source_manifest": manifest(core)}
    return mod, core, board, wrapper


@pytest.mark.parametrize("change", ["missing", "modified", "added"])
def test_changed_core_bundle_refused_before_staging_mutates_files(tmp_path, change):
    mod, core, board, wrapper = fixture(tmp_path)
    old = board / "rtl/keep-original.txt"
    old.parent.mkdir()
    old.write_text("previous attempt")
    weights = core.parent / "weights.mem"
    if change == "missing":
        weights.unlink()
    elif change == "modified":
        weights.write_text("FFFF")
    else:
        (core.parent / "extra.v").write_text("module extra; endmodule")
    with pytest.raises(ValueError, match="core source manifest"):
        mod.stage_rtl(board, wrapper)
    assert old.read_text() == "previous attempt"
    assert list(old.parent.iterdir()) == [old]


@pytest.mark.parametrize("filename", ["board_top.v", "Board_Top.v"])
def test_same_named_core_and_wrapper_sources_are_not_silently_overwritten(tmp_path, filename):
    mod, core, board, wrapper = fixture(tmp_path)
    (core.parent / filename).write_text("module collision; endmodule")
    wrapper["core_source_manifest"] = manifest(core)
    with pytest.raises(ValueError, match="colliding RTL"):
        mod.stage_rtl(board, wrapper)
    assert not (board / "rtl").exists()


def test_wrapper_only_change_retains_core_hashes_and_separates_invalidation(tmp_path):
    mod, core, board, wrapper = fixture(tmp_path)
    first = mod.stage_rtl(board, wrapper)
    core_time = (board / "rtl/top.v").stat().st_mtime_ns
    (board / "board_top.v").write_text("module board_top; wire changed; endmodule")
    second = mod.stage_rtl(board, wrapper)
    delta = mod.dependency_delta(first, second)
    assert delta["staged_core_content_unchanged"] is True
    assert "core_simulation" not in delta["affected_stages"]
    assert "synth" in delta["affected_stages"]
    assert (board / "rtl/top.v").stat().st_mtime_ns == core_time
    (core.parent / "weights.mem").write_text("0003")
    wrapper["core_source_manifest"] = manifest(core)
    third = mod.stage_rtl(board, wrapper)
    assert "core_simulation" in mod.dependency_delta(second, third)["affected_stages"]
    assert mod.dependency_delta({}, third)["staged_core_content_unchanged"] is None


def test_legacy_record_is_explicitly_unrecorded(tmp_path):
    mod, core, board, wrapper = fixture(tmp_path)
    wrapper.pop("core_source_manifest")
    result = mod.stage_rtl(board, wrapper)
    assert result["core_manifest_verification"] == "UNRECORDED_LEGACY"


def test_host_generator_records_complete_core_bundle(tmp_path):
    import sys

    from test_s15_numeric_policy import ROOT

    sys.path.insert(0, str(ROOT / "board-host-io/tests"))
    from test_board_host_io import MLP_WRAPPER

    gen = load("board-host-io/scripts/make_host_board.py", "dependency_host_gen")
    record = gen.generate(MLP_WRAPPER, tmp_path / "host")
    from pathlib import Path

    core = Path(record["core_verilog"])
    mod = load("vivado-manager/scripts/stage_rtl.py", "host_core_manifest_check")
    assert record["core_source_manifest"] == mod.core_sources(core)
    assert record["core_manifest_scope"] == "source files observed at host generation"
