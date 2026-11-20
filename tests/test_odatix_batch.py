import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts" / "line"))
from odatix_batch import collect_results, plan_candidates, snapshot_digest


def test_same_arguments_alias_and_distinct_arguments_run(tmp_path):
    (tmp_path / "board.xdc").write_text("create_clock -period 5")
    digest = snapshot_digest(tmp_path)
    cases = [
        {"id": "nominal", "args": ["200"]},
        {"id": "lower", "args": ["180"]},
        {"id": "repeat", "args": ["200"]},
    ]
    plan = plan_candidates(cases, digest, "Vivado 2026.1 SW Build 6511674", "build.py")
    assert [job["token"] for job in plan] == ["job0001", "job0002", "job0001"]
    assert plan[2]["canonical_id"] == "nominal"
    assert plan[0]["key"] == plan[2]["key"]
    (tmp_path / "board.xdc").write_text("create_clock -period 6")
    changed = plan_candidates(
        cases, snapshot_digest(tmp_path), "Vivado 2026.1 SW Build 6511674", "build.py"
    )
    assert changed[0]["key"] != plan[0]["key"]


def test_unknown_tool_identity_keeps_each_request(tmp_path):
    plan = plan_candidates(
        [{"id": "first", "args": []}, {"id": "second", "args": []}],
        snapshot_digest(tmp_path),
        "unknown",
        "build.py",
    )
    assert [job["token"] for job in plan] == ["job0001", "job0002"]


def test_duplicate_request_id_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        plan_candidates([{"id": "a", "args": []}] * 2, "digest", "tool", "build.py")


def test_aggregate_does_not_inherit_cli_success_or_alias_failure(tmp_path):
    plan = plan_candidates(
        [{"id": "a", "args": []}, {"id": "b", "args": ["different"]}, {"id": "c", "args": []}],
        "digest",
        "tool",
        "build.py",
    )
    a = tmp_path / "job0001"
    b = tmp_path / "job0002"
    a.mkdir()
    b.mkdir()
    (a / "candidate-result.json").write_text(
        json.dumps({**plan[0], "status": "failed", "returncode": 7})
    )
    (b / "candidate-result.json").write_text(
        json.dumps({**plan[1], "status": "success", "returncode": 0})
    )
    (b / "workflow_done").touch()
    results = collect_results(plan, tmp_path)
    assert [row["status"] for row in results] == ["failed", "success", "failed"]
    assert results[2]["canonical_id"] == "a"


def test_candidate_needs_both_worker_and_odatix_success(tmp_path):
    plan = plan_candidates([{"id": "a", "args": []}], "digest", "tool", "build.py")
    job = tmp_path / "job0001"
    job.mkdir()
    (job / "candidate-result.json").write_text(json.dumps({"status": "success", "returncode": 0}))
    assert collect_results(plan, tmp_path)[0]["status"] == "incomplete"


def test_unbound_worker_result_cannot_override_plan(tmp_path):
    plan = plan_candidates([{"id": "a", "args": []}], "digest", "tool", "build.py")
    job = tmp_path / "job0001"
    job.mkdir()
    (job / "candidate-result.json").write_text(
        json.dumps({"id": "wrong", "status": "success", "returncode": 0})
    )
    (job / "workflow_done").touch()
    [row] = collect_results(plan, tmp_path)
    assert row["id"] == "a"
    assert row["status"] == "incomplete"


def test_bad_json_does_not_hide_other_candidates(tmp_path):
    plan = plan_candidates([{"id": "a", "args": []}], "digest", "tool", "build.py")
    job = tmp_path / "job0001"
    job.mkdir()
    (job / "candidate-result.json").write_text("{bad")
    assert collect_results(plan, tmp_path)[0]["status"] == "incomplete"


def test_host_build_uses_existing_command_and_preserves_batch_failure(tmp_path, monkeypatch):
    import importlib.util

    import odatix_batch

    source = Path(__file__).parents[1] / "board-host-io/scripts/build_host.py"
    spec = importlib.util.spec_from_file_location("host_build_odatix_test", source)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    interpreter = Path(sys.executable)
    deployment = tmp_path / "mlp/contract/model/deployment.yaml"
    board = tmp_path / "mlp/p3-build/board"
    batch_dir = tmp_path / "batch"

    def fake_batch(request, payload, output, odatix):
        assert output == batch_dir
        assert request["candidates"] == [{"id": "host-build", "args": [], "independent": True}]
        assert not request.get("tool_identity")
        argv = json.loads((payload / "command.json").read_text())
        assert argv == [
            str(interpreter.resolve()),
            str(source.resolve()),
            "--deployment",
            str(deployment),
            "--board",
            str(board),
            "--python",
            str(interpreter.resolve()),
            "--start-stage",
            "synth",
        ]
        return {"status": "failed", "candidates": []}

    monkeypatch.setattr(odatix_batch, "run_batch", fake_batch)
    result = host.build_with_odatix(deployment, board, interpreter, Path("odatix.exe"), batch_dir)
    assert result["status"] == "failed"


def test_candidate_worker_detects_changed_arguments_before_subprocess(tmp_path, monkeypatch):
    from odatix_batch import run_candidate

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "build.py").write_text("raise SystemExit('unexpected execution')")
    digest = snapshot_digest(payload)
    [plan] = plan_candidates([{"id": "a", "args": ["original"]}], digest, "tool", "build.py")
    case = {
        "entrypoint": "build.py",
        "args": ["changed"],
        "expected": ["output.json"],
        "payload_digest": digest,
        "key": plan["key"],
        "tool_identity": "tool",
        "discriminator": None,
    }
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "job0001.json").write_text(json.dumps(case))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="candidate identity"):
        run_candidate("job0001")
    assert not (tmp_path / "candidate-console.log").exists()


def test_workflow_default_requires_explicit_opt_in_and_preserves_inner_direct(
    tmp_path, monkeypatch
):
    import importlib.util

    source = Path(__file__).parents[1] / "board-host-io/scripts/build_host.py"
    spec = importlib.util.spec_from_file_location("host_build_default_test", source)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    board = tmp_path / "board"
    monkeypatch.delenv("S15_ODATIX_EXE", raising=False)
    monkeypatch.delenv("S15_ODATIX_WORKER", raising=False)
    assert host.workflow_default(board) == (None, None)
    monkeypatch.setenv("S15_ODATIX_EXE", "D:/tools/odatix.exe")
    tool, work = host.workflow_default(board)
    assert tool == Path("D:/tools/odatix.exe") and work.parent == board
    assert work != host.workflow_default(board)[1]
    monkeypatch.setenv("S15_ODATIX_WORKER", "1")
    assert host.workflow_default(board) == (None, None)


def test_dispatch_waits_for_its_jobs_and_closes_the_owned_session(monkeypatch, tmp_path):
    from odatix_batch import dispatch

    calls = []

    class Run:
        def __init__(self, workspace, mode, **options):
            assert workspace == tmp_path and mode == "workflow"
            assert options["nb_jobs"] == 1
            self.session = types.SimpleNamespace(stop=self.stop)

        def start(self, *, detach):
            assert detach is True
            calls.append("start")

        def wait(self):
            calls.append("wait")
            return [{"status": "done"}, {"status": "failed"}]

        def stop(self):
            calls.append("stop")
            return True

    monkeypatch.setitem(sys.modules, "odatix.run", types.SimpleNamespace(Run=Run))
    monkeypatch.setitem(
        sys.modules,
        "odatix.workspace",
        types.SimpleNamespace(Workspace=types.SimpleNamespace(open=lambda: tmp_path)),
    )
    assert dispatch(2) == 0
    assert calls == ["start", "wait", "stop"]
    with pytest.raises(RuntimeError, match="expected 3"):
        dispatch(3)
    assert calls[-3:] == ["start", "wait", "stop"]
