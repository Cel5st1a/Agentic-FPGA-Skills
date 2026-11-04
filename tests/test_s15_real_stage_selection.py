"""Execute the installed template's actual option block in a real Tcl interpreter."""

import json
import sys
import tkinter
from pathlib import Path

import pytest
from test_s15_numeric_policy import load

TEMPLATE = Path(
    "D:/python_envs/hls4ml_conv/Lib/site-packages/hls4ml/templates/vivado/build_prj.tcl"
)


@pytest.mark.parametrize("stage", ["csim", "csynth"])
def test_real_template_consumes_requested_stage_options(tmp_path, stage):
    mod = load("vitis-build-agent/scripts/run_stage.py", "real_stage_selection")
    # This is the installed template's real default option block, not a fake reader
    # that assumes build_opt.tcl is consumed. No vendor commands are executed.
    source = TEMPLATE.read_text().split("set tcldir", 1)[0]
    project_script = tmp_path / "build_prj.tcl"
    project_script.write_text(source + "\nlist $opt(csim) $opt(synth) $opt(cosim)\n")
    options = mod.write_options(tmp_path, mod.STAGE_FLAGS[stage])
    actual = tkinter.Tcl().call("source", project_script.as_posix())
    assert tuple(map(int, actual)) == (options["csim"], options["synth"], 0)
    first = project_script.read_bytes()
    mod.write_options(tmp_path, mod.STAGE_FLAGS[stage])
    assert project_script.read_bytes() == first


def test_unrecognized_stage_binding_does_not_launch_unrequested_vendor_work(tmp_path, monkeypatch):
    mod = load("vitis-build-agent/scripts/run_stage.py", "unknown_stage_binding")
    project = tmp_path / "project"
    project.mkdir()
    (project / "build_prj.tcl").write_text('puts "unknown stage selection"')
    (project / "project.tcl").write_text('set project_name "myproject"')
    work = tmp_path / "attempt"
    calls = []

    def tool(*args):
        calls.append(args)
        return {
            "argv": [],
            "returncode": 0,
            "timed_out": False,
            "duration_s": 0,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(mod, "run_tool", tool)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
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
    mod.main()
    assert not calls
    evidence = json.loads((work / "evidence.json").read_text())
    assert evidence["status"] == "FAILED" and evidence["tool"]["launched"] is False
    assert evidence["tool"]["returncode"] is None and evidence["tool"]["duration_s"] is None
