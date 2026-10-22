"""Compile and run one Verilog testbench under xsim, in a caller-supplied work directory.

xsim is a simulator, not synthesis: it needs no EDA lock and no bitstream, so an adapter
or transport testbench can be a normal, fast, always-run test instead of an artifact
nobody executes. That is exactly how `host_stream_adapter_tb.v` came to ship unrun.

The runner fails closed. A testbench counts as passed only when every tool exited zero
*and* the run printed its own `..._PASS` marker; a zero exit with no marker is a failure
of the harness, not a pass.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


DEFAULT_VIVADO_BIN = Path(r"D:\vitis\2025.2\Vivado\bin")
#: A testbench announces success with a `<NAME>_PASS` line of its own. Testbenches are
#: allowed to report counts after the marker on the same line, so the marker is matched at
#: the start of a line rather than as the whole line.
MARKER = re.compile(r"(?m)^([A-Z][A-Z0-9_]*_PASS)\b")
#: Measured: xsim exits 0 even after `$fatal`, so the tool's exit code cannot be the
#: gate. This mirrors `simulate_host.simulation_completed`, which rejects the same text.
#: Leading whitespace is allowed: a tool line that is indented is still a failure, and the
#: `..._PASS` marker must be present anyway, so tolerating indentation can only make the
#: verdict stricter.
FAILURE = re.compile(r"(?m)^\s*(Fatal:|ERROR:)")
DEFAULT_TIMEOUT_S = 1800


class TestbenchError(RuntimeError):
    """Raised when the testbench harness itself cannot run."""


def run_testbench(top: str, sources, workdir: Path, data_files=(),
                  vivado_bin: Path = DEFAULT_VIVADO_BIN,
                  timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Elaborate `top` from `sources` and run it to completion.

    `data_files` are stimulus inputs (`$readmemh` targets); they are copied into the work
    directory, which is also the simulation's working directory, but are never handed to
    the compiler — a `.hex` file passed to `xvlog` is a syntax error.

    `returncode` is the *effective* status: 0 only when every tool exited zero, the run
    printed no `Fatal:`/`ERROR:` line, and the testbench printed its own PASS marker.
    `tool_returncode` carries the last tool's raw exit code, which is 0 even for a
    `$fatal` and is therefore recorded rather than trusted.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    resolved = []
    for source in sources:
        path = Path(source).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"testbench source does not exist: {path}")
        resolved.append(path)
    for data in data_files:
        path = Path(data).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"testbench data file does not exist: {path}")
        destination = workdir / path.name
        if destination != path:
            shutil.copyfile(path, destination)
    tools = {name: Path(vivado_bin) / f"{name}.bat" for name in ("xvlog", "xelab", "xsim")}
    for name, tool in tools.items():
        if not tool.is_file():
            raise TestbenchError(f"xsim tool not found: {tool}")
    snapshot = f"{top}_sim"
    steps = (
        [str(tools["xvlog"]), "--nolog", *[str(path) for path in resolved]],
        [str(tools["xelab"]), "--nolog", top, "-s", snapshot],
        [str(tools["xsim"]), "--nolog", snapshot, "-R"],
    )
    collected: list[str] = []
    last_code = 0
    for step in steps:
        try:
            done = subprocess.run(step, cwd=workdir, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            collected.append(f"TIMEOUT after {timeout}s: {' '.join(step)}")
            return {"returncode": 124, "tool_returncode": 124,
                    "stdout": "\n".join(collected), "marker": None,
                    "failed_step": Path(step[0]).stem}
        last_code = done.returncode
        collected.extend(part for part in (done.stdout, done.stderr) if part)
        if done.returncode != 0:
            return {"returncode": done.returncode, "tool_returncode": done.returncode,
                    "stdout": "\n".join(collected), "marker": None,
                    "failed_step": Path(step[0]).stem}
    text = "\n".join(collected)
    found = MARKER.findall(text)
    marker = found[-1] if found else None
    failure = FAILURE.search(text)
    if failure is not None:
        return {"returncode": 1, "tool_returncode": last_code, "stdout": text,
                "marker": marker, "failed_step": failure.group(1)}
    if marker is None:
        return {"returncode": 1, "tool_returncode": last_code, "stdout": text,
                "marker": None, "failed_step": "no-pass-marker"}
    return {"returncode": 0, "tool_returncode": last_code, "stdout": text,
            "marker": marker, "failed_step": None}
