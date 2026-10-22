"""Attempt evidence survives tool refusal, setup failure and user interruption."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "board-host-io/scripts"))
sys.path.insert(0, str(ROOT / "scripts/line"))
import program_jtag as program
import verify_board as verify
import run_line as line


def proof(monkeypatch):
    monkeypatch.setattr(program, "validate_inputs", lambda *a: {
        "board": {"pins_profile": "fixture", "jtag": {
            "cable_serial": "TEST123", "target_name": "xc7a200t"}}})


def good_log():
    return "TEST123 xc7a200t\nAFD_AFTER FPGA is configured\n" + "\n".join(
        f"{k}: {v}" for k, v in {"CRC ERROR": 0,
        "END OF STARTUP (EOS) STATUS": 1, "DONE PIN": 1,
        "IDCODE ERROR": 0, "BAD PACKET ERROR": 0}.items())


def test_program_works_while_eda_is_locked(monkeypatch, tmp_path):
    proof(monkeypatch)
    monkeypatch.setattr(line, "LOCK_PATH", tmp_path / "eda.lock")
    output = tmp_path / "attempt"
    def xsdb(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=good_log(), stderr="")
    monkeypatch.setattr(program, "run_xsdb", xsdb)
    with line.Lock(False):
        summary = program.program(tmp_path/"a.bit", tmp_path/"scope", tmp_path/"ev",
                                  output, None, Path("xsdb"))
    assert summary["programmed"] is True
    assert json.loads((output / "summary.json").read_text())["programmed"] is True


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_program_records_interruption_before_rethrow(monkeypatch, tmp_path, error):
    proof(monkeypatch)
    output = tmp_path / "attempt"
    def xsdb(*args, **kwargs):
        raise error("interrupted")
    monkeypatch.setattr(program, "run_xsdb", xsdb)
    with pytest.raises(error):
        program.program(tmp_path/"a.bit", tmp_path/"scope", tmp_path/"ev",
                        output, None, Path("xsdb"))
    summary = json.loads((output / "summary.json").read_text())
    assert summary["programmed"] is False
    assert "interrupted" in summary["error"]


def test_program_persists_failure_before_xsdb(monkeypatch, tmp_path):
    proof(monkeypatch)
    output = tmp_path / "attempt"
    def xsdb(*args, **kwargs):
        persisted = json.loads((output / "summary.json").read_text())
        assert persisted["programmed"] is False
        return SimpleNamespace(returncode=0, stdout=good_log(), stderr="")
    monkeypatch.setattr(program, "run_xsdb", xsdb)
    assert program.program(tmp_path/"a.bit", tmp_path/"scope", tmp_path/"ev",
                           output, None, Path("xsdb"))["programmed"] is True


def test_cable_lock_refusal_is_recorded_and_other_cable_is_independent(monkeypatch, tmp_path):
    import attempt_evidence
    proof(monkeypatch)
    monkeypatch.setattr(attempt_evidence, "LOCK_ROOT", tmp_path)
    output = tmp_path / "attempt"
    with attempt_evidence.CableLock("TEST123"):
        with attempt_evidence.CableLock("OTHER456"):
            result = program.program(tmp_path/"a.bit", tmp_path/"scope", tmp_path/"ev",
                                     output, None, Path("must-not-execute"))
        assert result["programmed"] is False
        assert "cable" in result["error"].lower()
        assert json.loads((output/"summary.json").read_text())["programmed"] is False
    with attempt_evidence.CableLock("TEST123"):
        pass


def test_existing_attempt_is_preserved_with_explicit_next_attempt_message(monkeypatch, tmp_path):
    proof(monkeypatch)
    output = tmp_path / "attempt"
    output.mkdir()
    (output/"events.jsonl").write_text("old evidence")
    with pytest.raises(ValueError, match="new.*attempt"):
        program.program(tmp_path/"a.bit", tmp_path/"scope", tmp_path/"ev",
                        output, None, Path("must-not-execute"))
    assert (output/"events.jsonl").read_text() == "old evidence"
    assert not (output/"summary.json").exists()


def capture_fixture(monkeypatch, tmp_path):
    from geometry_fixtures import synthetic_geometry
    import host_geometry
    run = tmp_path / "run"
    pkg = run / "contract/model"
    pkg.mkdir(parents=True)
    contract = {"precision": "ap_fixed<16,6>", "model": {"path": "model.bin", "input_shape": [1]},
                "verification": {"inputs": "inputs.json", "samples": 2}}
    deployment = pkg / "deployment.yaml"
    deployment.write_text(yaml.safe_dump(contract))
    (pkg/"inputs.json").write_text("[[0.0], [0.0]]")
    (pkg/"model.bin").write_bytes(b"model")
    board = run / "p3-build/host-board"
    board.mkdir(parents=True)
    wrapper = board / "wrapper.json"
    wrapper.write_text(json.dumps({"interface": {"style": "axis_stream_with_ap_ctrl_hs"},
                                   "clock": {"core_mhz": 100.0}}))
    bit = board / "board_top.bit"
    bit.write_bytes(b"bit")
    (run/"evidence").mkdir()
    (run/"evidence/python-output.json").write_text("[[0.0], [0.0]]")
    (run/"p3-build/csim").mkdir()
    (run/"p3-build/csim/csim_results.log").write_text("0.0\n0.0\n")
    scope, programmed = run/"scope.json", run/"program.json"
    scope.write_text("{}")
    programmed.write_text("{}")
    monkeypatch.setattr(verify, "preflight", lambda *a: {"run": run, "wrapper": wrapper})
    monkeypatch.setattr(verify, "require_selected_bridge", lambda *a: "TESTSERIAL")
    geometry = synthetic_geometry(host_geometry, contract, references=[[0.0]])
    monkeypatch.setattr(verify, "geometry_for_wrapper", lambda *a: geometry)
    return deployment, board, bit, scope, programmed, run/"capture"


@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
def test_capture_persists_failure_before_open_and_after_interrupt(monkeypatch, tmp_path, error):
    import serial
    deployment, board, bit, scope, programmed, output = capture_fixture(monkeypatch, tmp_path)
    observed = []
    class Port:
        def open(self):
            observed.append(json.loads((output/"summary.json").read_text()))
            raise error("port interrupted")
        def close(self):
            pass
    monkeypatch.setattr(serial, "Serial", Port)
    if error is KeyboardInterrupt:
        with pytest.raises(error):
            verify.capture(deployment, board, bit, scope, programmed,
                           "COM9", "TESTSERIAL", output)
    else:
        result = verify.capture(deployment, board, bit, scope, programmed,
                                "COM9", "TESTSERIAL", output)
        assert result["status"] == "FAILED"
    assert observed[0]["hardware_numerical_verified"] is False
    summary = json.loads((output/"summary.json").read_text())
    assert summary["status"] == "FAILED"
    assert summary["received_samples"] == 0
    assert "port interrupted" in summary["error"]


def test_xsdb_timeout_terminates_descendants_before_releasing_cable(tmp_path):
    import subprocess
    child_pid = tmp_path/"child.pid"
    child = "import time; time.sleep(60)"
    parent = ("import subprocess,sys,time; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
              "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    output = tmp_path/"tool"
    output.mkdir()
    with pytest.raises(subprocess.TimeoutExpired):
        program.run_xsdb([sys.executable, "-c", parent, str(child_pid), child], output, timeout=1)
    assert child_pid.exists()
    assert not line._pid_alive(int(child_pid.read_text()))


def test_cable_exclusion_and_release_after_process_is_killed(tmp_path):
    import subprocess
    import attempt_evidence
    probe = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
             "import attempt_evidence as a; a.LOCK_ROOT=Path(sys.argv[2]); "
             "lock=a.CableLock('TEST123'); lock.__enter__(); "
             "print('ready',flush=True); sys.stdin.read()")
    process = subprocess.Popen([sys.executable, "-B", "-c", probe,
        str(ROOT/"board-host-io/scripts"), str(tmp_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    old_root = attempt_evidence.LOCK_ROOT
    attempt_evidence.LOCK_ROOT = tmp_path
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="cable"):
            with attempt_evidence.CableLock("TEST123"):
                pytest.fail("another process owns this cable")
        with attempt_evidence.CableLock("OTHER456"):
            pass
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            process.terminate()
        process.wait(timeout=10)
        attempt_evidence.LOCK_ROOT = old_root
    attempt_evidence.LOCK_ROOT = tmp_path
    try:
        # Windows reports termination before the kernel finishes releasing byte locks.
        # Require automatic release within a bounded wait, without deleting the file.
        import time
        deadline = time.monotonic() + 2
        while True:
            try:
                with attempt_evidence.CableLock("TEST123"):
                    break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        attempt_evidence.LOCK_ROOT = old_root
