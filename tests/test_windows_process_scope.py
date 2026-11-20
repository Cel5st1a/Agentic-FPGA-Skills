import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects required")
LINE = Path(__file__).parents[1] / "scripts/line"
sys.path.insert(0, str(LINE))


def wait_for(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


@pytest.fixture
def worker_script(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1] == "grandchild":
    Path("active.json").write_text(json.dumps([os.getppid(), os.getpid()]))
    time.sleep(60)
else:
    subprocess.Popen(
        [sys.executable, __file__, "grandchild"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    while not Path("active.json").exists():
        time.sleep(0.02)
    if sys.argv[1] == "wait":
        time.sleep(60)
""",
        encoding="utf-8",
    )
    return script


def test_normal_completion_drains_detached_descendants(tmp_path, worker_script):
    from process_scope import PROCESS_PROOF, run_owned, scope_drained

    with (tmp_path / "console.log").open("wb") as console:
        assert (
            run_owned([sys.executable, worker_script, "return"], cwd=tmp_path, console=console) == 0
        )

    proof = json.loads((tmp_path / PROCESS_PROOF).read_text())
    assert proof["state"] == "closed"
    assert proof["active_processes"] == 0
    assert proof["terminated_descendants"] is True
    assert proof["dispatcher"]["created_filetime"] > 0
    assert scope_drained(proof["job_name"], tmp_path / PROCESS_PROOF)


def test_killed_owner_blocks_restart_until_its_job_is_drained(tmp_path, worker_script, monkeypatch):
    import run_line
    from process_scope import PROCESS_PROOF, process_identity, scope_drained

    owner_code = """
import sys
from pathlib import Path
import run_line
from process_scope import run_owned

root = Path(sys.argv[1])
run_line.LOCK_PATH = root / "test.eda.lock"
with run_line.Lock(False) as lock, (root / "console.log").open("wb") as console:
    run_owned(
        [sys.executable, root / "worker.py", "wait"],
        cwd=root, console=console, bind=lock.bind_process_scope,
    )
"""
    env = {**os.environ, "PYTHONPATH": str(LINE), "PYTHONDONTWRITEBYTECODE": "1"}
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(tmp_path)],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        ready = tmp_path / "active.json"
        wait_for(lambda: ready.exists() and ready.stat().st_size > 0)
        pids = json.loads(ready.read_text())
        processes = [process_identity(pid) for pid in pids]
        assert all(processes)
        proof_path = tmp_path / PROCESS_PROOF
        proof = json.loads(proof_path.read_text())
        assert proof["state"] == "active"
        lock_path = tmp_path / "test.eda.lock"
        monkeypatch.setattr(run_line, "LOCK_PATH", lock_path)
        # Simulate the old owner's PID already having been recycled while its job
        # still has live members, without interfering with the cleanup observer.
        holder = json.loads(lock_path.read_text())
        holder["created_filetime"] -= 1
        lock_path.write_text(json.dumps(holder))
        assert not scope_drained(proof["job_name"], proof_path)
        with pytest.raises(SystemExit, match="process scope"):
            with run_line.Lock(True):
                pytest.fail("stale active process scope was reclaimed")

        owner.kill()
        owner.wait(timeout=10)
        wait_for(lambda: scope_drained(proof["job_name"], proof_path))
        assert all(process_identity(p["pid"]) != p for p in processes)
        with run_line.Lock(False):
            pass
        recovered = json.loads(proof_path.read_text())
        assert recovered["state"] == "closed"
        assert recovered["active_processes"] == 0
        assert recovered["closed_by"] == "stale_recovery"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def test_batch_proves_owned_dispatch_and_session_with_stub(tmp_path, monkeypatch):
    from odatix_batch import run_batch
    from process_scope import PROCESS_PROOF
    from run_line import Lock

    odatix_stub = tmp_path / "odatix"
    odatix_stub.mkdir()
    (odatix_stub / "__init__.py").touch()
    (odatix_stub / "workspace.py").write_text(
        "from pathlib import Path\nWorkspace = type('Workspace', (), {'open': Path.cwd})\n",
        encoding="utf-8",
    )
    (odatix_stub / "run.py").write_text(
        """
import shutil
import subprocess
import sys
from pathlib import Path

class Run:
    def __init__(self, workspace, mode, nb_jobs):
        self.workspace_root = str(workspace)
        self.session = self

    def start(self, *, detach, session):
        self.name = session
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        self.id = f"{self.pid}.{session}"

    def wait(self):
        job = Path("work/workflows/candidates/job0001")
        shutil.copytree("sources", job)
        subprocess.run(
            [sys.executable, "odatix_batch.py", "--worker", "job0001"], cwd=job, check=True,
        )
        (job / "workflow_done").touch()
        return [{"id": 0, "status": "success"}]

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        return True
""",
        encoding="utf-8",
    )
    payload = tmp_path / "inputs"
    payload.mkdir()
    (payload / "compute.py").write_text(
        "from pathlib import Path\nPath('out.json').write_text('{\"complete\": true}')\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "batch"
    monkeypatch.setattr("run_line.LOCK_PATH", tmp_path / "test.eda.lock")
    with Lock(False) as lock:
        report = run_batch(
            {
                "entrypoint": "compute.py",
                "expected_outputs": ["out.json"],
                "candidates": [{"id": "test", "args": []}],
            },
            payload,
            workspace,
            Path(sys.executable).with_name("odatix.exe"),
            env={**lock.child_environment(), "PYTHONPATH": str(tmp_path)},
            lock=lock,
        )
    assert report["status"] == "success"
    job = json.loads((workspace / PROCESS_PROOF).read_text())
    session = json.loads((workspace / report["odatix_session"]).read_text())
    assert session["job_name"] == job["job_name"]
    assert session["daemon"]["created_filetime"] > 0
    assert job["state"] == session["state"] == "closed"
    assert job["active_processes"] == 0
