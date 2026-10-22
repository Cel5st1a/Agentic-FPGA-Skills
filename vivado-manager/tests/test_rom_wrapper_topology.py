"""The ROM wrapper's board top must follow the board profile's clock topology.

This file exists because of a near-miss. Generalising the host wrapper for a second board
touched `make_host_board.py`, `audit_ports.py`, `program_jtag.py` and the packager — but
the ROM wrapper's **AXI-Stream** path kept a literal `input wire sys_clk_p / sys_clk_n` and
`IBUFDS`. Nothing failed: the ALINX tests compare differential to differential, and the
synthetic fixtures never render the stream path at all. A Nexys Video stream model would
have been refused by its own XDC.

So both topologies are rendered here, from a real generated stream core, and the core's own
clock port name is asserted to survive the change (`clock` used to be a string there; it
became a dict by accident during the refactor, which no assertion noticed).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2]
PRODUCER = SKILLS / "vivado-manager" / "scripts" / "make_board_wrapper.py"
STREAM_RUN = Path(
    r"D:\openhands\workspace\cnn-fresh-run\runs\asymmetric_cnn_fc\openhands-fresh-01")


def generate(tmp_path: Path, board: str) -> tuple[str, dict]:
    deployment = STREAM_RUN / "contract" / "asymmetric_cnn_fc" / "deployment.yaml"
    verilog = (STREAM_RUN / "p2-convert" / "project" / "myproject_prj" / "solution1"
               / "syn" / "verilog")
    reference = STREAM_RUN / "evidence" / "python-output.json"
    if not (deployment.is_file() and verilog.is_dir() and reference.is_file()):
        pytest.skip(f"recorded stream run not present: {STREAM_RUN}")
    output = tmp_path / board
    command = [sys.executable, "-B", str(PRODUCER),
               "--deployment", str(deployment), "--verilog-dir", str(verilog),
               "--top", "myproject", "--output", str(output),
               "--board-profile", str(SKILLS / "board-profiles" / board / "board_profile.yaml"),
               "--reference-json", str(reference)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return (output / "board_top.v").read_text(encoding="utf-8"), json.loads(
        (output / "wrapper.json").read_text(encoding="utf-8"))


def test_differential_board_gets_ibufds_and_two_clock_ports(tmp_path):
    top, record = generate(tmp_path, "AX7A200B")
    assert "    input  wire sys_clk_p,\n    input  wire sys_clk_n,\n" in top
    assert "IBUFDS ref_input(.I(sys_clk_p), .IB(sys_clk_n), .O(refclk));" in top
    assert record["top_ports"] == ["sys_clk_p", "sys_clk_n", "rst_n", "led"]
    assert record["part"] == "xc7a200tfbg484-2"
    assert record["clock"]["reference_mhz"] == 200.0


def test_single_ended_board_gets_ibuf_and_one_clock_port(tmp_path):
    top, record = generate(tmp_path, "NexysVideo")
    assert "    input  wire sys_clk,\n    input  wire rst_n,\n" in top
    assert "IBUF ref_input(.I(sys_clk), .O(refclk));" in top
    assert "IBUFDS" not in top and "sys_clk_n" not in top
    assert record["top_ports"] == ["sys_clk", "rst_n", "led"]
    assert record["part"] == "xc7a200tsbg484-1"
    assert record["clock"]["reference_mhz"] == 100.0
    # 100 MHz in, 100 MHz out: the MMCM stays in the path so `~rst_n | ~locked` keeps its
    # meaning, and the VCO stays inside the MMCME2 range.
    assert 600.0 <= record["clock"]["vco_mhz"] <= 1200.0


@pytest.mark.parametrize("board", ["AX7A200B", "NexysVideo"])
def test_core_clock_port_name_survives_the_topology_change(board, tmp_path):
    """`clock` in that generator is the core's port name; the topology must not replace it.

    During the refactor a topology dict was bound to that name, and every test still
    passed: the generated file simply carried a Python dict repr where the core's clock
    port should have been.
    """
    top, record = generate(tmp_path, board)
    assert ".ap_clk(clk)" in top
    assert ".ap_rst" in top
    assert "{'" not in top and "declarations" not in top
    xdc = (tmp_path / board / "board.xdc").read_text(encoding="utf-8")
    assert "set_property PACKAGE_PIN R4 [get_ports sys_clk" in xdc
