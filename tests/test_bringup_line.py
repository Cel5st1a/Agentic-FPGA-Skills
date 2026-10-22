"""CLI behavior: default work, selected skill tree and two-file board identity."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LINE = ROOT / "scripts/line"


def fixture_contract(tmp_path, board="NexysVideo"):
    pkg = tmp_path / "source/model"
    pkg.mkdir(parents=True)
    contract = {"board_profile": "board.yaml", "precision": "ap_fixed<16,6>",
                "clock_period_ns": 10, "model": {"frontend": "keras_json_weights", "path": "model.json"},
                "verification": {"inputs": "inputs.json", "samples": 1}}
    deployment = pkg/"deployment.yaml"
    deployment.write_text(yaml.safe_dump(contract))
    part = "xc7a200tsbg484-1" if board == "NexysVideo" else "xc7a200tfbg484-2"
    (pkg/"board.yaml").write_text(yaml.safe_dump({"id": board, "part": part}))
    (pkg/"model.json").write_text("{}")
    (pkg/"inputs.json").write_text("[[0.0]]")
    return deployment


def invoke(script, *args, env=None):
    return subprocess.run([sys.executable, "-B", str(script), *map(str, args)],
                          env=env, capture_output=True, text=True)


def skills_copy(tmp_path):
    skills = tmp_path / "frozen skills"
    shutil.copytree(ROOT/"board-profiles", skills/"board-profiles")
    return skills


def test_default_line_dry_run_never_selects_cosim(tmp_path):
    deployment = fixture_contract(tmp_path)
    skills = skills_copy(tmp_path)
    result = invoke(LINE/"run_line.py", "--deployment", deployment, "--run", tmp_path/"run",
                    "--skills", skills, "--python", sys.executable, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "[dry-run] cosim:" not in result.stdout
    assert "[dry-run] boardsim:" in result.stdout
    assert not (tmp_path/"run").exists()


def test_bootstrap_records_selected_skills_in_all_commands_and_board_digest(tmp_path):
    deployment = fixture_contract(tmp_path)
    skills = skills_copy(tmp_path)
    result = invoke(LINE/"bootstrap_run.py", "--deployment", deployment, "--run", "one",
                    "--root", tmp_path/"runs", "--skills", skills)
    assert result.returncode == 0, result.stderr
    run = tmp_path/"runs/model/one"
    state = json.loads((run/"starting-state.json").read_text(encoding="utf-8"))
    assert state["skills_root"] == str(skills.resolve())
    assert state["board_profile_sha256"] == hashlib.sha256((deployment.parent/"board.yaml").read_bytes()).hexdigest()
    assert state["board_pins_profile_sha256"] == hashlib.sha256((skills/"board-profiles/NexysVideo/board_profile.yaml").read_bytes()).hexdigest()
    instructions = (run/"RUN.md").read_text(encoding="utf-8")
    assert str(ROOT) not in instructions
    for relative in ("hls4ml-convert/scripts/convert_keras.py", "scripts/line/run_line.py",
                     "fpga-env-audit/scripts/audit_system.sh"):
        assert str(skills/relative) in instructions or (skills/relative).as_posix() in instructions


def test_bootstrap_default_is_its_own_tree_and_board_hash_distinguishes_same_contract(tmp_path):
    frozen = skills_copy(tmp_path)
    shutil.copytree(LINE, frozen/"scripts/line", ignore=shutil.ignore_patterns("__pycache__"))
    states = []
    for board in ("NexysVideo", "AX7A200B"):
        deployment = fixture_contract(tmp_path/board, board)
        result = invoke(frozen/"scripts/line/bootstrap_run.py", "--deployment", deployment,
                        "--run", board, "--root", tmp_path/"runs")
        assert result.returncode == 0, result.stderr
        states.append(json.loads((tmp_path/"runs/model"/board/"starting-state.json").read_text(encoding="utf-8")))
    assert states[0]["contract_sha256"] == states[1]["contract_sha256"]
    assert states[0]["board_profile_sha256"] != states[1]["board_profile_sha256"]
    assert states[0]["skills_root"] == str(frozen)


def test_executed_stage_records_board_hash_in_plan_and_evidence(tmp_path):
    deployment = fixture_contract(tmp_path)
    skills = skills_copy(tmp_path)
    script = skills/"vitis-build-agent/scripts/run_stage.py"
    script.parent.mkdir(parents=True)
    script.write_text('import json,sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("--evidence")+1])\np.write_text(json.dumps({"status":"PASSED"}))\n')
    run = tmp_path/"run"
    env = {**os.environ, "TEMP": str(tmp_path)}
    result = invoke(LINE/"run_line.py", "--deployment", deployment, "--run", run,
                    "--skills", skills, "--python", sys.executable, "--stages", "csynth", env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    board_digest = hashlib.sha256((deployment.parent/"board.yaml").read_bytes()).hexdigest()
    pins_digest = hashlib.sha256((skills/"board-profiles/NexysVideo/board_profile.yaml").read_bytes()).hexdigest()
    for p in (run/"run-plan.json", run/"evidence/csynth-evidence.json"):
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["board_profile_sha256"] == board_digest
        assert payload["board_pins_profile_sha256"] == pins_digest


def test_explicit_cosim_uses_short_diagnostic_budget(tmp_path):
    deployment = fixture_contract(tmp_path)
    skills = skills_copy(tmp_path)
    result = invoke(LINE/"run_line.py", "--deployment", deployment, "--run", tmp_path/"run",
                    "--skills", skills, "--python", sys.executable, "--stages", "cosim", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "[dry-run] cosim:" in result.stdout and "--timeout 300" in result.stdout


def test_standalone_hls_failure_evidence_carries_board_identity(tmp_path):
    deployment = fixture_contract(tmp_path)
    project = tmp_path/"project"
    project.mkdir()
    (project/"build_prj.tcl").write_text("fixture")
    (project/"project.tcl").write_text('set project_name "myproject"')
    tool = tmp_path/"vitis.cmd"
    tool.write_text("@exit /b 1\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n")
    work = tmp_path/"work"
    result = invoke(ROOT/"vitis-build-agent/scripts/run_stage.py", "--deployment", deployment,
                    "--stage", "csynth", "--project", project, "--work", work, "--vitis-run", tool)
    assert result.returncode == 0, result.stderr
    payload = json.loads((work/"evidence.json").read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED"
    assert payload["board_profile_sha256"] == hashlib.sha256((deployment.parent/"board.yaml").read_bytes()).hexdigest()


def test_line_refuses_changed_board_in_pinned_starting_state(tmp_path):
    deployment = fixture_contract(tmp_path)
    skills = skills_copy(tmp_path)
    run = tmp_path/"run"
    run.mkdir()
    (run/"starting-state.json").write_text(json.dumps({
        "contract_sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
        "board_profile_sha256": "0"*64}))
    result = invoke(LINE/"run_line.py", "--deployment", deployment, "--run", run,
                    "--skills", skills, "--python", sys.executable, "--stages", "csynth")
    assert result.returncode != 0
    assert "board_profile_sha256" in result.stderr
    assert not (run/"run-plan.json").exists()
