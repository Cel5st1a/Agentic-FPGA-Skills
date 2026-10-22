"""Build the UART transport around a compatible hls4ml core.

This is a separate board variant: the ROM wrapper and its evidence are never rewritten.

Every wire-level number comes from `host_geometry`, which derives it from the deployment
contract (`precision`, `model.input_shape`) and the generated core's own interface record.
Nothing here pins a grid, an element count or a frame length.

Every *board-facing* number comes from `board_facts`, which reads the board profile the
wrapper was built from (`board-profiles/<BOARD>/board_profile.yaml`) and refuses a profile
that leaves a fact undeclared or unsourced. Nothing here pins a pin name, an I/O standard
or a clock topology either. The AX7A200B and the Nexys Video share the `R4` pin name and
mean different things by it (200 MHz differential `DIFF_SSTL15` versus 100 MHz
single-ended `LVCMOS33`), so the clock input stage is chosen from the profile's declared
`kind` — `IBUFDS` for a differential pair, `IBUF` for a single-ended pin — while the MMCM
stays in the path on both boards so the reset semantics (`~rst_n | ~locked`) are identical.

Which adapter sits between the transport and the core is decided by the core's *actual*
ports (`interface.style`), never by the contract's `io_type` string:

* `ap_vld_array_with_ap_ctrl_hs` -> `host_avld_adapter.v` (latched input, `ap_start` level
  held to `ap_done`; without it a transport one-cycle pulse hangs the core — defect D-M01);
* `axis_stream_with_ap_ctrl_hs`  -> `host_stream_adapter.v` (keep a dataflow core running,
  inject one framed sample, correlate ordered beats).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import yaml


sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_facts  # noqa: E402
import host_geometry  # noqa: E402


SKILLS = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parents[1] / "assets"
#: Assets staged into a build. Edited deliberately, never incidentally: a mismatch fails
#: closed before anything is written. `references/frozen/transport-96ceb796.v` is the
#: differential reference for the parameterised transport and is never staged.
SOURCE_HASHES = {
    "transport.v": "79648c16aff5659d3bff6d554ae3a55adf75e312541e9e46b4d7d3dab719f056",
    "uart_rx.v": "f5570b4c8f030694afd778d380b473b6987b7ef1f10e14dbeb762d4d4e3d5965",
    "uart_tx.v": "f72aa61750168dee3c8c874a8c4edb3aaa2573400a2ce7bea3c68062c4151974",
    "host_stream_adapter.v": "216192b0d637ca92895c68595b2c6399c8d6565f08bdb5e3a301f34f43bb695a",
    "host_avld_adapter.v": "a72944d5e7296f04ee532519af102fd7cdb126a0293f4b1a2e34bb868966e5a2",
}
ADAPTER_FOR_MODE = {"parallel": "host_avld_adapter.v", "stream": "host_stream_adapter.v"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def core_ports(path: Path, name: str) -> dict:
    helper = SKILLS / "vivado-manager" / "scripts" / "make_board_wrapper.py"
    spec = importlib.util.spec_from_file_location("make_board_wrapper", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_ports(path.read_text(encoding="utf-8", errors="replace"), name)


def require_port(ports: dict, name: str, direction: str, bits: int) -> None:
    if ports.get(name) != {"direction": direction, "bits": bits}:
        raise ValueError(f"core port {name} must be {direction} {bits} bits; found {ports.get(name)}")


def validate_interface(record: dict, ports: dict, geometry: host_geometry.Geometry) -> str:
    """Check the generated core against the derived geometry; return the adapter mode."""
    require_port(ports, "ap_clk", "input", 1)
    require_port(ports, "ap_start", "input", 1)
    require_port(ports, "ap_done", "output", 1)
    interface = record["interface"]
    style = interface["style"]
    if style == host_geometry.AP_VLD_STYLE:
        require_port(ports, "ap_rst", "input", 1)
        # The adapter starts from ap_idle, so a core that cannot report it is refused
        # rather than fed transactions it may not be ready for.
        require_port(ports, "ap_idle", "output", 1)
        require_port(ports, interface["input"], "input", geometry.in_bits)
        require_port(ports, interface["valid"], "input", 1)
        outputs = interface["result_outputs"]
        if len(outputs) != geometry.out_elements:
            raise ValueError(
                f"core has {len(outputs)} result words, the geometry derives "
                f"{geometry.out_elements}")
        for name in outputs:
            require_port(ports, name, "output", geometry.out_element_bits)
        return "parallel"
    if style == host_geometry.AXIS_STYLE:
        require_port(ports, "ap_rst_n", "input", 1)
        stream = interface["stream"]
        # Multi-beat input: There are two distinct
        # widths and they must not be conflated:
        #   * the *payload* width  = in_elements * element_bits (the whole sample), and
        #   * the *port* width      = input_lanes_per_beat * element_bits (one beat).
        # The generated RTL's real port is the port width; the transport still moves the
        # whole payload. Geometry keeps payload_bytes = in_elements*element_bits/8, while
        # the adapter and this validation use the port width.
        port_in_bits = stream["input_lanes_per_beat"] * stream["element_bits"]
        for key, value in (("input_elements", geometry.in_elements),
                           ("output_elements", geometry.out_elements),
                           ("element_bits", geometry.in_element_bits),
                           ("output_element_bits", geometry.out_element_bits),
                           ("tdata_bits", port_in_bits),
                           ("output_tdata_bits", geometry.out_bits)):
            if stream.get(key) != value:
                raise ValueError(f"generated stream geometry has {key}={stream.get(key)}, "
                                 f"the wrapper records {value}")
        for key, direction, bits in (
            ("input_port", "input", port_in_bits), ("input_valid", "input", 1),
            ("input_ready", "output", 1), ("output_port", "output", geometry.out_bits),
            ("output_valid", "output", 1), ("output_ready", "input", 1),
        ):
            require_port(ports, interface[key], direction, bits)
        for side, element_bits, integer_bits in (
                ("input", geometry.in_element_bits, geometry.in_integer_bits),
                ("output", geometry.out_element_bits, geometry.out_integer_bits)):
            element = interface["element_types"][side]
            if (element["total_bits"], element["integer_bits"]) != (element_bits, integer_bits):
                raise ValueError(
                    f"generated {side} element grid ap_fixed<{element['total_bits']},"
                    f"{element['integer_bits']}> disagrees with the derived "
                    f"ap_fixed<{element_bits},{integer_bits}>")
        return "stream"
    raise ValueError(f"unsupported interface style {style}")


def render_top(record: dict, mode: str, geometry: host_geometry.Geometry,
               facts: board_facts.BoardFacts) -> str:
    interface = record["interface"]
    # The core's actual input PORT is one beat wide; the whole sample may take several
    # beats (see docs/deployment-recovery.md). Derived from the recorded wrapper, not
    # from the payload width.
    port_in_bits = (interface["stream"]["input_lanes_per_beat"]
                    * interface["stream"]["element_bits"]) if mode == "stream" else geometry.in_bits
    clock = record["clock"]
    core_mhz = int(round(float(clock["core_mhz"])))
    clock_ports = facts.clock_ports
    if facts.clock_kind == "differential":
        clock_declaration = f"input wire {clock_ports[0]}, input wire {clock_ports[1]}"
        clock_input = (f"    IBUFDS ref_input(.I({clock_ports[0]}), .IB({clock_ports[1]}), "
                       f".O(refclk));")
    else:
        clock_declaration = f"input wire {clock_ports[0]}"
        clock_input = f"    IBUF ref_input(.I({clock_ports[0]}), .O(refclk));"
    # The transport moves whole bytes, so its two buses are the byte-padded widths, not the
    # padding-free core widths. For a 3 x 20-bit result grid that is 64 bits carrying a
    # 60-bit value: a 60-bit bus would make the transport's last response byte a part-select
    # of bits that do not exist.
    transport_parameters = (
        f".CLK_HZ({core_mhz * 1000000}), .PAYLOAD_BYTES({geometry.payload_bytes}), "
        f".RESPONSE_BYTES({geometry.response_bytes}), .INPUT_BITS({geometry.in_words_bits}), "
        f".OUTPUT_BITS({geometry.out_words_bits})")
    adapter = ""
    if mode == "parallel":
        core_connections = [
            ".ap_clk(clk)",
            ".ap_rst(~internal_rst_n)",
            ".ap_start(core_start)",
            ".ap_done(ap_done)",
            f".{interface['input']}(core_input)",
            f".{interface['valid']}(core_input_vld)",
        ]
        core_connections += [
            f".{name}(core_output[{(index + 1) * geometry.out_element_bits - 1}:"
            f"{index * geometry.out_element_bits}])"
            for index, name in enumerate(interface["result_outputs"])]
        if "ap_idle" in interface.get("control", []):
            core_connections.append(".ap_idle(ap_idle)")
        if "ap_ready" in interface.get("control", []):
            core_connections.append(".ap_ready()")
        adapter = f"""
    wire [{geometry.in_bits - 1}:0] core_input;
    wire [{geometry.out_bits - 1}:0] core_output;
    wire core_input_vld, core_start, ap_done, ap_idle;
    host_avld_adapter #(.INPUT_WORDS_BITS({geometry.in_words_bits}),
                        .CORE_INPUT_BITS({geometry.in_bits}),
                        .OUTPUT_WORDS_BITS({geometry.out_words_bits}),
                        .CORE_OUTPUT_BITS({geometry.out_bits})) apvld_adapter(
        .clk(clk), .rst_n(internal_rst_n), .start(start), .input_words(input_words),
        .done(done), .output_words(output_words), .core_input(core_input),
        .core_input_vld(core_input_vld), .ap_start(core_start), .ap_done(ap_done),
        .ap_idle(ap_idle), .core_output(core_output));
"""
    else:
        core_connections = [
            ".ap_clk(clk)",
            ".ap_rst_n(core_rst_n)",
            ".ap_start(core_start)",
            ".ap_done()",
            f".{interface['input_port']}(m_tdata)",
            f".{interface['input_valid']}(m_tvalid)",
            f".{interface['input_ready']}(m_tready)",
            f".{interface['output_port']}(s_tdata)",
            f".{interface['output_valid']}(s_tvalid)",
            f".{interface['output_ready']}(s_tready)",
        ]
        for name in ("ap_idle", "ap_ready"):
            if name in interface.get("control", []):
                core_connections.append(f".{name}()")
        adapter = f"""
    wire core_start, core_rst_n, m_tvalid, m_tready, s_tvalid, s_tready;
    // The transport carries the whole-sample payload ({geometry.in_words_bits} bits); the
    // core's actual input PORT is one beat wide ({port_in_bits} bits) and the sample is
    // handed over INPUT_BEATS beats (see docs/deployment-recovery.md).
    wire [{geometry.in_words_bits - 1}:0] input_words_full;
    wire [{port_in_bits - 1}:0] m_tdata;
    wire [{geometry.out_bits - 1}:0] s_tdata;
    host_stream_adapter #(.INPUT_WORDS_BITS({geometry.in_words_bits}),
                          .PORT_BITS({port_in_bits}),
                          .OUTPUT_BITS({geometry.out_bits}),
                          .INPUT_BEATS({interface['stream']['input_beats_per_sample']})) stream_adapter(
        .clk(clk), .rst_n(internal_rst_n), .start(start),
        .input_words(input_words_full), .done(done), .output_words(output_words),
        .core_rst_n(core_rst_n),
        .ap_start(core_start), .m_tdata(m_tdata), .m_tvalid(m_tvalid),
        .m_tready(m_tready), .s_tdata(s_tdata), .s_tvalid(s_tvalid),
        .s_tready(s_tready));
    // The transport delivers the whole-sample payload; the adapter walks it beat by beat.
    assign input_words_full = input_words;
"""
    joined = ",\n        ".join(core_connections)
    return f"""`timescale 1ns/1ps
// UART host path for board {facts.board_id} ({facts.part}); no sample ROM. Board-facing
// pins, standards and the clock topology come from {facts.profile_path.name} in
// board-profiles/, not from a pinout remembered here.
// Geometry: {geometry.geometry_source}; payload {geometry.payload_bytes} B,
// response {geometry.response_bytes} B, adapter {ADAPTER_FOR_MODE[mode]}.
module board_top(
    {clock_declaration}, input wire rst_n,
    input wire uart_rx, output wire uart_tx
);
    wire refclk, clk_raw, feedback_raw, feedback, clk, locked;
{clock_input}
    MMCME2_BASE #(.CLKIN1_PERIOD({1000.0/clock['reference_mhz']}),
                 .CLKFBOUT_MULT_F({clock['mmcm_mult']}), .DIVCLK_DIVIDE(1),
                 .CLKOUT0_DIVIDE_F({clock['mmcm_divide']})) clock_generator (
        .CLKIN1(refclk), .CLKFBIN(feedback), .CLKFBOUT(feedback_raw),
        .CLKOUT0(clk_raw), .LOCKED(locked), .PWRDWN(1'b0), .RST(~rst_n));
    BUFG feedback_buffer(.I(feedback_raw), .O(feedback));
    BUFG clock_buffer(.I(clk_raw), .O(clk));
    (* ASYNC_REG="TRUE" *) reg [2:0] reset_sync = 3'b000;
    wire reset_request = ~rst_n | ~locked;
    always @(posedge clk or posedge reset_request)
        if (reset_request) reset_sync <= 3'b000;
        else reset_sync <= {{reset_sync[1:0], 1'b1}};
    wire internal_rst_n = reset_sync[2];
    wire [7:0] rx_data, tx_data;
    wire rx_valid, tx_valid, tx_ready;
    wire [{geometry.in_words_bits - 1}:0] input_words;
    wire [{geometry.out_words_bits - 1}:0] output_words;
    wire start, done;
    uart_rx #(.CLK_FRE({core_mhz}), .BAUD_RATE({geometry.baudrate})) receiver(
        .clk(clk), .rst_n(internal_rst_n), .rx_pin(uart_rx),
        .rx_data(rx_data), .rx_data_valid(rx_valid), .rx_data_ready(1'b1));
    uart_tx #(.CLK_FRE({core_mhz}), .BAUD_RATE({geometry.baudrate})) transmitter(
        .clk(clk), .rst_n(internal_rst_n), .tx_pin(uart_tx),
        .tx_data(tx_data), .tx_data_valid(tx_valid), .tx_data_ready(tx_ready));
    cnn_uart_transport #({transport_parameters}) transport(
        .clk(clk), .rst_n(internal_rst_n),
        .uart_rx_data(rx_data), .uart_rx_valid(rx_valid),
        .uart_tx_data(tx_data), .uart_tx_valid(tx_valid), .uart_tx_ready(tx_ready),
        .input_words(input_words), .start(start), .done(done),
        .output_words(output_words));
{adapter}
    {record['top']} accelerator(
        {joined});
endmodule
"""


def render_xdc(facts: board_facts.BoardFacts) -> str:
    """Board facts, not model geometry: pins, standards and clock topology from the profile.

    Non-clock ports are grouped by their declared IOSTANDARD, so a board whose reset
    lives in a different bank (the Nexys Video's `cpu_resetn` is LVCMOS15 in the 1.5 V
    DDR3 bank, while its UART is LVCMOS33) is constrained per pin rather than in one
    blanket line that would only be right for one of them.
    """
    clock_ports = facts.clock_ports
    lines = ["set_property CFGBVS VCCO [current_design]",
             "set_property CONFIG_VOLTAGE 3.3 [current_design]"]
    lines += [f"set_property PACKAGE_PIN {pin} [get_ports {port}]"
              for port, pin in zip(clock_ports, facts.clock_pins)]
    lines.append(f"set_property IOSTANDARD {facts.clock_iostandard} "
                 f"[get_ports {{{' '.join(clock_ports)}}}]")
    lines.append(f"create_clock -name board_ref -period {1000.0/facts.reference_mhz:.3f} "
                 f"[get_ports {clock_ports[0]}]")
    lines.append(f"set_property PACKAGE_PIN {facts.reset_pin} [get_ports rst_n]")
    lines.append(f"set_property PACKAGE_PIN {facts.uart_rx_pin} [get_ports uart_rx]")
    lines.append(f"set_property PACKAGE_PIN {facts.uart_tx_pin} [get_ports uart_tx]")
    groups: dict[str, list[str]] = {}
    for port, standard in (("rst_n", facts.reset_iostandard),
                           ("uart_rx", facts.uart_iostandard),
                           ("uart_tx", facts.uart_iostandard)):
        groups.setdefault(standard, []).append(port)
    for standard, ports in groups.items():
        lines.append(f"set_property IOSTANDARD {standard} [get_ports {{{' '.join(ports)}}}]")
    lines += ["set_false_path -from [get_ports rst_n]",
              "set_false_path -from [get_ports uart_rx]",
              "set_false_path -to [get_ports uart_tx]"]
    header = (f"# Source: {facts.profile_path} ({facts.board_id}, {facts.part}). Board-facing\n"
              f"# pins, I/O standards and clock topology are read from this profile at build\n"
              f"# time; no pin is remembered in code.\n")
    return header + "\n".join(lines) + "\n"


def generate(source_wrapper: Path, output: Path) -> dict:
    source_wrapper = Path(source_wrapper).resolve()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output board directory is not empty: {output}")
    record = json.loads(source_wrapper.read_text(encoding="utf-8"))
    core = Path(record["core_verilog"])
    if sha256_file(core) != record["core_verilog_sha256"]:
        raise ValueError("source wrapper's core Verilog hash disagrees with the file")
    deployment = Path(record["contract"])
    contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
    model_path = (deployment.parent / contract["model"]["path"]).resolve()
    model_sha256 = sha256_file(model_path)
    run_dir = deployment.parents[2]
    starting_path = run_dir / "starting-state.json"
    if starting_path.is_file():
        starting = json.loads(starting_path.read_text(encoding="utf-8"))
        frozen = (starting.get("model_files") or {}).get(contract["model"]["path"], {}).get("sha256")
        if frozen != model_sha256:
            raise ValueError("model file differs from the bootstrap starting-state hash")
    profile_path = Path((record.get("board") or {}).get("pins_profile") or "")
    if not profile_path.is_file():
        raise ValueError(f"source wrapper names no readable board profile: {profile_path}")
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8-sig"))
    # Board facts are read from the profile the wrapper was built for, and the record's own
    # copy of them must agree. This is what lets a second board be added as data: there is no
    # board id to special-case, and a profile that leaves a pin, a standard or a clock
    # topology undeclared is refused rather than defaulted.
    facts = board_facts.load_facts(profile, profile_path)
    board_facts.require_recorded_board(record.get("board") or {}, facts)
    if record.get("part") != facts.part:
        raise ValueError(f"generated core targets {record.get('part')}, the board profile "
                         f"declares {facts.part}")
    reference_mhz = facts.reference_mhz
    if float(record["clock"]["reference_mhz"]) != float(reference_mhz):
        raise ValueError(f"generated core assumes a {record['clock']['reference_mhz']} MHz "
                         f"reference clock, the board profile declares {reference_mhz} MHz")
    expected_core_mhz = 1000.0 / float(contract["clock_period_ns"])
    if abs(float(record["clock"]["core_mhz"]) - expected_core_mhz) > 1e-6:
        raise ValueError(f"generated core runs at {record['clock']['core_mhz']} MHz, the "
                         f"contract's clock_period_ns derives {expected_core_mhz} MHz")
    if facts.core_frequency_mhz is not None and \
            abs(float(facts.core_frequency_mhz) - expected_core_mhz) > 1e-6:
        raise ValueError(f"the board profile {profile_path} declares core_frequency_mhz="
                         f"{facts.core_frequency_mhz}, the contract's clock_period_ns derives "
                         f"{expected_core_mhz} MHz")
    geometry = host_geometry.geometry_for_wrapper(deployment, contract, record,
                                                  board_profile=profile)
    ports = core_ports(core, record["top"])
    mode = validate_interface(record, ports, geometry)
    top_text, xdc_text = render_top(record, mode, geometry, facts), render_xdc(facts)
    extra = ["transport.v", "uart_rx.v", "uart_tx.v", ADAPTER_FOR_MODE[mode]]
    for name, digest in SOURCE_HASHES.items():
        if sha256_file(ASSETS / name) != digest:
            raise ValueError(f"verified RTL asset hash mismatch: {name}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "board_top.v").write_text(top_text, encoding="utf-8")
    (output / "board.xdc").write_text(xdc_text, encoding="utf-8")
    for name in extra:
        shutil.copyfile(ASSETS / name, output / name)
    # The host has no ROM, but its input element grid retains the producer's provenance.
    record["rom_image"] = {"grid": record["rom_image"]["grid"]}
    record.update({
        "skill": "board-host-io", "source_wrapper": str(source_wrapper),
        "source_wrapper_sha256": sha256_file(source_wrapper),
        "model_path": str(model_path), "model_sha256": model_sha256,
        "top_ports": list(facts.host_ports),
        "host_board_facts": facts.to_record(),
        "host_input_output_available": True,
        "input_source": "UART request payload",
        "outputs": ["board_top.v", "board.xdc", *extra],
        "notes": "Host UART board variant; no sample ROM. Hardware numerical verification is NOT_RUN.",
        "verification_scope": "host-path-built-unverified",
        "on_board_numerical": "NOT_RUN", "programmed": False,
        "hardware_verified": False, "extra_rtl": extra,
        "host_geometry": geometry.to_dict(),
        "host_protocol": host_geometry.protocol_record(geometry),
        "generated_sources": {name: sha256_file(output / name)
                              for name in ["board_top.v", "board.xdc", *extra]},
    })
    (output / "wrapper.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-wrapper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = generate(args.source_wrapper, args.output)
    print(json.dumps({"board": str(args.output), "top_ports": record["top_ports"],
                      "mode": record["interface"]["style"],
                      "host_protocol": record["host_protocol"],
                      "hardware_verified": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
