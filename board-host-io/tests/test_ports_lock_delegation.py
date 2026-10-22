"""D-N08: explicit acquisition identity, never OS ancestry, authorizes ports."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/line'))
import run_line as line


@pytest.fixture
def audit(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('dn08_audit', ROOT / 'board-host-io/scripts/audit_ports.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'LOCK_PATH', tmp_path / 'eda.lock')
    monkeypatch.setattr(line, 'LOCK_PATH', module.LOCK_PATH)
    monkeypatch.delenv('AFD_LINE_LOCK_PID', raising=False)
    monkeypatch.delenv('AFD_LINE_LOCK_TOKEN', raising=False)
    return module


def delegate(audit, monkeypatch):
    token = 'a' * 64
    record = {'pid': os.getpid(), 'delegation_sha256': hashlib.sha256(token.encode()).hexdigest()}
    audit.LOCK_PATH.write_text(json.dumps(record), encoding='utf-8')
    monkeypatch.setenv('AFD_LINE_LOCK_PID', str(os.getpid()))
    monkeypatch.setenv('AFD_LINE_LOCK_TOKEN', token)
    return record


def test_valid_delegation_ignores_unreliable_ppid(audit, monkeypatch):
    delegate(audit, monkeypatch)
    monkeypatch.setattr(os, 'getppid', lambda: 987654321)
    audit.require_parent_lock()


@pytest.mark.parametrize('fault', ['missing_token', 'wrong_token', 'wrong_pid', 'dead_holder', 'old_record', 'malformed', 'invalid_digest'])
def test_ancestry_cannot_authorize_invalid_delegation(audit, monkeypatch, fault):
    record = delegate(audit, monkeypatch)
    monkeypatch.setattr(os, 'getppid', os.getpid)
    if fault == 'missing_token':
        monkeypatch.delenv('AFD_LINE_LOCK_TOKEN')
    elif fault == 'wrong_token':
        monkeypatch.setenv('AFD_LINE_LOCK_TOKEN', 'b' * 64)
    elif fault == 'wrong_pid':
        monkeypatch.setenv('AFD_LINE_LOCK_PID', '123456789')
    elif fault == 'dead_holder':
        monkeypatch.setattr(line, '_pid_alive', lambda pid: False)
    elif fault == 'old_record':
        record.pop('delegation_sha256')
    elif fault == 'invalid_digest':
        record['delegation_sha256'] = '\u9519' * 64
    else:
        record = []
    audit.LOCK_PATH.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='EDA lock') as caught:
        audit.require_parent_lock()
    message = str(caught.value)
    assert 'holder_pid=' in message and 'child_ppid=' in message
    assert 'a' * 64 not in message and 'b' * 64 not in message


@pytest.mark.parametrize('delegated', [True, False])
def test_lock_rejection_leaves_no_output_and_retry_keeps_root_error(audit, tmp_path, delegated):
    checkpoint = tmp_path / 'routed.dcp'
    checkpoint.write_bytes(b'test checkpoint; Vivado must not start')
    wrapper = tmp_path / 'wrapper.json'
    wrapper.write_text('{}')
    if not delegated:
        audit.LOCK_PATH.write_text(json.dumps({'pid': os.getpid()}))
    output = tmp_path / 'audit'
    for _ in range(2):
        with pytest.raises((ValueError, SystemExit), match='EDA lock'):
            audit.audit_checkpoint(checkpoint, wrapper, output, Path('never-start-vivado'), under_line_lock=delegated)
        assert not output.exists()


@pytest.mark.parametrize('delegated', [True, False])
def test_cli_expected_error_is_named_without_traceback(audit, tmp_path, monkeypatch, capsys, delegated):
    checkpoint = tmp_path / 'routed.dcp'
    checkpoint.write_bytes(b'fixture')
    wrapper = tmp_path / 'wrapper.json'
    wrapper.write_text('{}')
    if not delegated:
        audit.LOCK_PATH.write_text(json.dumps({'pid': os.getpid()}))
    monkeypatch.setattr(sys, 'argv', ['audit_ports.py', '--routed-dcp', str(checkpoint),
        '--wrapper', str(wrapper), '--output', str(tmp_path / 'audit')] +
        (['--under-line-lock'] if delegated else []))
    assert audit.main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error['error'] == 'port_audit_failed'
    assert 'EDA lock' in error['message']
    if delegated:
        assert 'holder_pid=' in error['message'] and 'child_ppid=' in error['message']
    assert 'Traceback' not in error['message']


def test_acquisition_delegates_to_real_child_and_old_token_expires(audit, monkeypatch):
    probe = ('import sys,os; sys.path.insert(0,sys.argv[1]); import audit_ports as a; '
             'from pathlib import Path; a.LOCK_PATH=Path(sys.argv[2]); '
             'os.getppid=lambda:987654321; a.require_parent_lock()')
    with line.Lock(False) as held:
        env = held.child_environment()
        result = subprocess.run([sys.executable, '-B', '-c', probe,
            str(ROOT / 'board-host-io/scripts'), str(audit.LOCK_PATH)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        contender = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                     'import run_line as r; r.LOCK_PATH=Path(sys.argv[2]); '
                     'r.Lock(False).__enter__()')
        refused = subprocess.run([sys.executable, '-B', '-c', contender,
            str(ROOT / 'scripts/line'), str(audit.LOCK_PATH)], capture_output=True, text=True, timeout=10)
        assert refused.returncode != 0 and 'EDA lock' in refused.stderr
        with pytest.raises(SystemExit, match='EDA lock'):
            with line.Lock(False):
                pytest.fail('second holder acquired live lock')
    assert not audit.LOCK_PATH.exists()
    with line.Lock(False):
        for name in ('AFD_LINE_LOCK_PID', 'AFD_LINE_LOCK_TOKEN'):
            monkeypatch.setenv(name, env[name])
        with pytest.raises(ValueError, match='EDA lock'):
            audit.require_parent_lock()


def test_execute_forwards_delegation_through_audit_cli(audit, tmp_path):
    board = tmp_path / 'p3-build/board'
    board.mkdir(parents=True)
    (board / 'routed.dcp').write_bytes(b'fixture checkpoint for fake Vivado')
    (board / 'wrapper.json').write_text(json.dumps({'top_ports': ['led']}))
    # External Vivado is replaced by a tiny executable; audit, child process,
    # environment transfer and stage evidence validation all remain production code.
    vivado = tmp_path / ('vivado.cmd' if os.name == 'nt' else 'vivado')
    vivado.write_text('@echo HOST_IO_PORT^|led^|OUT^|A1^|LVCMOS33\n' if os.name == 'nt'
                      else '#!/bin/sh\necho "HOST_IO_PORT|led|OUT|A1|LVCMOS33"\n')
    if os.name != 'nt':
        vivado.chmod(0o755)
    probe = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
             'import audit_ports as a; a.LOCK_PATH=Path(sys.argv[2]); '
             'sys.argv=["audit_ports.py",*sys.argv[3:]]; raise SystemExit(a.main())')
    command = [sys.executable, '-B', '-c', probe, str(ROOT / 'board-host-io/scripts'),
        str(audit.LOCK_PATH), '--routed-dcp', str(board / 'routed.dcp'),
        '--wrapper', str(board / 'wrapper.json'), '--output', str(tmp_path / 'evidence/io-port-audit'),
        '--vivado', str(vivado), '--under-line-lock', '--allow-configure-only']
    results = []
    with line.Lock(False) as held:
        assert line._execute([('ports', lambda: command)], results, tmp_path, held) == 0
    assert results == [{'stage': 'ports', 'exit_code': 0, 'problems': []}]
    scope = json.loads((tmp_path / 'evidence/io-port-audit/io-scope.json').read_text())
    assert scope['wrapper_ports_match'] is True
    assert scope['on_board_numerical'] == 'NOT_RUN'
