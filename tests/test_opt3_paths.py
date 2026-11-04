import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_s15_numeric_policy import load


def test_relative_audit_source_resolves_in_actual_child_cwd(tmp_path, monkeypatch):
    mod = load("board-host-io/scripts/audit_ports.py", "opt3_paths")
    monkeypatch.chdir(tmp_path)
    Path("routed.dcp").write_bytes(b"development fixture")
    Path("wrapper.json").write_text(json.dumps({"top_ports": ["led"]}))
    monkeypatch.setattr(mod, "Lock", lambda *_: nullcontext())

    def execute(argv, **kw):
        source = Path(argv[argv.index("-source") + 1])
        actual = source if source.is_absolute() else Path(kw["cwd"]) / source
        assert actual.is_file(), "relative script is duplicated under the child cwd"
        return SimpleNamespace(returncode=0, stdout="HOST_IO_PORT|led|OUT|A1|LVCMOS33\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", execute)
    record = mod.audit_checkpoint(
        Path("routed.dcp"),
        Path("wrapper.json"),
        Path("run/evidence/ports"),
        Path("vivado.exe"),
        require_host=False,
    )
    assert record["wrapper_ports_match"]
    with pytest.raises(FileExistsError):
        mod.audit_checkpoint(
            Path("routed.dcp"),
            Path("wrapper.json"),
            Path("run/evidence/ports"),
            Path("vivado.exe"),
            require_host=False,
        )
