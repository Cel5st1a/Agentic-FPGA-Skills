#!/usr/bin/env python
"""vivado-manager: render a board wrapper for an hls4ml-generated RTL core.

The SKILL.md documents the board flow (project, synthesis, implementation, bitstream)
but the generated core has no board-facing interface of its own, and this project's
first target is a plain Artix-7 board with a 200 MHz differential reference clock.
This script bridges that gap generically:

  * it parses the core's port list out of the generated Verilog, so no model is assumed;
  * it implements the `ap_ctrl_hs` handshake that hls4ml's ap_vld array interface still
    carries. Leaving `ap_start` unconnected is not a cosmetic omission: the core never
    starts, its outputs stay constant, and synthesis removes the whole network
    (measured on this contract: 15 LUTs / 0 DSP against a CSynth estimate of 40794
    LUTs);
  * the top level exposes only pins the board profile can actually supply (clock pair,
    reset, one status LED). The model's outputs stay internal, are captured per sample
    and folded into a kept checksum, which is what keeps the logic alive in synthesis
    while the simulation testbench reads the captured words hierarchically;
  * clock pins and the LED pin/standard come from the board profile and its constraints
    file (manual-sourced); the reset pin is either declared by the profile or given on
    the command line with its provenance recorded. Pins are never guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "board-host-io" / "scripts"))
from line.geometry import declared_elements, flatten_sample, parse_precision as fixed_point, quantise, sample_rows
from line.generated_geometry import stream_facts
import board_facts

MODULE_RE = re.compile(r"^\s*module\s+(\w+)\s*\((.*?)\);", re.S | re.M)
DIRECTION_RE = re.compile(r"^\s*(input|output)\s*(\[[^\]]+\])?\s*([\w,\s]+);", re.M)
PIN_RE = re.compile(
    r"set_property\s+PACKAGE_PIN\s+(\w+)\s+\[get_ports\s+\{?(\w+)\}?\]", re.I
)
IOSTANDARD_RE = re.compile(
    r"set_property\s+IOSTANDARD\s+(\w+)\s+\[get_ports\s+\{?(\w+)\}?\]", re.I
)
CONTROL_PORTS = ("ap_start", "ap_done", "ap_ready", "ap_idle", "ap_ce")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_ports(verilog: str, top: str) -> dict:
    header = next((m for m in MODULE_RE.finditer(verilog) if m.group(1) == top), None)
    if header is None:
        raise SystemExit(f"module '{top}' not found in the generated Verilog")
    ports: dict[str, dict] = {}
    for match in DIRECTION_RE.finditer(verilog[header.end():]):
        direction, width, names = match.group(1), match.group(2), match.group(3)
        bits = 1
        if width:
            bounds = re.findall(r"\d+", width)
            if len(bounds) == 2:
                bits = int(bounds[0]) - int(bounds[1]) + 1
        for name in (n.strip() for n in names.split(",")):
            if name:
                ports[name] = {"direction": direction, "bits": bits}
    if not ports:
        raise SystemExit("no port declarations parsed; refusing to guess a wrapper")
    return ports


def load_samples(path: Path) -> list[list[float]]:
    return sample_rows(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def board_pins(profile: dict, profile_path: Path | None = None) -> dict:
    """The clock pins and reference frequency, read through the board-facts reader."""
    facts = board_facts.load_facts(profile, profile_path or Path("<profile>"))
    return {"clk_p": facts.clock_pins[0],
            "clk_n": facts.clock_pins[1] if len(facts.clock_pins) > 1 else None,
            "clk_mhz": facts.reference_mhz, "kind": facts.clock_kind}


def clock_topology(facts) -> dict:
    """Port declarations, input buffer and testbench fragments for this board's clock.

    The AX7A200B drives the design from a 200 MHz differential pair (``IBUFDS``) and the
    Nexys Video from a single-ended 100 MHz pin (``IBUF``). The MMCM stays in both paths,
    so ``~rst_n | ~locked`` keeps exactly the same meaning on both boards and nothing
    downstream has to know which one it is.
    """
    ports = facts.clock_ports
    half_period = round(500.0 / facts.reference_mhz, 4)
    if facts.clock_kind == "differential":
        return {
            "declarations": f"    input  wire {ports[0]},\n    input  wire {ports[1]},\n",
            "ibuf": f"    IBUFDS ref_input(.I({ports[0]}), .IB({ports[1]}), .O(refclk));",
            "tb_regs": f"    reg {ports[0]} = 0, {ports[1]} = 1, rst_n = 0;",
            "tb_toggle": (f"    always #{half_period} begin {ports[0]} <= ~{ports[0]}; "
                          f"{ports[1]} <= ~{ports[1]}; end"),
            "tb_dut": (f"    board_top dut(.{ports[0]}({ports[0]}), .{ports[1]}({ports[1]}), "
                       f".rst_n(rst_n), .led(led));"),
            "ref_port": ports[0], "ports": ports,
        }
    return {
        "declarations": f"    input  wire {ports[0]},\n",
        "ibuf": f"    IBUF ref_input(.I({ports[0]}), .O(refclk));",
        "tb_regs": f"    reg {ports[0]} = 0, rst_n = 0;",
        "tb_toggle": f"    always #{half_period} begin {ports[0]} <= ~{ports[0]}; end",
        "tb_dut": f"    board_top dut(.{ports[0]}({ports[0]}), .rst_n(rst_n), .led(led));",
        "ref_port": ports[0], "ports": ports,
    }


def xdc_clock_pin_lines(facts) -> str:
    return "\n".join(f"set_property PACKAGE_PIN {pin} [get_ports {port}]"
                     for port, pin in zip(facts.clock_ports, facts.clock_pins))


def xdc_clock_standard_lines(facts, standard: str) -> str:
    return "\n".join(f"set_property IOSTANDARD {standard} [get_ports {port}]"
                     for port in facts.clock_ports)


def xdc_create_clock(facts) -> str:
    return (f"create_clock -name board_ref -period {round(1000.0 / facts.reference_mhz, 4)} "
            f"[get_ports {facts.clock_ports[0]}]")


def board_top_ports(facts) -> list:
    """The ROM wrapper's port list: this board's clock ports, then reset and LED."""
    return [*facts.clock_ports, "rst_n", "led"]


def constraints_pins(path: Path) -> dict:
    """Pin/IOSTANDARD pairs a board constraints file already declares."""
    if not path or not Path(path).is_file():
        return {}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    pins = {name: {"pin": pin} for pin, name in PIN_RE.findall(text)}
    for standard, name in IOSTANDARD_RE.findall(text):
        pins.setdefault(name, {})["iostandard"] = standard
    return pins



def axis_lane_geometry(verilog_dir: Path, top: str, tdata_in: int, tdata_out: int,
                       in_elements: int | None, out_elements: int | None, element_bits: int) -> dict:
    """Read sample extents/types from generated sources and check physical lane widths.

    Optional count arguments are assertions only; never sources. The legacy element_bits
    argument is retained for API compatibility, but cannot supply a missing generated grid.
    """
    facts = stream_facts(verilog_dir, top)
    recorded_in, recorded_out = facts["input"]["elements"], facts["output"]["elements"]
    for side, asserted, actual in (("input", in_elements, recorded_in),
                                   ("output", out_elements, recorded_out)):
        if asserted is not None and asserted != actual:
            raise ValueError(f"interface.stream.{side}_elements: expected generated {actual}; actual {asserted!r}")
    lanes_in, lanes_out = facts["input"]["lanes"], facts["output"]["lanes"]
    if tdata_in % lanes_in or tdata_out % lanes_out:
        raise ValueError(f"interface.stream.TDATA: expected complete generated lanes {lanes_in}/{lanes_out}; actual {tdata_in}/{tdata_out} bits")
    lane_in, lane_out = tdata_in // lanes_in, tdata_out // lanes_out
    if lane_in != lane_out:
        raise ValueError(f"interface.stream.lane_bits: expected equal input/output slot widths; actual {lane_in}/{lane_out}")
    widest = max(facts["input"]["grid"]["total_bits"], facts["output"]["grid"]["total_bits"])
    if lane_in < widest:
        raise ValueError(f"interface.stream.lane_bits: expected at least {widest}; actual {lane_in}")
    if recorded_in % lanes_in or recorded_out % lanes_out:
        raise ValueError(f"interface.stream.sample_elements: expected whole beats of {lanes_in}/{lanes_out}; actual {recorded_in}/{recorded_out}")
    return {"lane_bits": lane_in, "input_lanes_per_beat": lanes_in,
            "output_lanes_per_beat": lanes_out,
            "input_beats_per_sample": recorded_in // lanes_in,
            "output_beats_per_sample": recorded_out // lanes_out,
            "input_elements": recorded_in, "output_elements": recorded_out,
            "input_element": facts["input"]["grid"], "output_element": facts["output"]["grid"],
            "source": "firmware_defines", "firmware_headers": facts["firmware"],
            "sample_geometry_source": facts["source"], "geometry_files": facts["files"]}


def render_axis_board(out_dir: Path, out_ctx: dict) -> dict:
    """Render the board wrapper for an AXI-Stream core (io_stream / `INTERFACE axis`).

    Why this exists: hls4ml's io_parallel interface makes Vitis HLS partition every
    intermediate array completely, and the tool stops progressing once the unrolled design
    passes ~1M instructions (measured on this line: 14k-23k instructions -> CSynth finished
    in 46-310 s; 1.0M-2.1M -> 0 % CPU and no log growth; DEFECTS D-048). io_stream
    synthesises, but its top is AXI-Stream, so the board wrapper has to be a stream master
    on the input and a stream slave on the output.

    Per sample: hold `ap_start` the way Vitis's own testbench does, drive `in_beats` input
    beats (each carrying `in_lanes` packed elements) from the sample ROM while the core
    accepts beats, accept output beats with TREADY held high, and every `out_beats` accepted
    beats pulse `stamp` so the testbench can close that sample's row. Accepted beats are
    folded into a kept checksum: the output stream has no board pin, and without something
    kept synthesis would remove the whole network.
    """
    clock, reset = out_ctx["clock"], out_ctx["reset"]
    topology = out_ctx["clock_topology"]
    assert clock and reset  # both are used by the port connections built at the call site
    in_name, in_vld, in_ready = out_ctx["in_name"], out_ctx["in_vld"], out_ctx["in_ready"]
    out_name, out_valid, out_ready = out_ctx["out_name"], out_ctx["out_valid"], out_ctx["out_ready"]
    in_elements, out_elements = out_ctx["in_elements"], out_ctx["out_elements"]
    elem_bits = out_ctx["elem_bits"]
    out_elem_bits = out_ctx["out_elem_bits"]
    tdata_in, tdata_out = out_ctx["tdata_bits"], out_ctx["tdata_out_bits"]
    in_lanes, out_lanes = out_ctx["in_lanes"], out_ctx["out_lanes"]
    lane_bits = out_ctx["lane_bits"]
    in_beats, out_beats = out_ctx["in_beats"], out_ctx["out_beats"]
    sample_count = out_ctx["sample_count"]
    index_bits = max(1, (sample_count - 1).bit_length())
    # Both counters must be able to *hold* their limit, not just reach limit-1: with
    # `(n - 1).bit_length()` bits the input index wrapped 1023 -> 0 and the wrapper kept
    # streaming the sample ROM forever, while the core started a second transaction as soon
    # as ap_done pulsed (measured: the frame never closed, out_count ran past its limit).
    in_index_bits = max(1, in_elements.bit_length())
    out_count_bits = max(1, out_elements.bit_length())
    in_bits = in_elements * elem_bits
    lane_pad = lane_bits - elem_bits
    # Verilog for "sign-extend the element into its TDATA slot". Built as a plain string
    # because brace escaping inside the f-string template is unreadable.
    lane_expr = ("lane_element" if lane_pad == 0 else
                 "{{" + str(lane_pad) + "{lane_element[ELEM_BITS-1]}}, lane_element}")
    # Per-lane source for the beat word when the ROM is beat-organised: lane j of the
    # presented beat is `rom_data_q[j]`, the registered word fetched for that element.
    lane_expr_rom = ("rom_data_q[lane]" if lane_pad == 0 else
                     "{{" + str(lane_pad) + "{rom_data_q[lane][ELEM_BITS-1]}}, rom_data_q[lane]}")
    # Address width for the beat-organised ROM: it holds SAMPLE_COUNT*IN_ELEMENTS words, so
    # it needs enough bits to hold that count (not just count-1).
    rom_addr_bits = max(1, (sample_count * in_elements).bit_length())
    core_conn = ",\n".join(f"        {item}" for item in out_ctx["core_connections"])

    (out_dir / "board_top.v").write_text(f"""// Generated by the vivado-manager skill for {out_ctx['board_id']} ({out_ctx['part']}).
// Core interface: AXI-Stream, ap_fixed<{out_ctx['total']},{out_ctx['integer']}> per element.
// One beat carries {in_lanes} input elements at a {lane_bits}-bit slot stride, so a sample is
// {in_beats} input beats and {out_beats} output beats ({out_lanes} elements per output beat).
// Board-facing pins are only the ones the profile can supply: clock, reset, status LED.
`timescale 1ns / 1ps

module board_top (
{topology['declarations']}    input  wire rst_n,
    output wire led
);
    localparam integer SAMPLE_COUNT   = {sample_count};
    localparam integer INPUT_BITS     = {in_bits};
    localparam integer IN_ELEMENTS    = {in_elements};
    localparam integer OUT_ELEMENTS   = {out_elements};
    localparam integer ELEM_BITS      = {elem_bits};
    localparam integer LANE_BITS      = {lane_bits};
    localparam integer IN_LANES       = {in_lanes};
    localparam integer OUT_LANES      = {out_lanes};
    localparam integer IN_BEATS       = {in_beats};
    localparam integer OUT_BEATS      = {out_beats};
    localparam integer IN_TDATA_BITS  = {tdata_in};
    localparam integer OUT_TDATA_BITS = {tdata_out};
    localparam integer START_DELAY    = 4;
    // Zero on the element grid, used to reset the registered ROM data.
    localparam [ELEM_BITS-1:0] ELEM_BITS_ZERO = 0;
    localparam [1:0] S_RUN = 2'd0, S_DONE = 2'd1;

    wire refclk, clk_raw, feedback_raw, feedback, clk, locked;
{topology['ibuf']}
    MMCME2_BASE #(.CLKIN1_PERIOD({out_ctx['refclk_period']}), .CLKFBOUT_MULT_F({out_ctx['mult']}),
                  .DIVCLK_DIVIDE(1), .CLKOUT0_DIVIDE_F({out_ctx['divide']})) clock_generator (
        .CLKIN1(refclk), .CLKFBIN(feedback), .CLKFBOUT(feedback_raw),
        .CLKOUT0(clk_raw), .LOCKED(locked), .PWRDWN(1'b0), .RST(~rst_n)
    );
    BUFG feedback_buffer(.I(feedback_raw), .O(feedback));
    BUFG clock_buffer(.I(clk_raw), .O(clk));

    (* ASYNC_REG = "TRUE" *) reg [2:0] reset_sync = 3'b000;
    wire reset_request = ~rst_n | ~locked;
    always @(posedge clk or posedge reset_request)
      if (reset_request) reset_sync <= 3'b000;
      else reset_sync <= {{reset_sync[1:0], 1'b1}};
    wire internal_rst = ~reset_sync[2];

    // Beat-organised sample ROM (see docs/deployment-recovery.md).
    // Storage is one ELEM_BITS-wide word per *element*, addressed by the element's global
    // index `sample_index*IN_ELEMENTS + in_index + lane`, instead of one IN_TDATA_BITS-wide
    // word per sample followed by a variable part-select.
    //
    // This is exactly the same data in exactly the same order: element i of sample s is
    // word s*IN_ELEMENTS+i, and the previous packing already put element i at bits
    // [i*ELEM_BITS +: ELEM_BITS] of sample s's wide word, which the wide read then selected
    // with `(in_index+lane)*ELEM_BITS`. Nothing about the beat sequence, the element order
    // or the handshake changes.
    //
    // The read is *registered* (address latched, data valid the next cycle) because a
    // synchronous single-port read is the pattern block RAM inference requires; an
    // asynchronous read of this array is what synthesis previously turned into ~48k LUT of
    // distributed selection, which made the design unroutable (measured: 235,413 node
    // overlaps, global congestion level 6). The one-cycle read latency is compensated by
    // driving TDATA from the registered data word and holding TVALID until the beat is
    // accepted, so the core still sees the same beats in the same order.
    reg [ELEM_BITS-1:0] beat_rom [0:SAMPLE_COUNT*IN_ELEMENTS-1];
    initial $readmemh("{out_ctx['mem_path']}", beat_rom);

    // Registered ROM data for each lane of the beat being presented, and a flag saying that
    // data is valid for the *current* `in_index`. The ROM read is synchronous (address now,
    // data next cycle), so a beat is presented only while `rom_dvalid_q` is set, and the
    // index advances only when the presented beat is accepted. This keeps the data and the
    // element pointer aligned: element i is presented exactly once, with element i's word.
    reg [ELEM_BITS-1:0] rom_data_q [0:IN_LANES-1];
    reg rom_dvalid_q = 1'b0;
    integer lane_init;
    initial for (lane_init = 0; lane_init < IN_LANES; lane_init = lane_init + 1)
        rom_data_q[lane_init] = ELEM_BITS_ZERO;

    reg [{index_bits - 1}:0] sample_index = 0;      // which ROM entry is being streamed
    reg [{in_index_bits - 1}:0] in_index = 0;       // first element of the beat on the wire
    reg [{out_count_bits - 1}:0] out_count = 0;     // beats within the current output row
    reg [{index_bits - 1}:0] row_index = 0;         // rows emitted so far
    reg [23:0] wait_cycles = 24'd0;
    reg [1:0] state = 2'd0;
    reg ap_start = 1'b0;
    reg done_r = 1'b0;
    reg stamp = 1'b0;
    reg s_tready = 1'b0;

    wire [IN_TDATA_BITS-1:0]  m_tdata;
    // TVALID is combinational from the registered valid flag so it can never be high for a
    // beat whose word has not been fetched yet. The run state permits feeding.
    wire                      m_tvalid = rom_dvalid_q && (state == S_RUN);
    wire                      m_tready;
    wire [OUT_TDATA_BITS-1:0] s_tdata;
    wire                      s_tvalid;
    wire ap_done, ap_idle, ap_ready;

    (* keep = "true" *) reg [OUT_TDATA_BITS-1:0] result_checksum = 0;

    // Synchronous single-port ROM read. The address is the element index of the beat that
    // is *going to be* presented; the data appears in `rom_data_q` one clock later, which
    // is why TVALID is asserted only once the data has been fetched.
    // Registered outputs are what let synthesis map this to block RAM rather than to
    // distributed selection logic.
    wire [{rom_addr_bits - 1}:0] rom_addr =
        (sample_index*IN_ELEMENTS + in_index);

    {out_ctx['top']} core (
{core_conn}
    );

    // One beat carries IN_LANES elements, element j of the beat sitting in slot j at
    // `j*LANE_BITS +: LANE_BITS` and right-aligned in it. drived as one packed word: this is
    // the layout Vitis's own RTL testbench drives (see axis_lane_geometry for the
    // measurement), and driving one element per beat instead put the element in lane 0 with
    // lanes 1..N-1 repeating it, and over-fed the core by a factor of N.
    //
    // The beat word comes from the registered ROM data (`rom_data_q`), i.e. the beat that
    // was addressed on the previous cycle. `rom_data_q` is itself the concatenation of the
    // per-lane registered words, so the whole word becomes valid one cycle after the
    // address is presented and stays stable until the index advances.
    wire [IN_TDATA_BITS-1:0] beat_word;
    genvar lane;
    generate
      for (lane = 0; lane < IN_LANES; lane = lane + 1) begin : input_lane
        assign beat_word[lane*LANE_BITS +: LANE_BITS] =
            {lane_expr_rom};
      end
    endgenerate

    // TDATA is driven from the registered ROM data word for the presented element;
    // TVALID is only high while that word is valid (see rom_dvalid_q above).
    assign m_tdata = beat_word;

    // Driven the way Vitis's own RTL testbench drives it (`myproject.autotb.v` and
    // `myproject_subsys_test_sequence_lib.sv` were read for this):
    //   * `ap_start` is raised once and then only dropped for a delta cycle between
    //     transactions (`#0; ap_start = 1; @(ap_ready); #0; ap_start = 0;` in a loop), so at
    //     every clock edge it is high and the core chains transactions back to back;
    //   * the input beats flow CONTINUOUSLY, independently of the start handshake: the beats
    //     a transaction consumes are the next IN_BEATS beats in stream order;
    //   * a transaction produces OUT_BEATS output beats, and TREADY is held high so none of
    //     them is ever refused.
    // The run that proves this is the tool's own: 100/100 transactions completed in xsim and
    // its RTL output stream was value-for-value identical to the C++ log.
    wire feeding = (state == S_RUN);

    // Synchronous ROM fetch followed by a one-beat handshake. `rom_dvalid_q` means
    // `rom_data_q` holds the word(s) for the current `in_index` and the beat may be
    // presented; while it is clear, the address for the current index is issued and the
    // data arrives next cycle. TVALID is only asserted while the data is valid, so a beat
    // can never be presented with another element's word. The index advances only on an
    // accepted beat, so every element is presented exactly once and in order.
    integer lane_fetch;
    always @(posedge clk) begin
      if (internal_rst) begin
        for (lane_fetch = 0; lane_fetch < IN_LANES; lane_fetch = lane_fetch + 1)
            rom_data_q[lane_fetch] <= ELEM_BITS_ZERO;
        rom_dvalid_q <= 1'b0;
        in_index <= 0;
        sample_index <= 0;
      end else if (feeding) begin
        if (rom_dvalid_q) begin
          if (m_tready) begin
            // Beat accepted: move to the next element of this sample, or to the next sample.
            rom_dvalid_q <= 1'b0;
            if (in_index + IN_LANES >= IN_ELEMENTS) begin
              in_index <= 0;
              if (sample_index != SAMPLE_COUNT - 1) sample_index <= sample_index + 1'b1;
            end else begin
              in_index <= in_index + IN_LANES;
            end
          end
        end else begin
          for (lane_fetch = 0; lane_fetch < IN_LANES; lane_fetch = lane_fetch + 1)
              rom_data_q[lane_fetch] <= beat_rom[rom_addr + lane_fetch];
          rom_dvalid_q <= 1'b1;
        end
      end else begin
        rom_dvalid_q <= 1'b0;
      end
    end

    always @(posedge clk) begin
      if (internal_rst) begin
        state <= S_RUN; row_index <= 0; out_count <= 0; wait_cycles <= 24'd0;
        ap_start <= 1'b0; done_r <= 1'b0; stamp <= 1'b0; s_tready <= 1'b0;
      end else begin
        stamp <= 1'b0;
        case (state)
          S_RUN: begin
            wait_cycles <= wait_cycles + 24'd1;
            if (wait_cycles >= START_DELAY - 1) ap_start <= 1'b1;
            s_tready <= 1'b1;
            if (s_tvalid && s_tready) begin
              result_checksum <= {{result_checksum[OUT_TDATA_BITS-2:0],
                                   result_checksum[OUT_TDATA_BITS-1]}} ^ s_tdata;
              // The row delimiter is this beat counter, not ap_done: one transaction is
              // exactly OUT_BEATS output beats, so the boundary is the same on every
              // sample and cannot drift with the core's internal latency.
              if (out_count == OUT_BEATS - 1) begin
                out_count <= 0;
                stamp <= 1'b1;
                if (row_index == SAMPLE_COUNT - 1) begin
                  state <= S_DONE; done_r <= 1'b1; s_tready <= 1'b0; ap_start <= 1'b0;
                end else begin
                  row_index <= row_index + 1'b1;
                end
              end else begin
                out_count <= out_count + 1'b1;
              end
            end
          end
          S_DONE: begin
            ap_start <= 1'b0;
            s_tready <= 1'b0;
            done_r <= 1'b1;
            state <= S_DONE;
          end
          default: state <= S_RUN;
        endcase
      end
    end

    assign led = done_r | result_checksum[0];
endmodule
""", encoding="utf-8")

    (out_dir / "board_top_tb.v").write_text(f"""// Board-level per-value testbench for an AXI-Stream core. Every accepted output beat
// contributes OUT_LANES values, each read out of its own slot at the element's own
// fixed-point width (the grid the HLS C++ log converts to); the row is closed when the
// wrapper reports that the sample's {out_beats} beats have arrived. A cycle budget bounds a
// stalled core.
`timescale 1ns / 1ps

module board_top_tb;
{topology['tb_regs']}
    wire led;
    // The wrapper's localparams are not visible at this scope, so the geometry the
    // testbench slices out of TDATA is declared again here. ELEM_BITS is the *output*
    // element width: the stream carries the element zero-extended inside its slot, so
    // slicing the contract's wider precision would read that padding as value.
    localparam integer ELEM_BITS    = {out_elem_bits};
    localparam integer LANE_BITS    = {lane_bits};
    localparam integer OUT_LANES    = {out_lanes};
    localparam integer OUT_BEATS    = {out_beats};
    localparam integer SAMPLE_COUNT = {sample_count};

{topology['tb_toggle']}

{topology['tb_dut']}

    integer results, samples, row_beats, lane, cycles;
    initial begin
        results = $fopen("board_results.log", "w");
        if (results == 0) begin
            $display("ERROR: cannot open board_results.log");
            $finish;
        end
        samples = 0;
        row_beats = 0;
        cycles = 0;
        #200 rst_n = 1;
        while (samples < SAMPLE_COUNT && cycles < 200000000) begin
            @(posedge dut.clk);
            cycles = cycles + 1;
            // Close the previous row before logging this cycle's beat: the wrapper pulses
            // `stamp` one cycle after the row's last beat was accepted, so on that cycle the
            // next row's first beat can already be on the wire. Without this order the row
            // boundary lands one beat late (measured earlier: rows came out 1025 and 1023
            // values wide instead of 1024).
            if (dut.stamp) begin
                if (row_beats != OUT_BEATS)
                    $display("ERROR: row %0d carried %0d beats, expected %0d",
                             samples, row_beats, OUT_BEATS);
                $fwrite(results, "\\n");
                samples = samples + 1;
                row_beats = 0;
            end
            if (dut.s_tvalid && dut.s_tready) begin
                for (lane = 0; lane < OUT_LANES; lane = lane + 1)
                    $fwrite(results, "%0d ",
                            $signed(dut.s_tdata[lane*LANE_BITS +: ELEM_BITS]));
                row_beats = row_beats + 1;
            end
        end
        $fclose(results);
        if (samples < SAMPLE_COUNT)
            $display("ERROR: only %0d of %0d samples completed", samples, SAMPLE_COUNT);
        $finish;
    end
endmodule
""", encoding="utf-8")

    return {
        "style": "axis_stream_with_ap_ctrl_hs",
        "input_port": in_name,
        "input_valid": in_vld,
        "input_ready": in_ready,
        "output_port": out_name,
        "output_valid": out_valid,
        "output_ready": out_ready,
        "stream": {"input_elements": in_elements, "output_elements": out_elements,
                   "element_bits": elem_bits, "output_element_bits": out_elem_bits,
                   "tdata_bits": tdata_in,
                   "output_tdata_bits": tdata_out, "lane_bits": lane_bits,
                   "input_lanes_per_beat": in_lanes, "output_lanes_per_beat": out_lanes,
                   "input_beats_per_sample": in_beats, "output_beats_per_sample": out_beats,
                   "lane_geometry_source": out_ctx["lane_geometry_source"],
                   "firmware_headers": out_ctx.get("firmware_headers")},
        "element_types": {"input": out_ctx["input_element"],
                          "output": out_ctx["output_element"]},
        "control": sorted(out_ctx["control"]),
        "per_sample_handshake": "ap_start is raised once and held high; the core chains "
                                "transactions, so a sample is delimited by its output beats",
        "row_delimiter": "the wrapper's own accepted-beat counter (one row per "
                         "output_beats_per_sample beats), not ap_done",
        "checksum": "result_checksum",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--verilog-dir", type=Path, required=True)
    parser.add_argument("--top", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--board-profile", type=Path, default=None,
                        help="profile carrying the clock pins when the contract's own does not")
    parser.add_argument("--reset-pin", default=None)
    parser.add_argument("--reset-iostandard", default=None)
    parser.add_argument("--reset-provenance", default=None,
                        help="where the reset pin comes from; recorded in the XDC and evidence")
    parser.add_argument("--led-port", default="LED1")
    parser.add_argument("--led-pin", default=None)
    parser.add_argument("--led-iostandard", default=None)
    parser.add_argument(
        "--clk-iostandard",
        default=None,
        help="clock I/O standard override; otherwise require the declared profile "
        "standard and provenance through board_facts (no constraints-name fallback).",
    )
    parser.add_argument("--clk-provenance", default=None)
    parser.add_argument("--input-elements", type=int, default=None,
                        help="elements per sample for an AXI-Stream input (one element per "
                             "cycle); required for io_stream cores, ignored for ap_vld")
    parser.add_argument("--output-elements", type=int, default=None,
                        help="beats that make one sample on the AXI-Stream output; required "
                             "as an optional assertion against the generated sample layout")
    parser.add_argument("--reference-json", type=Path,
                        help="generated reference sample table; otherwise use contract verification.references")
    parser.add_argument("--force", action="store_true",
                        help="regenerate this script's own outputs in a non-empty board directory")
    args = parser.parse_args()

    contract_path = args.deployment.resolve()
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8-sig"))
    verilog_dir = args.verilog_dir.resolve()
    top_file = verilog_dir / f"{args.top}.v"
    if not top_file.is_file():
        raise SystemExit(f"generated core not found: {top_file}")

    ports = parse_ports(top_file.read_text(encoding="utf-8", errors="replace"), args.top)
    clock = "ap_clk"
    if clock not in ports:
        raise SystemExit(f"generated core has no '{clock}' port: {sorted(ports)}")
    # The reset polarity follows the generated port: ap_vld cores carry an active-high
    # `ap_rst`, while the AXI-Stream cores carry `ap_rst_n`. Wiring the wrong one holds the
    # core in reset forever (or never resets it), so the polarity is taken from the name and
    # the connection is inverted accordingly.
    if "ap_rst" in ports:
        reset, reset_expression = "ap_rst", "internal_rst"
    elif "ap_rst_n" in ports:
        reset, reset_expression = "ap_rst_n", "~internal_rst"
    else:
        raise SystemExit(f"generated core has no reset port: {sorted(ports)}")
    control = {n: ports[n] for n in CONTROL_PORTS if n in ports}
    if "ap_start" not in control:
        raise SystemExit(
            "core has no ap_start port; this wrapper implements the ap_ctrl_hs handshake "
            f"only (ports seen: {sorted(ports)})"
        )
    reserved = {clock, reset, *control}
    data_inputs = [n for n, p in ports.items() if p["direction"] == "input" and n not in reserved]
    valid_inputs = [n for n in data_inputs if n.endswith("_ap_vld")]
    word_inputs = [n for n in data_inputs if n not in valid_inputs]
    outputs = [n for n, p in ports.items() if p["direction"] == "output" and n not in control]
    total, integer = fixed_point(contract["precision"])
    inputs_path = (contract_path.parent / contract["verification"]["inputs"]).resolve()
    samples = load_samples(inputs_path)
    width = len(samples[0])
    declared_shape = list((contract.get("model") or {}).get("input_shape") or [])
    if declared_shape and width != declared_elements(declared_shape):
        raise SystemExit(f"model.input_shape: expected {declared_elements(declared_shape)} "
                         f"values per sample; actual inputs width {width}")
    reference_entry = args.reference_json or (contract.get("verification") or {}).get("references")
    reference_rows = None
    if reference_entry:
        reference_path = (contract_path.parent / reference_entry).resolve()
        reference_rows = sample_rows(json.loads(reference_path.read_text(encoding="utf-8-sig")),
                                     f"references {reference_path}")

    # Two core interfaces exist in hls4ml 1.3.0, and which one appears depends on the
    # contract's io_type, not on the model:
    #   * io_parallel -> an `ap_vld` array port (one packed word per sample);
    #   * io_stream   -> AXI-Stream (`<name>_TDATA/_TVALID/_TREADY`, one element per cycle).
    # The second one is what makes the large image models synthesizable at all: with
    # io_parallel, hls4ml partitions every intermediate array completely, and Vitis HLS
    # 2025.2 stops progressing after the unroll phase once the design reaches ~1M
    # instructions (measured: 22k instructions -> CSynth completes in 46-310 s; 1.0-2.1M
    # -> 0 % CPU and no log growth; DEFECTS D-048).
    axis_input = next((n for n in data_inputs if n.endswith("_TDATA")), None)
    axis_output = next((n for n in outputs if n.endswith("_TDATA")), None)
    axis_output_valid = next((n for n in outputs if n.startswith(axis_output[:-6])
                              and n.endswith("_TVALID")), None) if axis_output else None
    axis_output_ready = next((n for n in data_inputs if n.startswith(axis_output[:-6])
                              and n.endswith("_TREADY")), None) if axis_output else None
    style = "axis" if axis_input and axis_output else "ap_vld"
    # The grid the sample ROM is quantised on and the grid the board log is read on. The
    # ap_vld path has always used the contract's precision for both; the AXI-Stream path
    # replaces them with the generated stream element types, which are not always equal to
    # the contract's precision (see axis_lane_geometry).
    rom_total, rom_integer, out_element_bits = total, integer, total

    if style == "ap_vld":
        if len(word_inputs) != 1 or len(valid_inputs) != 1 or not outputs:
            raise SystemExit(
                "unsupported interface for a generic board wrapper: "
                f"data inputs={word_inputs}, valid={valid_inputs}, outputs={outputs}. "
                "Only the ap_vld array interface (io_parallel) and AXI-Stream (io_stream) "
                "are implemented."
            )
        in_name, in_vld = word_inputs[0], valid_inputs[0]
        in_bits = ports[in_name]["bits"]
        if in_bits % total:
            raise SystemExit(f"input bus {in_bits} bits is not a multiple of {total}")
        n_in = in_bits // total
        # Status outputs (`*_ap_vld`) are captured for the checksum but are not result
        # columns: the C++ reference log carries only the data outputs, so comparing them
        # would fail on shape for a reason that has nothing to do with numerics.
        result_outputs = [n for n in outputs if not n.endswith("_ap_vld")]
        if not result_outputs:
            raise SystemExit(f"no result outputs to compare (saw {outputs})")
        for name in result_outputs:
            if ports[name]["bits"] != total:
                raise SystemExit(f"interface.output_bits.{name}: expected one {total}-bit "
                                 f"scalar port; actual {ports[name]['bits']}")
        if reference_rows is not None and len(reference_rows[0]) != len(result_outputs):
            raise SystemExit(f"references: expected {len(result_outputs)} result elements; "
                             f"actual {len(reference_rows[0])}")
    else:
        in_name = axis_input
        in_vld = next((n for n in data_inputs if n.startswith(axis_input[:-6]) and n.endswith("_TVALID")), None)
        if in_vld is None:
            raise SystemExit(f"axis input {axis_input} has no _TVALID partner (ports: {sorted(ports)})")
        if not any(n.endswith("_TREADY") and ports[n]["direction"] == "input" for n in outputs + data_inputs):
            raise SystemExit(
                f"axis output {axis_output} has no _TREADY partner (ports: {sorted(ports)})"
            )
        tdata_bits = ports[axis_input]["bits"]
        if tdata_bits < total:
            raise SystemExit(f"axis input TDATA is {tdata_bits} bits, narrower than the contract's {total}")
        geometry = axis_lane_geometry(verilog_dir, args.top, tdata_bits,
                                      ports[axis_output]["bits"], None, None, total)
        n_in = geometry["input_elements"]
        output_elements = geometry["output_elements"]
        if args.input_elements is not None and args.input_elements != n_in:
            raise SystemExit(f"--input-elements: expected generated sample width {n_in}; actual {args.input_elements}")
        if args.output_elements is not None and args.output_elements != output_elements:
            raise SystemExit(f"--output-elements: expected generated sample width {output_elements}; actual {args.output_elements}")
        if reference_rows is not None and len(reference_rows[0]) != output_elements:
            raise SystemExit(f"references: expected generated {output_elements} result elements; actual {len(reference_rows[0])}")
        in_bits = n_in * geometry["input_element"]["total_bits"]
        result_outputs = []
        tdata_out_bits = ports[axis_output]["bits"]
        # The ROM packs one sample on the *input element's* grid, because that is the type
        # the generated C++ testbench casts a float into, and the board log is read on the
        # *output element's* grid. Both come from the generated typedefs when available.
        rom_total = geometry["input_element"]["total_bits"]
        rom_integer = geometry["input_element"]["integer_bits"]
        out_element_bits = geometry["output_element"]["total_bits"]


    board_path = (contract_path.parent / contract["board_profile"]).resolve()
    profile = yaml.safe_load(board_path.read_text(encoding="utf-8-sig"))
    pins_profile_path, pins_profile, pins_source = board_path, profile, "contract"
    if args.board_profile is not None:
        pins_profile_path = args.board_profile.resolve()
        pins_profile = yaml.safe_load(pins_profile_path.read_text(encoding="utf-8-sig"))
        pins_source = "explicit --board-profile"
    try:
        pins = board_pins(pins_profile, pins_profile_path)
    except (SystemExit, ValueError) as exc:
        raise SystemExit(
            f"{exc}. The contract names {board_path}, which does not carry the clock pins. "
            "Pass --board-profile with a profile that does: the skills ship one under "
            "board-profiles/<BOARD>/board_profile.yaml, sourced from the board manual. "
            "Pins are never guessed."
        )
    part = (pins_profile.get("part") or (pins_profile.get("fpga") or {}).get("part")
            or profile.get("part") or (profile.get("fpga") or {}).get("part"))
    if not part:
        raise SystemExit(f"neither board profile declares a part: {board_path}, {pins_profile_path}")

    constraints_file = pins_profile_path.parent / "default_constraints.xdc"
    known_pins = constraints_pins(constraints_file)
    # Board facts come from the profile, through the same reader the host path uses: a
    # profile that leaves the clock topology, a pin or an I/O standard undeclared, or that
    # declares them without provenance, is refused rather than defaulted. The ROM wrapper
    # needs the clock and the reset; it has no UART, so it validates the whole profile and
    # uses the clock half.
    try:
        facts = board_facts.load_facts(pins_profile, pins_profile_path)
    except ValueError as exc:
        raise SystemExit(
            f"{exc}. The contract names {board_path}; pass --board-profile with a profile "
            "that declares the board-facing pins with provenance (the skills ship them "
            "under board-profiles/<BOARD>/board_profile.yaml). Pins are never guessed."
        )
    topology = clock_topology(facts)
    # A profile may declare the board-facing pins explicitly, with provenance. That is
    # what makes the step executable without a human retyping flags: the constraints
    # file ships RESET_N as TBD and can carry an IOSTANDARD the bank's VCCO rejects.
    # Command-line flags still win over the profile.
    declared = pins_profile.get("board_pins") or {}
    declared_clock = declared.get("clock") or {}
    declared_reset = declared.get("reset") or {}
    declared_led = declared.get("led") or {}
    led = known_pins.get(args.led_port, {})
    led_pin = args.led_pin or declared_led.get("pin") or led.get("pin")
    led_standard = (args.led_iostandard or declared_led.get("iostandard")
                    or led.get("iostandard"))
    led_source = ("--led-iostandard" if args.led_iostandard else
                  "profile board_pins" if declared_led.get("iostandard") else
                  f"{constraints_file}" if led.get("iostandard") else "default LVCMOS15")
    if not led_pin:
        raise SystemExit(
            f"no LED pin for port '{args.led_port}': not in {constraints_file} or the "
            "profile's board_pins block, and --led-pin was not given. The status LED is "
            "what keeps the model logic alive in synthesis, so it is required."
        )
    reset_pin = args.reset_pin or declared_reset.get("pin")
    reset_standard = args.reset_iostandard or declared_reset.get("iostandard") or "LVCMOS33"
    reset_standard_source = ("--reset-iostandard" if args.reset_iostandard else
                             "profile board_pins" if declared_reset.get("iostandard") else
                             "default LVCMOS33 (no profile value)")
    reset_pin_source = ("--reset-pin" if args.reset_pin else
                        "profile board_pins" if declared_reset.get("pin") else None)
    reset_provenance = args.reset_provenance or declared_reset.get("provenance")
    if not reset_pin:
        candidate = known_pins.get("RESET_N", {})
        reset_pin = candidate.get("pin")
        if reset_pin:
            reset_provenance = reset_provenance or f"{constraints_file} (RESET_N)"
    if not reset_pin:
        raise SystemExit(
            "no reset pin: neither the profile's board_pins block nor its constraints "
            "file declares one. Pass --reset-pin with --reset-provenance naming the "
            "manual or validated vendor IO report it comes from; a reset pin is never "
            "guessed."
        )

    # Clock IOSTANDARD/provenance and the reset-provenance comment are computed here rather
    # than next to the XDC write because both interface paths (ap_vld and AXI-Stream) render
    # their own constraints file.
    clk_standard = args.clk_iostandard or facts.clock_iostandard
    clk_provenance = args.clk_provenance or facts.provenance["clock"]
    clk_confirmation = declared_clock.get("requires_operator_confirmation")
    reset_provenance_line = (f"# Reset pin provenance: {reset_provenance}\n"
                            if reset_provenance else "")
    reset_provenance_line = f"# Reset pin provenance: {reset_provenance}\n" if reset_provenance else ""

    if width != n_in:
        raise SystemExit(f"contract inputs have {width} values per sample, core takes {n_in}")

    out_dir = args.output.resolve()
    own_outputs = ("board_top.v", "board_top_tb.v", "board.xdc", "input_words.mem", "wrapper.json")
    if out_dir.exists() and any(out_dir.iterdir()):
        if not args.force:
            raise SystemExit(
                f"board directory is not empty: {out_dir}. A rerun must not silently replace "
                "generated RTL, so pass --force to regenerate this script's own outputs "
                f"({', '.join(own_outputs)}) in place; other files are left alone."
            )
        for name in own_outputs:
            stale = out_dir / name
            if stale.is_file():
                stale.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    mem_lines = []
    # Only the AXI-Stream wrapper was changed to a beat-organised ROM; the ap_vld branch
    # still reads one wide word per sample, so the .mem must match the interface in use.
    interface_is_stream = (style == "axis")
    for row in samples:
        words = []
        for index, value in enumerate(row):
            # Element i of an hls4ml array port occupies bits [i*W +: W]: the first
            # element is the LOW word. Packing it MSB-first reverses the input vector,
            # which leaves the network computing on the wrong features (measured: board
            # and C++ disagreed at 490/500 values with a valid-looking magnitude).
            field = quantise(value, rom_total, rom_integer) & ((1 << rom_total) - 1)
            words.append(field)
        # The sample ROM is written in the shape the interface's generated RTL reads:
        #   * AXI-Stream (io_stream): one ELEM_BITS-wide word per element, in element
        #     order, because the wrapper's beat-organised ROM is addressed by element
        #     index. See docs/deployment-recovery.md.
        #   * anything else (the ap_vld array path, io_parallel): one wide word per
        #     sample, exactly as the unchanged ap_vld wrapper reads it. This branch is NOT
        #     changed by the stream packaging work and must keep its wide encoding.
        if interface_is_stream:
            mem_lines.extend(f"{word:0{(rom_total + 3) // 4}x}" for word in words)
        else:
            word = 0
            for index, field in enumerate(words):
                word |= field << (rom_total * index)
            mem_lines.append(f"{word:0{(n_in * rom_total + 3) // 4}x}")
    mem_path = out_dir / "input_words.mem"
    mem_path.write_text("\n".join(mem_lines) + "\n", encoding="utf-8")

    sample_count = len(samples)
    index_bits = max(1, (sample_count - 1).bit_length())
    ref_mhz = pins["clk_mhz"]
    core_mhz = 1000.0 / float(contract["clock_period_ns"])
    mult = round(core_mhz * 5.0 / ref_mhz, 3)
    vco = ref_mhz * mult
    while vco < 600.0:
        mult *= 2.0
        vco = ref_mhz * mult
    while vco > 1200.0:
        mult /= 2.0
        vco = ref_mhz * mult
    divide = round(vco / core_mhz, 3)

    if style == "axis":
        interface_record = render_axis_board(out_dir, {
            "clock": clock, "reset": reset, "top": args.top,
            "clock_topology": topology,
            "in_name": in_name, "in_vld": in_vld,
            "in_ready": next((n for n in ports if n.startswith(in_name[:-6]) and n.endswith("_TREADY")), None),
            "out_name": axis_output,
            "out_valid": axis_output_valid,
            "out_ready": axis_output_ready,
            "in_elements": n_in, "out_elements": output_elements,
            "elem_bits": geometry["input_element"]["total_bits"],
            "out_elem_bits": out_element_bits,
            "input_element": geometry["input_element"],
            "output_element": geometry["output_element"],
            "integer": integer, "total": total,
            "tdata_bits": ports[axis_input]["bits"],
            "tdata_out_bits": tdata_out_bits,
            "in_lanes": geometry["input_lanes_per_beat"],
            "out_lanes": geometry["output_lanes_per_beat"],
            "lane_bits": geometry["lane_bits"],
            "in_beats": geometry["input_beats_per_sample"],
            "out_beats": geometry["output_beats_per_sample"],
            "lane_geometry_source": geometry["source"],
            "firmware_headers": geometry["firmware_headers"],
            "sample_count": sample_count,
            "board_id": pins_profile.get("id") or profile.get("id"), "part": part,
            "refclk_period": round(1000.0 / ref_mhz, 4), "mult": mult, "divide": divide,
            "mem_path": mem_path.as_posix(),
            "control": control,
            "core_connections": [f".{clock}(clk)",
                                 f".{reset}({reset_expression})",
                                 f".{in_name}(m_tdata)", f".{in_vld}(m_tvalid)",
                                 f".{in_name[:-6]}_TREADY(m_tready)",
                                 f".{axis_output}(s_tdata)",
                                 f".{axis_output_valid}(s_tvalid)",
                                 f".{axis_output_ready}(s_tready)",
                                 ".ap_start(ap_start)", ".ap_done(ap_done)",
                                 ".ap_idle(ap_idle)", ".ap_ready(ap_ready)"],
        })
        (out_dir / "board.xdc").write_text(f"""# Generated from the board profile: {pins_profile_path}
# Pins come from the profile's board_pins block when it declares them, otherwise from
# its constraints file: {constraints_file}
# Clock pair IOSTANDARD provenance: {clk_provenance}
{reset_provenance_line}set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
{xdc_clock_pin_lines(facts)}
{xdc_clock_standard_lines(facts, clk_standard)}
{xdc_create_clock(facts)}
set_property PACKAGE_PIN {reset_pin} [get_ports rst_n]
set_property IOSTANDARD {reset_standard} [get_ports rst_n]
set_false_path -from [get_ports rst_n]
set_property PACKAGE_PIN {led_pin} [get_ports led]
set_property IOSTANDARD {led_standard or 'LVCMOS15'} [get_ports led]
""", encoding="utf-8")
        record = {
            "schema_version": 1,
            "skill": "vivado-manager",
            "contract": str(contract_path),
            "contract_sha256": sha256_file(contract_path),
            "core_verilog": str(top_file),
            "core_verilog_sha256": sha256_file(top_file),
            "top": args.top,
            "part": part,
            "interface": interface_record,
            "precision": contract["precision"],
            "samples": sample_count,
            "geometry_files": geometry["geometry_files"],
            "output_layout": {"flatten": "c_order", "elements": output_elements,
                              "source": "generated_bridge"},
            "input_layout": {"flatten": "c_order", "elements": n_in,
                             "source": "generated_bridge", "declared_shape": declared_shape},
            "clock": {"reference_mhz": ref_mhz, "core_mhz": round(core_mhz, 4),
                      "mmcm_mult": mult, "mmcm_divide": divide,
                      "vco_mhz": round(ref_mhz * mult, 2)},
            "board": {"contract_profile": str(board_path), "pins_profile": str(pins_profile_path),
                      "pins_source": pins_source, "pins": pins,
                      "part": part, "constraints_file": str(constraints_file),
                      "led": {"port": args.led_port, "pin": led_pin,
                              "iostandard": led_standard or "LVCMOS15",
                              "iostandard_source": led_source},
                      "clock_pair": {"iostandard": clk_standard, "provenance": clk_provenance,
                                     "iostandard_source": ("--clk-iostandard" if args.clk_iostandard else
                                                           "profile board_pins" if declared_clock.get("iostandard")
                                                           else f"{constraints_file}"),
                                     "requires_operator_confirmation": clk_confirmation},
                      "reset": {"pin": reset_pin, "iostandard": reset_standard,
                                "pin_source": reset_pin_source,
                                "iostandard_source": reset_standard_source,
                                "provenance": reset_provenance}},
            "top_ports": board_top_ports(facts),
            "rom_image": {"path": str(mem_path), "sha256": sha256_file(mem_path),
                          "layout": "one ELEM_BITS-wide word per element, "
                                    "address = sample_index*input_elements + element_index",
                          "words": len(mem_lines),
                          "quantisation": "floor_toward_minus_infinity",
                          "quantisation_note": "matches the ap_types cast the generated C++ "
                                               "testbench applies when it assigns a float to "
                                               "the ap_fixed port, so both sides of the board "
                                               "gate receive the same input values",
                          "grid": {"total_bits": rom_total, "integer_bits": rom_integer,
                                   "source": geometry["input_element"]["source"]}},
            "output_grid": {"total_bits": out_element_bits,
                            "integer_bits": geometry["output_element"]["integer_bits"],
                            "source": geometry["output_element"]["source"],
                            "note": "the board log carries the output stream's element, so it is "
                                    "scaled on this grid and not on the contract precision"},
            "outputs": ["board_top.v", "board_top_tb.v", "board.xdc", "input_words.mem"],
            "notes": [
                "the model's output stream has no board pin; accepted beats are folded into a "
                "kept checksum and the testbench reads them as they arrive",
                "ap_start is raised once after reset and held high, matching the sequence "
                "Vitis's own RTL testbench uses; the core then chains transactions back to back",
                "one AXI-Stream beat carries several elements packed into fixed slots; the "
                "count and slot width are recorded under interface.stream and come from the "
                "generated C++ stream types, or from the TDATA widths when those are absent",
            ],
        }
        (out_dir / "wrapper.json").write_text(json.dumps(record, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    wire_decl = "\n".join(f"    wire [{ports[n]['bits'] - 1}:0] {n};" for n in outputs)
    reg_decl = "\n".join(
        f"    (* keep = \"true\" *) reg [{ports[n]['bits'] - 1}:0] {n}_r = 0;" for n in outputs
    )
    connections = [f".{clock}(clk)", f".{reset}({reset_expression})",
                   f".{in_name}(input_word)", f".{in_vld}(input_vld)",
                   ".ap_start(ap_start)"]
    if "ap_ce" in control:
        connections.append(".ap_ce(1'b1)")
    connections += [f".{n}({n})" for n in outputs]
    if "ap_done" in control:
        connections.append(".ap_done(ap_done)")
    if "ap_ready" in control:
        connections.append(".ap_ready(ap_ready)")
    if "ap_idle" in control:
        connections.append(".ap_idle(ap_idle)")
    core_conn = ",\n".join(f"        {item}" for item in connections)
    capture_lines = "\n".join(f"                {n}_r <= {n};" for n in outputs)
    checksum_inputs = " ^ ".join(f"{n}_r" for n in outputs)
    tb_reads = ",\n".join(f"            $signed(dut.{n}_r)" for n in result_outputs)
    tb_formats = " ".join("%0d" for _ in result_outputs)

    (out_dir / "board_top.v").write_text(f"""// Generated by the vivado-manager skill for {pins_profile.get('id') or profile.get('id')} ({part}).
// Core interface: ap_vld array with an ap_ctrl_hs control port; one packed {in_bits}-bit
// input word ({n_in} x ap_fixed<{total},{integer}>), {len(outputs)} result outputs.
// Board-facing pins are only the ones the profile can supply: clock pair, reset, status LED.
`timescale 1ns / 1ps

module board_top (
{topology['declarations']}    input  wire rst_n,
    output wire led
);
    localparam integer SAMPLE_COUNT = {sample_count};
    localparam integer INPUT_BITS   = {in_bits};

    wire refclk, clk_raw, feedback_raw, feedback, clk, locked;
{topology['ibuf']}
    MMCME2_BASE #(.CLKIN1_PERIOD({round(1000.0 / ref_mhz, 4)}), .CLKFBOUT_MULT_F({mult}),
                  .DIVCLK_DIVIDE(1), .CLKOUT0_DIVIDE_F({divide})) clock_generator (
        .CLKIN1(refclk), .CLKFBIN(feedback), .CLKFBOUT(feedback_raw),
        .CLKOUT0(clk_raw), .LOCKED(locked), .PWRDWN(1'b0), .RST(~rst_n)
    );
    BUFG feedback_buffer(.I(feedback_raw), .O(feedback));
    BUFG clock_buffer(.I(clk_raw), .O(clk));

    (* ASYNC_REG = "TRUE" *) reg [2:0] reset_sync = 3'b000;
    wire reset_request = ~rst_n | ~locked;
    always @(posedge clk or posedge reset_request)
      if (reset_request) reset_sync <= 3'b000;
      else reset_sync <= {{reset_sync[1:0], 1'b1}};
    wire internal_rst = ~reset_sync[2];

    reg [INPUT_BITS-1:0] sample_rom [0:SAMPLE_COUNT-1];
    initial $readmemh("{mem_path.as_posix()}", sample_rom);

    reg [{index_bits - 1}:0] sample_index = 0;
    reg [INPUT_BITS-1:0]     input_word = 0;
    reg                      input_vld = 1'b0;
    reg                      ap_start = 1'b0;
    reg                      done_r = 1'b0;
    reg                      stamp = 1'b0;
    reg [1:0]                state = 2'd0;
    reg [3:0]                start_hold = 4'd0;
    reg [23:0]               wait_cycles = 24'd0;
    localparam [1:0] S_READY = 2'd3, S_LOAD = 2'd0, S_RUN = 2'd1, S_CAPTURE = 2'd2;

{wire_decl}
    wire ap_done, ap_ready, ap_idle;

    {args.top} core (
{core_conn}
    );

{reg_decl}
    (* keep = "true" *) reg [31:0] result_checksum = 32'd0;

    // ap_ctrl_hs: pulse ap_start, then wait for ap_done before capturing. The wait is
    // bounded so a stalled core ends the run instead of hanging the simulation.
    always @(posedge clk) begin
      if (internal_rst) begin
        state <= S_READY; sample_index <= 0; input_word <= 0; input_vld <= 1'b0;
        ap_start <= 1'b0; done_r <= 1'b0; stamp <= 1'b0; wait_cycles <= 24'd0;
        start_hold <= 4'd0;
      end else begin
        stamp <= 1'b0;
        case (state)
          S_READY: begin
            // Start from idle, then hold ap_start until the core answers with ap_ready.
            // This core drives ap_ready from its first dataflow stage, so it is low until
            // a start is presented: a one-cycle pulse from reset left the transaction
            // waiting forever (measured: ap_done low for 5M cycles).
            if (ap_idle) begin
              input_word <= sample_rom[sample_index];
              input_vld  <= 1'b1;
              ap_start   <= 1'b1;
              start_hold <= 4'd12;
              wait_cycles <= 24'd0;
              state      <= S_LOAD;
            end
          end
          S_LOAD: begin
            // The generated top wires the first dataflow stage's ap_start straight to the
            // top-level ap_start (`assign config2_U0_ap_start = ap_start`), so it is a
            // level that must stay high until the transaction reports done; releasing it
            // early left the core idle with a valid input (measured: ap_idle=1, ap_done=0
            // for 5M cycles after a 12-cycle pulse).
            state <= S_RUN;
          end
          S_RUN: begin
            wait_cycles <= wait_cycles + 24'd1;
            if (ap_done || wait_cycles == 24'hFFFFFF) state <= S_CAPTURE;
          end
          S_CAPTURE: begin
            ap_start  <= 1'b0;
            input_vld <= 1'b0;
            stamp <= 1'b1;
{capture_lines}
            result_checksum <= {{result_checksum[30:0], 1'b0}} ^ ({checksum_inputs});
            if (sample_index == SAMPLE_COUNT - 1) begin
              done_r <= 1'b1;
              state  <= S_CAPTURE;
            end else begin
              sample_index <= sample_index + 1'b1;
              state <= S_READY;
            end
          end
          default: state <= S_READY;
        endcase
      end
    end

    assign led = done_r | result_checksum[0];
endmodule
""", encoding="utf-8")

    (out_dir / "board_top_tb.v").write_text(f"""// Board-level per-value testbench. It waits for one capture pulse per sample and writes
// the captured result words as signed raw integers on the contract's fixed-point grid --
// the same grid the HLS C++ log converts to. The wires are read hierarchically because
// the board top deliberately exposes only board-legal pins.
`timescale 1ns / 1ps

module board_top_tb;
{topology['tb_regs']}
    wire led;

{topology['tb_toggle']}

{topology['tb_dut']}

    integer results, i;
    initial begin
        results = $fopen("board_results.log", "w");
        if (results == 0) begin
            $display("ERROR: cannot open board_results.log");
            $finish;
        end
        #200 rst_n = 1;
        for (i = 0; i < {sample_count}; i = i + 1) begin
            @(posedge dut.stamp);
            $fwrite(results, "{tb_formats}\\n",
{tb_reads});
        end
        $fclose(results);
        $display("board_results written: {sample_count} samples");
        $finish;
    end

    initial begin
        #50ms;
        $display("ERROR: board testbench timed out: locked=%b internal_rst=%b state=%0d sample_index=%0d ap_start=%b ap_done=%b ap_ready=%b ap_idle=%b input_vld=%b stamp=%b done=%b input_lo=%h",
                 dut.locked, dut.internal_rst, dut.state, dut.sample_index,
                 dut.ap_start, dut.ap_done, dut.ap_ready, dut.ap_idle,
                 dut.input_vld, dut.stamp, dut.done_r, dut.input_word[19:0]);
        $finish;
    end
endmodule
""", encoding="utf-8")

    provenance = reset_provenance_line
    (out_dir / "board.xdc").write_text(f"""# Generated from the board profile: {pins_profile_path}
# Pins come from the profile's board_pins block when it declares them, otherwise from
# its constraints file: {constraints_file}
# Clock pair IOSTANDARD provenance: {clk_provenance}
{provenance}set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
{xdc_clock_pin_lines(facts)}
# Applied per port: a braced pair list left both clock ports at IOSTANDARD DEFAULT and
# the resulting NSTD-1 critical warning blocked write_bitstream.
{xdc_clock_standard_lines(facts, clk_standard)}
{xdc_create_clock(facts)}
set_property PACKAGE_PIN {reset_pin} [get_ports rst_n]
set_property IOSTANDARD {reset_standard} [get_ports rst_n]
set_false_path -from [get_ports rst_n]
set_property PACKAGE_PIN {led_pin} [get_ports led]
set_property IOSTANDARD {led_standard or 'LVCMOS15'} [get_ports led]
""", encoding="utf-8")

    record = {
        "schema_version": 1,
        "skill": "vivado-manager",
        "contract": str(contract_path),
        "contract_sha256": sha256_file(contract_path),
        "core_verilog": str(top_file),
        "core_verilog_sha256": sha256_file(top_file),
        "top": args.top,
        "part": part,
        "interface": {"style": "ap_vld_array_with_ap_ctrl_hs", "input": in_name, "valid": in_vld,
                      "input_bits": in_bits, "elements": n_in, "control": sorted(control),
                      "outputs": outputs, "result_outputs": result_outputs,
                      "output_bits": {n: ports[n]["bits"] for n in outputs}},
        "precision": contract["precision"],
        "samples": sample_count,
        "output_layout": {"flatten": "c_order", "elements": len(result_outputs),
                          "ports": result_outputs, "source": "interface.result_outputs"},
        "input_layout": {"flatten": "c_order", "elements": n_in, "declared_shape": declared_shape},
        "clock": {"reference_mhz": ref_mhz, "core_mhz": round(core_mhz, 4),
                  "mmcm_mult": mult, "mmcm_divide": divide, "vco_mhz": round(ref_mhz * mult, 2)},
        "board": {"contract_profile": str(board_path), "pins_profile": str(pins_profile_path),
                  "pins_source": pins_source, "pins": pins, "part": part,
                  "constraints_file": str(constraints_file),
                  "led": {"port": args.led_port, "pin": led_pin,
                          "iostandard": led_standard or "LVCMOS15",
                          "iostandard_source": led_source},
                  "clock_pair": {"iostandard": clk_standard, "provenance": clk_provenance,
                                 "iostandard_source": ("--clk-iostandard" if args.clk_iostandard else
                                                       "profile board_pins" if declared_clock.get("iostandard")
                                                       else f"{constraints_file}"),
                                 "requires_operator_confirmation": clk_confirmation},
                  "reset": {"pin": reset_pin, "iostandard": reset_standard,
                            "pin_source": reset_pin_source,
                            "iostandard_source": reset_standard_source,
                            "provenance": reset_provenance}},
        "top_ports": board_top_ports(facts),
        "rom_image": {"path": str(mem_path), "sha256": sha256_file(mem_path),
                      "quantisation": "floor_toward_minus_infinity",
                      "quantisation_note": "matches the ap_types cast the generated C++ "
                                           "testbench applies when it assigns a float to "
                                           "the ap_fixed port, so both sides of the board "
                                           "gate receive the same input values",
                      "grid": {"total_bits": rom_total, "integer_bits": rom_integer,
                               "source": "contract_precision"}},
        "output_grid": {"total_bits": out_element_bits, "integer_bits": rom_integer,
                        "source": "contract_precision",
                        "note": "the ap_vld result ports carry the contract's precision"},
        "outputs": ["board_top.v", "board_top_tb.v", "board.xdc", "input_words.mem"],
        "notes": [
            "the model's result words stay internal and are captured per sample; the "
            "testbench reads them hierarchically, and a kept checksum keeps them alive "
            "in synthesis",
            "ap_start is driven: leaving it unconnected removes the whole network",
        ],
    }
    (out_dir / "wrapper.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
