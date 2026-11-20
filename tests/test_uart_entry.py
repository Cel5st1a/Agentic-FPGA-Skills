import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_bringup_line import ROOT, fixture_contract, skills_copy


def test_uart_entry_runs_frontend_build_program_and_capture_in_order(tmp_path):
    contract = fixture_contract(tmp_path)
    run = tmp_path / "new-run"
    skills = skills_copy(tmp_path)
    entry = Path(__file__).resolve().parents[1] / "scripts/line/run_uart.py"
    now = subprocess.run(
        [
            sys.executable,
            str(entry),
            "--deployment",
            str(contract),
            "--run",
            str(run),
            "--skills",
            str(skills),
            "--python",
            sys.executable,
            "--port",
            "COM17",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert now.returncode == 0, now.stderr
    scripts = [
        "run_line.py",
        "make_host_board.py",
        "build_host.py",
        "audit_ports.py",
        "program_jtag.py",
        "verify_board.py",
    ]
    offsets = [now.stdout.index(name) for name in scripts]
    assert offsets == sorted(offsets)
    assert "convert,csynth,wrapper" in now.stdout
    assert "COM17" in now.stdout
    assert not run.exists()


def test_uart_entry_retains_failed_stage_and_stops(tmp_path):
    run = tmp_path / "run"
    deployment = run / "contract/model/deployment.yaml"
    deployment.parent.mkdir(parents=True)
    deployment.write_bytes(fixture_contract(tmp_path).read_bytes())
    (run / "starting-state.json").write_text("{}", encoding="ascii")
    skills = skills_copy(tmp_path)
    stub = skills / "scripts/line/run_line.py"
    stub.parent.mkdir(parents=True)
    stub.write_text("print('frontend diagnostic')\nraise SystemExit(7)\n", encoding="ascii")
    entry = Path(__file__).resolve().parents[1] / "scripts/line/run_uart.py"
    result = subprocess.run(
        [
            sys.executable,
            str(entry),
            "--deployment",
            str(deployment),
            "--run",
            str(run),
            "--skills",
            str(skills),
            "--python",
            sys.executable,
            "--port",
            "COM17",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7, result.stderr
    logs = run / "logs/uart-deployment"
    assert sorted(p.name for p in logs.iterdir()) == ["frontend.log", "steps.jsonl"]
    assert (logs / "frontend.log").read_text().strip() == "frontend diagnostic"
    assert '"returncode": 7' in (logs / "steps.jsonl").read_text()


def test_scan_uart_has_separate_build_and_program_phases(tmp_path):
    contract = fixture_contract(tmp_path)
    run = tmp_path / "scan-run"
    skills = skills_copy(tmp_path)
    entry = Path(__file__).resolve().parents[1] / "scripts/line/run_uart.py"
    base = [
        sys.executable,
        str(entry),
        "--deployment",
        str(contract),
        "--run",
        str(run),
        "--skills",
        str(skills),
        "--python",
        sys.executable,
        "--dry-run",
    ]
    build = subprocess.run(
        [*base, "--reuse-hls", "--build-only"], capture_output=True, text=True, check=True
    )
    assert "--stages wrapper" in build.stdout
    assert "audit_ports.py" in build.stdout
    assert "program_jtag.py" not in build.stdout
    verify = subprocess.run(
        [*base, "--verify-only", "--port", "COM17"], capture_output=True, text=True, check=True
    )
    assert "program_jtag.py" in verify.stdout and "verify_board.py" in verify.stdout
    assert "build_host.py" not in verify.stdout


@pytest.mark.parametrize("capture_returncode", [0, 9])
def test_verified_entry_holds_program_lock_during_capture(
    tmp_path, monkeypatch, capture_returncode
):
    run = tmp_path / "run"
    deployment = run / "contract/model/deployment.yaml"
    deployment.parent.mkdir(parents=True)
    deployment.write_bytes(fixture_contract(tmp_path).read_bytes())
    (run / "starting-state.json").write_text("{}", encoding="ascii")
    skills = skills_copy(tmp_path)
    host = skills / "board-host-io/scripts"
    host.mkdir(parents=True)
    support = ROOT / "board-host-io/scripts"
    lock_dir = tmp_path / "process-locks"
    lock_dir.mkdir()
    for key in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(lock_dir))

    (host / "program_jtag.py").write_text(
        f"""from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

spec = spec_from_file_location("actual_program_jtag", {str(support / "program_jtag.py")!r})
module = module_from_spec(spec)
spec.loader.exec_module(module)
module.validate_inputs = lambda *a: {{
    'board': {{'pins_profile': 'fixture', 'jtag': {{'cable_serial': 'FIXTURE83',
                                                   'target_name': 'xc7a200t'}}}}
}}

def fake_tool(command, output, **kwargs):
    (output / 'xsdb.called').touch()
    return SimpleNamespace(returncode=0, stdout='FIXTURE83 xc7a200t\\n'
                           'AFD_AFTER FPGA is configured\\n', stderr='')

module.run_xsdb = fake_tool

def main(*args, **kwargs):
    return module.main(*args, **kwargs)

if __name__ == '__main__':
    raise SystemExit(main())
""",
        encoding="utf-8",
    )
    (host / "verify_board.py").write_text(
        f"""import json
import subprocess
import sys
from pathlib import Path

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    summary = Path(argv[argv.index('--program-summary') + 1])
    assert json.loads(summary.read_text())['programmed'] is True
    alt = summary.parent.with_name('competing-program')
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('program_jtag.py')),
        '--bitstream', str(summary.with_name('design.bit')), '--io-scope', str(summary),
        '--bitstream-evidence', str(summary), '--output', str(alt)],
        capture_output=True, text=True, check=False)
    print('second writer:', result.returncode, result.stdout)
    assert result.returncode == 1
    assert 'cable is busy' in json.loads((alt / 'summary.json').read_text())['error']
    assert not (alt / 'xsdb.called').exists()
    return {capture_returncode}

if __name__ == '__main__':
    raise SystemExit(main())
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/line/run_uart.py"),
            "--verify-only",
            "--deployment",
            str(deployment),
            "--run",
            str(run),
            "--skills",
            str(skills),
            "--python",
            sys.executable,
            "--port",
            "COM17",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    logs = run / "logs/uart-deployment-verify"
    steps = [json.loads(row) for row in (logs / "steps.jsonl").read_text().splitlines()]
    assert [step["stage"] for step in steps] == ["program", "capture"]
    assert [step["returncode"] for step in steps] == [0, capture_returncode], (
        result.stdout + result.stderr + (logs / "capture.log").read_text()
    )
    assert result.returncode == capture_returncode
    assert (run / "hardware-program/xsdb.called").is_file()
    assert "second writer: 1" in (logs / "capture.log").read_text()

    available = subprocess.run(
        [
            sys.executable,
            str(host / "program_jtag.py"),
            "--bitstream",
            str(run / "unused.bit"),
            "--io-scope",
            str(run / "unused.json"),
            "--bitstream-evidence",
            str(run / "unused.json"),
            "--output",
            str(run / "next-program"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert available.returncode == 0, available.stderr
    assert (run / "next-program/xsdb.called").is_file()
