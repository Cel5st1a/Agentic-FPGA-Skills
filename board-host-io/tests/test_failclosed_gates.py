"""Every fail-closed gate in the host-board generator is exercised behaviourally.

An independent review mutation-tested the frozen tree and showed that neutering all ten
fail-closed conditions (``if False and ...``, error messages left intact) kept the whole
suite green: the older test only grepped for the message strings. Since this project's
guarantee is "fail closed", a message grep is not a test.

Each test below drives `make_host_board.generate()` with a real generated core and a real
contract copied into a throwaway run, mutates exactly one thing, and requires a refusal.
`test_the_fixture_itself_generates_a_board` is the positive control: without it, a fixture
that refused for an unrelated reason would make every other test in this file pass.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from geometry_fixtures import synthetic_geometry, wrapper_record

import pytest


SKILL = Path(__file__).resolve().parents[1]
RUN = Path(r"D:\ML_models\_line\runs\mlp_64_32\20260928-hostd")
CONTRACT_SRC = RUN / "contract" / "mlp_64_32"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def board_profile(**overrides) -> dict:
    """A minimal but complete AX7A200B pins profile, matching what the generator reads.

    Every board-facing fact carries provenance: since the second board was added, a
    profile that declares a pin without saying where it came from is refused, so a
    fixture without provenance would be testing the wrong refusal.
    """
    profile = {
        "board_id": "AX7A200B",
        "fpga": {"part": "xc7a200tfbg484-2"},
        "board_pins": {
            "clock": {"kind": "differential", "clk_p": "R4", "clk_n": "T4",
                      "frequency_mhz": 200, "core_frequency_mhz": 100,
                      "iostandard": "DIFF_SSTL15",
                      "provenance": "test fixture: AX7A200B manual + vendor uart_test.xdc"},
            "reset": {"pin": "F15", "iostandard": "LVCMOS33",
                      "provenance": "test fixture: AX7A200B manual"},
        },
        "peripherals": {"uart": {"fpga_rxd": "L14", "fpga_txd": "L15",
                                 "iostandard": "LVCMOS33",
                                 "host_bridge": {"chip": "CP2102GM", "usb_vid": "10c4",
                                                 "usb_pid": "ea60",
                                                 "serial_number": "TESTSERIAL",
                                                 "provenance": "test fixture"},
                                 "provenance": "test fixture: cnn-board-03 verified pinout"}},
    }
    for key, value in overrides.items():
        target = profile
        parts = key.split("__")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return profile


@pytest.fixture
def run_tree(tmp_path: Path):
    """A throwaway run holding a real contract copy, a real core, and a crafted profile."""
    run = tmp_path / "run"
    contract_dir = run / "contract" / "mlp_64_32"
    contract_dir.parent.mkdir(parents=True)
    shutil.copytree(CONTRACT_SRC, contract_dir)
    deployment = contract_dir / "deployment.yaml"
    contract = load("host_geometry")  # ensures scripts/ is importable; unused value
    del contract

    real = json.loads((RUN / "p3-build" / "board" / "wrapper.json").read_text(encoding="utf-8"))
    model = contract_dir / "encoder.keras"
    (run / "starting-state.json").write_text(
        json.dumps({"model_files": {"encoder.keras": {"sha256": sha256(model)}}}),
        encoding="utf-8")

    profile_path = tmp_path / "board_profile.yaml"
    profile_path.write_text(json.dumps(board_profile()), encoding="utf-8")

    record = {
        "top": real["top"],
        "part": real["part"],
        "contract": str(deployment),
        "core_verilog": real["core_verilog"],
        "core_verilog_sha256": real["core_verilog_sha256"],
        "clock": real["clock"],
        "interface": real["interface"],
        "contract_sha256": sha256(deployment),
        "input_layout": real["input_layout"],
        "rom_image": real["rom_image"],
        "output_grid": real["output_grid"],
        "board": {
            "pins_profile": str(profile_path),
            "pins": real["board"]["pins"],
            "reset": real["board"]["reset"],
            "clock_pair": real["board"]["clock_pair"],
        },
    }
    return {"run": run, "deployment": deployment, "record": record,
            "profile_path": profile_path, "tmp": tmp_path}


def generate(run_tree, out_name="board", generator=None):
    wrapper = run_tree["tmp"] / "wrapper.json"
    wrapper.write_text(json.dumps(run_tree["record"]), encoding="utf-8")
    if generator is None:
        generator = load("make_host_board")
    return generator.generate(wrapper, run_tree["tmp"] / out_name)


# ---------------------------------------------------------------- positive control

def test_the_fixture_itself_generates_a_board(run_tree):
    """Without this, every refusal below could be passing for an unrelated reason."""
    generator = load("make_host_board")
    record = generate(run_tree)
    assert record["skill"] == "board-host-io"
    assert record["host_protocol"]["request_bytes"] == 264
    assert record["host_protocol"]["response_bytes"] == 28
    assert record["host_protocol"]["baudrate"] == 115200
    assert (run_tree["tmp"] / "board" / "host_avld_adapter.v").is_file()
    del generator


# ---------------------------------------------------------------- the ten gates

def test_gate_output_directory_must_be_empty(run_tree):
    generate(run_tree)
    with pytest.raises(ValueError, match="not empty"):
        generate(run_tree)


def test_gate_core_verilog_hash_must_match_the_record(run_tree):
    run_tree["record"]["core_verilog_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="core Verilog hash"):
        generate(run_tree)


def test_gate_model_must_match_the_bootstrap_starting_state(run_tree):
    (run_tree["run"] / "starting-state.json").write_text(
        json.dumps({"model_files": {"encoder.keras": {"sha256": "0" * 64}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="starting-state hash"):
        generate(run_tree)


def test_gate_every_staged_asset_hash_is_enforced(run_tree, monkeypatch):
    generator = load("make_host_board")
    stale = dict(generator.SOURCE_HASHES)
    stale["transport.v"] = "0" * 64
    monkeypatch.setattr(generator, "SOURCE_HASHES", stale)
    with pytest.raises(ValueError, match="RTL asset hash mismatch"):
        generate(run_tree, generator=generator)


def test_uart_pins_follow_the_board_profile(run_tree):
    """A second board is added as data: change the profile, the constraints change.

    This replaces the old "the profile must say L14/L15" gate, which could not express a
    second board at all. The behaviour it protected — a host path is only ever built on
    pins the profile declares — is now the profile's own content, so the check that
    matters is that the generated artifact follows it.
    """
    run_tree["profile_path"].write_text(
        json.dumps(board_profile(peripherals__uart={
            "fpga_rxd": "V18", "fpga_txd": "AA19", "iostandard": "LVCMOS33",
            "host_bridge": {"chip": "FT232R", "usb_vid": "0403", "usb_pid": "6001",
                            "serial_number": "AV0KR4KQ", "provenance": "test fixture"},
            "provenance": "test fixture: Digilent Nexys Video master XDC"})),
        encoding="utf-8")
    generate(run_tree)
    xdc = (run_tree["tmp"] / "board" / "board.xdc").read_text(encoding="utf-8")
    assert "set_property PACKAGE_PIN V18 [get_ports uart_rx]" in xdc
    assert "set_property PACKAGE_PIN AA19 [get_ports uart_tx]" in xdc
    assert "PACKAGE_PIN L14" not in xdc and "PACKAGE_PIN L15" not in xdc


def test_gate_uart_iostandard_is_required(run_tree):
    profile = board_profile()
    del profile["peripherals"]["uart"]["iostandard"]
    run_tree["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="iostandard"):
        generate(run_tree)


def test_gate_board_facts_must_carry_provenance(run_tree):
    """The successor to "the board must be AX7A200B": a second board is welcome, an
    undescribed one is not. Adding a board has to mean stating where its pins came from."""
    profile = board_profile()
    del profile["board_pins"]["clock"]["provenance"]
    run_tree["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        generate(run_tree)


def test_gate_ambiguous_clock_topology_is_refused(run_tree):
    """Both a single-ended pin and a differential pair: refused, never guessed."""
    profile = board_profile()
    profile["board_pins"]["clock"]["pin"] = "R4"
    run_tree["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        generate(run_tree)


def test_single_ended_profile_generates_ibuf_and_one_clock_port(run_tree):
    """The second board's whole difference at the top level: IBUF, one clock port."""
    profile = board_profile()
    profile["board_id"] = "NexysVideo"
    profile["fpga"] = {"part": "xc7a200tsbg484-1"}
    profile["board_pins"]["clock"] = {
        "kind": "single_ended", "pin": "R4", "frequency_mhz": 100,
        "core_frequency_mhz": 100, "iostandard": "LVCMOS33",
        "provenance": "test fixture: Digilent Nexys Video master XDC"}
    profile["board_pins"]["reset"] = {
        "pin": "G4", "iostandard": "LVCMOS15",
        "provenance": "test fixture: Digilent master XDC, cpu_resetn"}
    profile["peripherals"]["uart"] = {
        "fpga_rxd": "V18", "fpga_txd": "AA19", "iostandard": "LVCMOS33",
        "host_bridge": {"chip": "FT232R", "usb_vid": "0403", "usb_pid": "6001",
                        "serial_number": "AV0KR4KQ", "provenance": "test fixture"},
        "provenance": "test fixture: Digilent master XDC, uart_tx_in/uart_rx_out"}
    run_tree["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    run_tree["record"]["part"] = "xc7a200tsbg484-1"
    run_tree["record"]["board"]["part"] = "xc7a200tsbg484-1"
    run_tree["record"]["board"]["pins"] = {"clk_p": "R4", "clk_n": None, "clk_mhz": 100}
    run_tree["record"]["board"]["clock_pair"]["iostandard"] = "LVCMOS33"
    run_tree["record"]["board"]["reset"] = {"pin": "G4", "iostandard": "LVCMOS15"}
    run_tree["record"]["clock"] = dict(run_tree["record"]["clock"], reference_mhz=100.0,
                                       mmcm_mult=10.0, mmcm_divide=10.0)
    record = generate(run_tree)
    top = (run_tree["tmp"] / "board" / "board_top.v").read_text(encoding="utf-8")
    xdc = (run_tree["tmp"] / "board" / "board.xdc").read_text(encoding="utf-8")
    assert "IBUF ref_input(.I(sys_clk), .O(refclk));" in top
    assert "IBUFDS" not in top
    assert record["top_ports"] == ["sys_clk", "rst_n", "uart_rx", "uart_tx"]
    assert "create_clock -name board_ref -period 10.000 [get_ports sys_clk]" in xdc
    assert "set_property PACKAGE_PIN G4 [get_ports rst_n]" in xdc
    # Per-pin standards: the reset is in the 1.5 V bank and the UART in a 3.3 V one, so a
    # single blanket IOSTANDARD line (right for the AX7A200B) would be wrong here.
    assert "set_property IOSTANDARD LVCMOS15 [get_ports {rst_n}]" in xdc
    assert "set_property IOSTANDARD LVCMOS33 [get_ports {uart_rx uart_tx}]" in xdc
    assert record["host_board_facts"]["board_id"] == "NexysVideo"
    assert record["host_board_facts"]["clock"]["kind"] == "single_ended"


def test_gate_uart_bridge_is_a_board_fact(run_tree):
    """A capture must refuse a port that belongs to another board's UART bridge."""
    run_tree["profile_path"].write_text(json.dumps(board_profile()), encoding="utf-8")
    generate(run_tree)
    module = load("board_facts")
    facts = module.load_facts(json.loads(run_tree["profile_path"].read_text()),
                              run_tree["profile_path"])

    class Device:
        device = "COM9"
        serial_number = "TESTSERIAL"
        vid, pid = 0x10C4, 0xEA60

    module.require_usb_bridge(Device(), facts)
    Device.vid, Device.pid = 0x0403, 0x6001
    with pytest.raises(ValueError, match="0403:6001"):
        module.require_usb_bridge(Device(), facts)
    Device.vid, Device.pid = 0x10C4, 0xEA60
    Device.serial_number = "SOMEONEELSE"
    with pytest.raises(ValueError, match="SOMEONEELSE"):
        module.require_usb_bridge(Device(), facts)


def test_gate_record_that_disagrees_with_its_profile_is_refused(run_tree):
    """The record's own copy of the board facts is checked, not trusted."""
    run_tree["record"]["board"]["pins"]["clk_n"] = "T4"
    run_tree["record"]["board"]["reset"]["pin"] = "F16"
    with pytest.raises(ValueError, match="reset.pin"):
        generate(run_tree)


def test_gate_clock_standard_must_match_the_verified_board(run_tree):
    run_tree["record"]["board"]["clock_pair"]["iostandard"] = "LVCMOS33"
    with pytest.raises(ValueError, match="clock_pair"):
        generate(run_tree)


def test_gate_reference_clock_must_be_declared_by_the_profile(run_tree):
    profile = board_profile()
    del profile["board_pins"]["clock"]["frequency_mhz"]
    run_tree["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="frequency_mhz"):
        generate(run_tree)


def test_gate_core_clock_must_follow_the_contract_clock_period(run_tree):
    run_tree["record"]["clock"] = dict(run_tree["record"]["clock"], core_mhz=50.0)
    with pytest.raises(ValueError, match="clock_period_ns"):
        generate(run_tree)


def test_gate_part_must_match_the_board_profile(run_tree):
    run_tree["record"]["part"] = "xc7a100tcsg324-1"
    with pytest.raises(ValueError, match="board profile declares"):
        generate(run_tree)


def test_gate_response_length_must_fit_one_byte():
    """host_geometry's own fail-closed bound, which no other test exercised."""
    module = load("host_geometry")
    with pytest.raises(ValueError, match="one byte|255"):
        synthetic_geometry(module, 
            {"precision": "ap_fixed<32,12>", "model": {"input_shape": [4, 1000]}},
            references=[[0.0] * 1000])


def test_gate_reference_rows_must_be_rows_not_scalars():
    """A flat list of numbers is not a reference table; refuse rather than read it as N rows."""
    module = load("host_geometry")
    with pytest.raises(ValueError, match="row"):
        synthetic_geometry(module, 
            {"precision": "ap_fixed<32,12>", "model": {"input_shape": [4, 64]}},
            references=[0.0, 0.0, 0.0, 0.0])
