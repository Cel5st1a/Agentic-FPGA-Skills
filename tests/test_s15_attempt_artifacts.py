import os
import sys

import pytest
from test_s15_numeric_policy import load


def test_old_canonical_result_cannot_hide_a_current_result(tmp_path):
    mod = load("vitis-build-agent/scripts/run_stage.py", "s15_artifacts")
    project, work = tmp_path / "project", tmp_path / "work"
    old = project / "tb_data/csim_results.log"
    fresh = project / "solution/sim/csim_results.log"
    for path in (old, fresh):
        path.parent.mkdir(parents=True)
    old.write_text("OLD RESULT")
    fresh.write_text("CURRENT RESULT")
    os.utime(old, (100, 100))
    os.utime(fresh, (200, 200))
    work.mkdir()
    record = mod.collect(project, work, "csim_results.log", not_before=150)
    assert record["source_path"] == "solution/sim/csim_results.log"
    assert (work / "csim_results.log").read_text() == "CURRENT RESULT"
    assert mod.collect(project, work, "csim_results.log", not_before=300) is None


def test_existing_stage_attempt_is_not_overwritten(tmp_path, monkeypatch):
    mod = load("vitis-build-agent/scripts/run_stage.py", "s15_immutable_stage")
    work, project = tmp_path / "work", tmp_path / "project"
    work.mkdir()
    project.mkdir()
    (project / "build_prj.tcl").write_text("unused")
    old = work / "evidence.json"
    old.write_text("ORIGINAL ATTEMPT")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_stage",
            "--stage",
            "csim",
            "--project",
            str(project),
            "--work",
            str(work),
            "--vitis-run",
            sys.executable,
        ],
    )
    with pytest.raises(SystemExit, match="new.*attempt"):
        mod.main()
    assert old.read_text() == "ORIGINAL ATTEMPT"


@pytest.mark.parametrize('content', [None, 'broken nonnumeric HLS output'])
def test_missing_or_invalid_hls_is_diagnostic_during_capture_setup(tmp_path, monkeypatch, content):
    # Existing capture fixture stubs physical I/O; reaching Serial.open proves setup
    # does not require HLS outputs. No numeric/hardware success is claimed here.
    import serial

    sys.path.insert(
        0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "board-host-io/tests")
    )
    from test_attempt_persistence import capture_fixture, verify

    deployment, board, bit, scope, programmed, output = capture_fixture(monkeypatch, tmp_path)
    hls_path = board.parent / "csim/csim_results.log"
    if content is None:
        hls_path.unlink()
    else:
        hls_path.write_text(content)

    class Port:
        def open(self):
            raise OSError("software probe reached port open")

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", Port)
    record = verify.capture(deployment, board, bit, scope, programmed, "COM9", "TEST", output)
    assert record["hardware_numerical_verified"] is False
    assert record["error"] == "software probe reached port open"


@pytest.mark.parametrize('script', ['vitis-build-agent/scripts/run_stage.py',
                                    'vivado-manager/scripts/run_vivado.py'])
def test_external_evidence_is_never_overwritten(tmp_path, monkeypatch, script):
    mod = load(script, 's15_external_evidence')
    evidence = tmp_path / 'previous.json'
    evidence.write_text('PREVIOUS ATTEMPT')
    args = ['runner', '--work', str(tmp_path / 'fresh'), '--evidence', str(evidence)]
    if 'vitis' in script:
        args += ['--stage', 'csim', '--project', str(tmp_path / 'project')]
    else:
        args += ['--stage', 'synth', '--board', str(tmp_path / 'board'),
                 '--deployment', str(tmp_path / 'deployment.yaml')]
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(SystemExit, match='evidence.*exists'):
        mod.main()
    assert evidence.read_text() == 'PREVIOUS ATTEMPT'
