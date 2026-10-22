"""Behavioral checks for the board host path; no FPGA hardware is required."""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import time
from pathlib import Path

from geometry_fixtures import synthetic_geometry, wrapper_record

import pytest


SKILLS = Path(r"C:\Users\Administrator\Agentic_FPGA_Deployment_skills")
HERE = Path(__file__).resolve().parents[1]
CNN_WRAPPER = Path(r"D:\ML_models\_line\runs\asymmetric_cnn_fc\20260928-cnn09\p3-build\board\wrapper.json")
MLP_WRAPPER = Path(r"D:\ML_models\_line\runs\mlp_64_32\20260928-mlp03\p3-build\board\wrapper.json")


def load_script(name: str):
    path = HERE / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules[cls.__module__], so a module
    # loaded without being registered cannot define a dataclass.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verified_geometry():
    """The derived geometry of the verified 64x4 grid, for tests that need a frame."""
    module = load_script("host_geometry")
    return synthetic_geometry(module, 
        {"precision": "ap_fixed<32,12>", "model": {"input_shape": [256, 64]}},
        references=[[0.0] * 4])


@pytest.mark.parametrize("source", [CNN_WRAPPER, MLP_WRAPPER], ids=["io_stream", "io_parallel"])
def test_host_wrapper_uses_actual_core_interface_and_uart_ports(source: Path, tmp_path: Path):
    generator = load_script("make_host_board")
    record = generator.generate(source, tmp_path / "board")
    top = (tmp_path / "board" / "board_top.v").read_text(encoding="utf-8")
    xdc = (tmp_path / "board" / "board.xdc").read_text(encoding="utf-8")
    assert record["top_ports"] == ["sys_clk_p", "sys_clk_n", "rst_n", "uart_rx", "uart_tx"]
    assert record["host_input_output_available"] is True
    assert record["hardware_verified"] is False
    assert len(record["model_sha256"]) == 64
    assert set(record['rom_image']) == {'grid'}  # provenance only; no ROM path/image
    assert not (tmp_path / 'board' / 'input_words.mem').exists()
    assert "input_words.mem" not in record["outputs"]
    assert "$readmemh" not in top
    assert "input_words.mem" not in top
    assert "cnn_uart_transport #(" in top and ") transport(" in top
    assert "PACKAGE_PIN L14 [get_ports uart_rx]" in xdc
    assert "PACKAGE_PIN L15 [get_ports uart_tx]" in xdc
    assert "set_false_path -from [get_ports uart_rx]" in xdc
    assert "set_false_path -to [get_ports uart_tx]" in xdc
    if record["interface"]["style"].startswith("axis"):
        assert ".waveform_input_TDATA(" in top
        assert ".layer10_out_TDATA(" in top
        assert "host_stream_adapter" in top
        assert "host_avld_adapter" not in top
    else:
        # D-M01: the core is fed by the ap_vld adapter, never directly by the transport.
        assert "host_avld_adapter" in top
        assert ".ae_input(core_input)" in top
        assert ".ae_input_ap_vld(core_input_vld)" in top
        assert ".ae_input(input_words)" not in top
        assert ".ae_input_ap_vld(1'b1)" not in top
        assert ".layer6_out_0(core_output[31:0])" in top
    assert record["host_protocol"]["request_bytes"] == 264
    assert record["host_protocol"]["response_bytes"] == 28
    assert record["host_protocol"]["geometry_source"] == record["host_geometry"]["geometry_source"]
    assert "wrapper.input_layout" in record["host_protocol"]["geometry_source"]


def test_transport_sources_are_staged_with_core(tmp_path: Path):
    stage_path = SKILLS / "vivado-manager" / "scripts" / "stage_rtl.py"
    spec = importlib.util.spec_from_file_location("stage_rtl", stage_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    core = core_dir / "sample.v"
    core.write_text("module sample; endmodule\n", encoding="utf-8")
    board = tmp_path / "board"
    board.mkdir()
    (board / "board_top.v").write_text("module board_top; endmodule\n", encoding="utf-8")
    (board / "board.xdc").write_text("", encoding="utf-8")
    (board / "transport.v").write_text("module transport; endmodule\n", encoding="utf-8")
    record = module.stage_rtl(board, {"core_verilog": str(core), "extra_rtl": ["transport.v"]})
    assert "transport.v" in record["files"]
    assert (board / "rtl" / "transport.v").is_file()


def test_protocol_rejects_partial_duplicate_and_bad_crc():
    protocol = load_script("protocol")
    geometry = verified_geometry()
    values = [0.0] * 64
    request = protocol.encode_request(values, 7, geometry)
    assert len(request) == geometry.request_bytes == 264
    assert request[:2] == b"\xa5\x5a"
    assert protocol.crc16(request[:-2]) == int.from_bytes(request[-2:], "little")
    body = b"\x5a\xa5" + struct.pack("<HHI4i", 7, 20, 656, 0, 0, 0, 0)
    response = body + struct.pack("<H", protocol.crc16(body))
    assert protocol.decode_response(response, 7, geometry)[0] == 656
    with pytest.raises(ValueError, match="length"):
        protocol.decode_response(response[:9], 7, geometry)
    with pytest.raises(ValueError, match="sequence"):
        protocol.decode_response(response, 6, geometry)
    with pytest.raises(ValueError, match="CRC"):
        protocol.decode_response(response[:-1] + b"\x00", 7, geometry)
    with pytest.raises(ValueError, match="length"):
        protocol.decode_response(response + response, 7, geometry)


def test_port_audit_classifies_missing_host_path_as_configure_only():
    audit = load_script("audit_ports")
    scope = audit.classify_ports({"sys_clk_p": "IN", "sys_clk_n": "IN", "rst_n": "IN", "led": "OUT"})
    assert scope["verification_scope"] == "configure-only"
    assert scope["hardware_verified"] is False
    assert scope["on_board_numerical"] == "NOT_RUN"
    with pytest.raises(ValueError, match="uart_tx"):
        audit.require_host_path({"sys_clk_p": "IN", "sys_clk_n": "IN", "rst_n": "IN", "uart_rx": "IN"})


def test_program_log_requires_done_and_clean_configuration():
    program = load_script("program_jtag")
    good = Path(r"D:\Agentic_FPGA_Deployment\runs\cnn-program-01\execution\stdout.txt").read_text(
        encoding="utf-8"
    )
    program.validate_program_stdout(good, "210512180081", "xc7a200t")
    with pytest.raises(ValueError, match="DONE PIN"):
        program.validate_program_stdout(good.replace("DONE PIN (Bits [14]): 1", "DONE PIN (Bits [14]): 0"),
                                        "210512180081", "xc7a200t")
    with pytest.raises(ValueError, match="JTAG cable"):
        program.validate_program_stdout(good, "wrong-cable", "xc7a200t")
    with pytest.raises(ValueError, match="xc7a100t"):
        program.validate_program_stdout(good, "210512180081", "xc7a100t")


def test_board_evidence_uses_frozen_criterion_and_exact_sample_count():
    verifier = load_script("verify_board")
    deployment = Path(r"D:\ML_models\_line\runs\asymmetric_cnn_fc\20260928-cnn09\contract\asymmetric_cnn_fc\deployment.yaml")
    reference = [[0.0, 1.0, 2.0, 3.0] for _ in range(256)]
    passed = verifier.evaluate_capture(deployment, reference, reference, reference)
    assert passed["status"] == "HARDWARE_NUMERICAL_PASSED"
    assert passed["python"]["criterion"]["samples"] == 256
    assert passed["python"]["samples"]["compared"] == 256
    damaged = [row[:] for row in reference]
    damaged[10][2] = 9.0
    failed = verifier.evaluate_capture(deployment, reference, reference, damaged)
    assert failed["status"] == "FAILED"
    assert failed["python"]["passed"] is False
    short = verifier.evaluate_capture(deployment, reference, reference, reference[:-1])
    assert short["status"] == "FAILED"


def test_exchange_records_partial_frame_before_timeout(monkeypatch):
    protocol = load_script("protocol")

    class PartialSerial:
        in_waiting = 0
        timeout = 0

        def __init__(self):
            self.reads = [b"\x5a\xa5\x07"]

        def write(self, data):
            return len(data)

        def read(self, count):
            return self.reads.pop(0) if self.reads else b""

    chunks = []
    with pytest.raises(TimeoutError, match="partial response"):
        protocol.exchange(PartialSerial(), [0.0] * 64, 7, verified_geometry(), timeout_s=0.001,
                          on_receive=chunks.append)
    assert chunks == [b"\x5a\xa5\x07"]


def test_host_build_stops_before_impl_when_synth_does_not_pass():
    build = load_script("build_host")
    attempted = []

    def run(stage):
        attempted.append(stage)
        return {"status": "FAILED" if stage == "synth" else "PASSED"}

    with pytest.raises(ValueError, match="synth"):
        build.execute_stages(run)
    assert attempted == ["synth"]


def test_program_gate_rejects_adapter_changed_after_bitstream(tmp_path: Path):
    import hashlib

    program = load_script("program_jtag")
    board = tmp_path / "board"
    board.mkdir()
    names = ["board_top.v", "board.xdc", "transport.v", "uart_rx.v", "uart_tx.v",
             "host_stream_adapter.v"]
    for name in names:
        (board / name).write_text(name, encoding="utf-8")
    wrapper = board / "wrapper.json"
    wrapper.write_text(json.dumps({"extra_rtl": ["transport.v", "uart_rx.v", "uart_tx.v",
                                                 "host_stream_adapter.v"],
                                   "board": {"pins_profile": str(
                                       SKILLS / "board-profiles" / "AX7A200B"
                                       / "board_profile.yaml")}}), encoding="utf-8")
    bitstream = board / "board_top.bit"
    bitstream.write_bytes(b"host bitstream")
    dcp = board / "routed.dcp"
    dcp.write_bytes(b"routed")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    scope = tmp_path / "io-scope.json"
    scope.write_text(json.dumps({"host_input_output_available": True,
                                 "verification_scope": "host-path-present-unverified",
                                 "on_board_numerical": "NOT_RUN", "hardware_verified": False,
                                 "wrapper_ports_match": True, "checkpoint": str(dcp),
                                 "checkpoint_sha256": digest(dcp), "wrapper": str(wrapper),
                                 "wrapper_sha256": digest(wrapper),
                                 "ports": {"uart_rx": {"direction": "IN", "pin": "L14",
                                                       "iostandard": "LVCMOS33"},
                                           "uart_tx": {"direction": "OUT", "pin": "L15",
                                                       "iostandard": "LVCMOS33"}}}), encoding="utf-8")
    evidence = tmp_path / "bit-evidence.json"
    evidence.write_text(json.dumps({"status": "PASSED",
                                    "artifacts": {"bitstream": {"sha256": digest(bitstream)},
                                                  "routed.dcp": {"sha256": digest(dcp)}},
                                    "rtl_used": {"files": {name: digest(board / name)
                                                           for name in names}}}), encoding="utf-8")
    program.validate_inputs(bitstream, scope, evidence)
    (board / "host_stream_adapter.v").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="host_stream_adapter.v"):
        program.validate_inputs(bitstream, scope, evidence)
    (board / "host_stream_adapter.v").write_text("host_stream_adapter.v", encoding="utf-8")
    scope_data = json.loads(scope.read_text(encoding="utf-8"))
    scope_data["ports"]["uart_rx"]["pin"] = "L16"
    scope.write_text(json.dumps(scope_data), encoding="utf-8")
    with pytest.raises(ValueError, match="L14"):
        program.validate_inputs(bitstream, scope, evidence)
    # The pins are checked against the profile the wrapper names, so a scope carried over
    # from the other board is refused even when its numbers are internally consistent.
    scope_data["ports"]["uart_rx"] = {"direction": "IN", "pin": "V18",
                                      "iostandard": "LVCMOS33"}
    scope.write_text(json.dumps(scope_data), encoding="utf-8")
    with pytest.raises(ValueError, match="V18"):
        program.validate_inputs(bitstream, scope, evidence)


def test_response_reader_never_touches_the_port_timeout():
    """Re-applying COMMTIMEOUTS mid-transaction loses in-flight bytes on the FTDI driver.

    Measured on the Nexys Video (FT232R), same frozen frame format and bitstream:
    assigning `serial.timeout` every loop pass lost 1 request in 7..28, once per
    transaction lost 1 in 70, and never assigning it lost 0 in 450. The port's timeout
    belongs to the session that opened it. The fake below has no `timeout` attribute at
    all, so any assignment raises.
    """
    protocol = load_script("protocol")

    class BareSerial:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def read(self, count):
            return self._chunks.pop(0) if self._chunks else b""

        def __setattr__(self, name, value):
            if name == "timeout":
                raise AssertionError("read_exact must not assign the port timeout")
            super().__setattr__(name, value)

    assert protocol.read_exact(BareSerial([b"", b"ab", b"", b"cd"]), 4,
                               time.monotonic() + 1.0) == b"abcd"
    # A deadline already in the past refuses before reading anything.
    with pytest.raises(TimeoutError, match="partial response: 0/4"):
        protocol.read_exact(BareSerial([b"a"]), 4, time.monotonic() - 1.0)


def test_capture_bridge_check_reads_the_wrapper_record_from_disk(tmp_path: Path):
    """The check takes the wrapper *path*, as `preflight` returns it.

    The first version of this check passed that Path straight to the profile reader, which
    expects a mapping: a real board had already been configured before the capture died on
    `'WindowsPath' object has no attribute 'get'`. The fixture below is therefore a path.
    """
    import types

    verifier = load_script("verify_board")
    wrapper = tmp_path / "wrapper.json"
    wrapper.write_text(json.dumps({"board": {"pins_profile": str(
        SKILLS / "board-profiles" / "NexysVideo" / "board_profile.yaml")}}), encoding="utf-8")

    def device(serial, vid, pid, port="COM4"):
        return types.SimpleNamespace(device=port, serial_number=serial, vid=vid, pid=pid)

    ft232r = device("AV0KR4KQA", 0x0403, 0x6001)
    assert verifier.require_selected_bridge(wrapper, "COM4", [ft232r]) == "AV0KR4KQA"
    # The AX7A200B's bridge, on this board's port: refused on its serial number.
    with pytest.raises(ValueError, match="declares 'AV0KR4KQA'"):
        verifier.require_selected_bridge(wrapper, "COM4",
                                         [device("CA127350C9F9EF1187F562135C2A50C9",
                                                 0x10C4, 0xEA60)])
    # Right serial, wrong silicon: refused on the ids.
    with pytest.raises(ValueError, match="0403:6001"):
        verifier.require_selected_bridge(wrapper, "COM4",
                                         [device("AV0KR4KQA", 0x10C4, 0xEA60)])
    with pytest.raises(ValueError, match="no single serial device"):
        verifier.require_selected_bridge(wrapper, "COM4", [])


def test_package_requires_routed_scope_and_records_configure_only(tmp_path: Path):
    import hashlib

    script = SKILLS / "bitstream-package" / "scripts" / "package_bitstream.py"
    spec = importlib.util.spec_from_file_location("package_bitstream", script)
    pkg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg)
    board = tmp_path / "p3-build" / "board"
    board.mkdir(parents=True)
    wrapper = board / "wrapper.json"
    wrapper.write_text(json.dumps({"top_ports": ["sys_clk_p", "sys_clk_n", "rst_n", "led"]}),
                       encoding="utf-8")
    dcp = board / "routed.dcp"
    dcp.write_bytes(b"routed")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = {"vivado-bitstream-evidence.json":
                {"artifacts": {"routed.dcp": {"sha256": digest(dcp)}}}}
    refusals = []
    assert pkg.verify_io_scope(tmp_path, evidence, refusals) is None
    assert any("io-scope" in reason for reason in refusals)
    audit_dir = tmp_path / "evidence" / "io-port-audit"
    audit_dir.mkdir(parents=True)
    scope_path = audit_dir / "io-scope.json"
    scope_path.write_text(json.dumps({"checkpoint": str(dcp), "checkpoint_sha256": digest(dcp),
                                      "wrapper": str(wrapper), "wrapper_sha256": digest(wrapper),
                                      "wrapper_ports_match": True,
                                      "top_ports": ["led", "rst_n", "sys_clk_n", "sys_clk_p"],
                                      "ports": {"led": {"direction": "OUT"},
                                                "rst_n": {"direction": "IN"},
                                                "sys_clk_n": {"direction": "IN"},
                                                "sys_clk_p": {"direction": "IN"}},
                                      "host_input_output_available": False,
                                      "verification_scope": "configure-only",
                                      "hardware_verified": False,
                                      "on_board_numerical": "NOT_RUN"}), encoding="utf-8")
    refusals = []
    scope = pkg.verify_io_scope(tmp_path, evidence, refusals)
    assert refusals == []
    assert scope["verification_scope"] == "configure-only"


def test_line_has_routed_port_gate_before_package(tmp_path: Path):
    script = SKILLS / "scripts" / "line" / "run_line.py"
    spec = importlib.util.spec_from_file_location("run_line", script)
    line = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(line)
    assert line.STAGE_ORDER.index("bitstream") < line.STAGE_ORDER.index("ports")
    assert line.STAGE_ORDER.index("ports") < line.STAGE_ORDER.index("package")
    assert any("io-scope" in reason for reason in line.stage_verdict("ports", tmp_path))


def test_audit_cannot_skip_eda_lock_without_line_parent(monkeypatch, tmp_path: Path):
    audit = load_script("audit_ports")
    monkeypatch.setattr(audit, "LOCK_PATH", tmp_path / "missing.lock")
    with pytest.raises(ValueError, match="EDA lock"):
        audit.require_parent_lock()


def test_host_build_requires_core_and_rom_wrapper_from_same_run(tmp_path: Path):
    build = load_script("build_host")
    run = tmp_path / "runs" / "new"
    run.mkdir(parents=True)
    old = tmp_path / "runs" / "old"
    old.mkdir(parents=True)
    with pytest.raises(ValueError, match="same run"):
        build.require_run_local_sources({"source_wrapper": str(old / "wrapper.json"),
                                         "core_verilog": str(old / "core.v")}, run)
