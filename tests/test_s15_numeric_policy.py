"""Software-only regressions; fake tools never establish hardware success."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    path = ROOT / relative
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "stage,used,timing,exit_code,expected",
    [
        ("synth", 94.8, None, 0, "PASSED"),
        ("impl", 40, None, 0, "PASSED"),
        ("impl", 40, "negative", 0, "PASSED"),
        ("synth", 101, None, 0, "FAILED"),
        ("impl", 40, None, 1, "FAILED"),
    ],
)
def test_diagnostics_do_not_replace_tool_result(
    tmp_path, monkeypatch, stage, used, timing, exit_code, expected
):
    mod = load("vivado-manager/scripts/run_vivado.py", "policy_vivado")
    board = tmp_path / "board"
    (board / "rtl").mkdir(parents=True)
    (board / "wrapper.json").write_text(json.dumps({"part": "test-part", "top": "test"}))
    (board / "synth.dcp").write_bytes(b"software fixture")
    rpt = "utilization_synth.rpt" if stage == "synth" else "utilization_routed.rpt"
    (board / rpt).write_text(f"| Site Type | Used | Available |\n| CLB LUTs | {used} | 100 |\n")
    if timing:
        (board / "timing_routed.rpt").write_text(
            "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n-0.12 -1.00 0.10 0.00\n"
        )
    contract = tmp_path / "deployment.yaml"
    contract.write_text(yaml.safe_dump({"constraints": {"resource_fraction_max": 0.8}}))
    monkeypatch.setattr(mod, "identity_fields", lambda *args: {})
    monkeypatch.setattr(mod, "stage_rtl", lambda *args: {"files": {}, "ip_scripts": []})
    monkeypatch.setattr(
        mod,
        "run_vivado",
        lambda *args: {
            "argv": ["software-test"],
            "returncode": exit_code,
            "timed_out": False,
            "duration_s": 0,
            "stdout": "ERROR: Route 35-3" if exit_code else "",
        },
    )
    evidence = tmp_path / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vivado",
            "--stage",
            stage,
            "--board",
            str(board),
            "--deployment",
            str(contract),
            "--work",
            str(tmp_path / "work"),
            "--evidence",
            str(evidence),
        ],
    )
    mod.main()
    record = json.loads(evidence.read_text())
    assert record["status"] == expected
    assert record.get("hardware_verified") is False
    if used == 94.8:
        assert record["resource_gate"]["passed"] is False
        assert record["resource_gate"]["diagnostic_only"] is True
    if timing:
        assert record["timing"]["met"] is False


def test_board_python_success_is_not_rejected_by_hls_diagnostic(tmp_path):
    mod = load("board-host-io/scripts/verify_board.py", "policy_board")
    contract = tmp_path / "deployment.yaml"
    contract.write_text(
        yaml.safe_dump(
            {"precision": "ap_fixed<24,6>", "verification": {"samples": 256, "rtol": 0.001}}
        )
    )
    actual = [[0.5] for _ in range(256)]
    record = mod.evaluate_capture(contract, actual, [[1.0]] * 256, actual)
    assert record["python"]["passed"] is True
    assert record["hls"]["passed"] is False
    assert record["status"] == "HARDWARE_NUMERICAL_PASSED"


def test_incomplete_board_values_remain_failure(tmp_path):
    mod = load("board-host-io/scripts/verify_board.py", "policy_board_missing")
    contract = tmp_path / "deployment.yaml"
    contract.write_text(
        yaml.safe_dump({"precision": "ap_fixed<24,6>", "verification": {"samples": 256}})
    )
    record = mod.evaluate_capture(contract, [[0.5]] * 256, [[0.5]] * 256, [[0.5]] * 255)
    assert record["status"] == "FAILED"


def test_board_rule_is_original_python_tolerance_without_precision(tmp_path):
    mod = load("board-host-io/scripts/verify_board.py", "policy_board_rule")
    contract = tmp_path / "deployment.yaml"
    contract.write_text("{}")
    # A historical rtol=.01 or coarse LSB rule would wrongly pass these values.
    record = mod.evaluate_capture(contract, [[1.0]] * 256, None, [[1.003]] * 256)
    assert record["status"] == "FAILED"
    assert record["python"]["criterion"]["atol"] == 0.001
    assert record["python"]["criterion"]["rtol"] == 0.001
    assert record["hls"]["status"] == "NOT_RUN"


@pytest.mark.parametrize("template", ["run_impl.tcl", "write_bitstream.tcl"])
@pytest.mark.parametrize("paths", ["", "timing_path"])
def test_tcl_timing_is_diagnostic_but_vendor_failures_propagate(tmp_path, template, paths):
    import subprocess

    mod = load("vivado-manager/scripts/run_vivado.py", "policy_render")
    rendered = mod.render(
        ROOT / "vivado-manager/templates" / template,
        {"THREADS": 1, "BITSTREAM": (tmp_path / "test.bit").as_posix()},
    )
    script = tmp_path / template
    script.write_text(rendered)
    command = "route_design" if template == "run_impl.tcl" else "write_bitstream"
    # A separate Tcl process avoids binding Tcl stdout to pytest capture handles.
    probe = """import json,sys,tkinter
d=json.load(sys.stdin)
t=tkinter.Tcl()
t.eval('proc unknown {args} {return ""}; proc exit {} {}; proc puts {args} {}')
t.eval('proc get_timing_paths {args} {return {'+d['paths']+'}}')
t.eval('proc get_property {args} {return -0.12}')
t.eval('proc get_drc_violations {args} {return ""}; proc cd {args} {}')
t.eval(d['script'])
t.eval('proc '+d['command']+' {args} {error "VENDOR_FAILURE"}')
try:
    t.eval(d['script'])
except tkinter.TclError as exc:
    assert 'VENDOR_FAILURE' in str(exc)
else:
    raise AssertionError('vendor failure was suppressed')
"""
    result = subprocess.run(
        ["D:/python_envs/hls4ml_conv/Scripts/python.exe", "-c", probe],
        input=json.dumps({"script": rendered, "paths": paths, "command": command}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
