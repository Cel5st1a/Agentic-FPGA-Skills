import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/line"))

import run_line
from scan_hls import rank_cases


def test_delegation_rejects_physical_stages_before_checking_lock(monkeypatch):
    checked = []
    monkeypatch.setattr(run_line, "require_delegated_lock", checked.append)
    with pytest.raises(ValueError, match="HLS"):
        run_line.check_hls_delegation(["csynth", "impl"], False)
    with pytest.raises(ValueError, match="force"):
        run_line.check_hls_delegation(["csim"], True)
    assert checked == []
    run_line.check_hls_delegation(["convert", "csim", "csynth"], False)
    assert checked == [run_line.LOCK_PATH]


def test_ranking_excludes_failed_and_uses_measured_latency_and_resources():
    cases = [
        {"id": "fast-but-failed", "eligible": False},
        {"id": "b", "eligible": True, "worst_latency_cycles": 40, "peak_fraction": 0.5},
        {"id": "a", "eligible": True, "worst_latency_cycles": 40, "peak_fraction": 0.4},
        {"id": "c", "eligible": True, "worst_latency_cycles": 41, "peak_fraction": 0.1},
    ]
    assert rank_cases(cases, 2) == ["a", "b"]


def test_reuse_manifest_detects_a_changed_generated_rtl_file(tmp_path):
    from hls_artifacts import MANIFEST, describe_hls, verify_hls
    from odatix_batch import file_digest, write_json

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for filename in (
        "convert-evidence.json",
        "python-output.json",
        "csim-evidence.json",
    ):
        (evidence / filename).write_text("{}")
    csim = tmp_path / "p3-build/csim/csim_results.log"
    csim.parent.mkdir(parents=True)
    csim.write_text("1 2 3 4")
    xml = tmp_path / "p3-build/csynth/top.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text("<root/>")
    write_json(
        evidence / "csynth-evidence.json",
        {"artifacts": {"csynth_xml": {"file": xml.name, "sha256": file_digest(xml)}}},
    )
    verilog = tmp_path / "p2-convert/project/myproject_prj/solution1/syn/verilog/top.v"
    verilog.parent.mkdir(parents=True)
    verilog.write_text("module top; endmodule")
    write_json(tmp_path / MANIFEST, describe_hls(tmp_path))
    verify_hls(tmp_path)
    verilog.write_text("module different; endmodule")
    with pytest.raises(ValueError, match="differ from the scan manifest"):
        verify_hls(tmp_path)
