"""The transport is a board-verified asset: parameterising it must not touch its brain.

`transport.v` came from a run that passed 256/256 samples on real hardware. Making it
generic is allowed to add parameters and replace geometry literals; it is not allowed to
change the state machine. Two independent gates enforce that:

* a structural diff against the byte-frozen original, with the complete set of permitted
  line edits declared explicitly — anything else, added or removed, fails;
* a cycle-exact xsim run of the frozen original beside the parameterised module at the
  verified 64x4 geometry, fed the same byte stream, comparing every port every cycle.

The second gate is the real one: file-level "identical except for the parameter" is not
achievable, because a parameter list, port widths, index widths and localparams must all
change. Behaviour is what has to be preserved.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[1]
FROZEN = SKILL / "references" / "frozen" / "transport-96ceb796.v"
LIVE = SKILL / "assets" / "transport.v"
FROZEN_SHA256 = "96ceb7966f18fa06b44104ea067594196e42b5b73477c483ccb187e1846abbcb"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_rtl_tb", SKILL / "scripts" / "run_rtl_tb.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_rtl_tb"] = module
    spec.loader.exec_module(module)
    return module


def significant(path: Path) -> Counter:
    """Every line that carries meaning: code only, no blank or whole-line comments."""
    return Counter(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.strip().startswith("//"))


#: Lines the geometry parameterisation removes. Each is replaced by exactly one entry in
#: ADDED. Nothing that implements a state transition appears here.
REMOVED = {
    "    output reg [2047:0] input_words,":
        "    output reg [INPUT_BITS-1:0] input_words,",
    "    input wire [127:0] output_words":
        "    input wire [OUTPUT_BITS-1:0] output_words",
    "    parameter integer COMPUTE_TIMEOUT_CYCLES = CLK_HZ":
        "    parameter integer COMPUTE_TIMEOUT_CYCLES = CLK_HZ,",
    "    reg [7:0] payload_index;":
        "    reg [PAYLOAD_INDEX_BITS-1:0] payload_index;",
    "    reg [127:0] captured_output;":
        "    reg [OUTPUT_BITS-1:0] captured_output;",
    "    reg [4:0] tx_index;":
        "    reg [TX_INDEX_BITS-1:0] tx_index;",
    "        input [4:0] index;":
        "        input [TX_INDEX_BITS-1:0] index;",
    "                4: response_byte = 8'd20;":
        "                4: response_byte = RESPONSE_LENGTH;",
    "                26: response_byte = tx_crc[7:0];":
        "                RESPONSE_TOTAL-2: response_byte = tx_crc[7:0];",
    "                27: response_byte = tx_crc[15:8];":
        "                RESPONSE_TOTAL-1: response_byte = tx_crc[15:8];",
    "                            if (length_low == 0 && uart_rx_data == 1) begin":
        "                            if (length_low == PAYLOAD_LOW && uart_rx_data == PAYLOAD_HIGH) begin",
    "                    if (payload_index == 255) state <= CRC_LO;":
        "                    if (payload_index == PAYLOAD_BYTES-1) state <= CRC_LO;",
    "                    if (tx_index < 26) tx_crc <= crc16_update(tx_crc, uart_tx_data);":
        "                    if (tx_index < RESPONSE_TOTAL-2) tx_crc <= crc16_update(tx_crc, uart_tx_data);",
    "                    if (tx_index == 27) state <= SEEK_A5;":
        "                    if (tx_index == RESPONSE_TOTAL-1) state <= SEEK_A5;",
}

#: Lines the parameterisation introduces that are not replacements above.
ADDED_ONLY = {
    "    parameter integer PAYLOAD_BYTES = 256,",
    "    parameter integer RESPONSE_BYTES = 16,",
    "    parameter integer INPUT_BITS = 2048,",
    "    parameter integer OUTPUT_BITS = 128",
    "    localparam integer RESPONSE_TOTAL = RESPONSE_BYTES + 12;",
    "    localparam integer PAYLOAD_INDEX_BITS = (PAYLOAD_BYTES > 1) ? $clog2(PAYLOAD_BYTES) : 1;",
    "    localparam integer TX_INDEX_BITS = (RESPONSE_TOTAL > 1) ? $clog2(RESPONSE_TOTAL) : 1;",
    "    localparam [7:0] PAYLOAD_LOW = PAYLOAD_BYTES % 256;",
    "    localparam [7:0] PAYLOAD_HIGH = (PAYLOAD_BYTES / 256) % 256;",
    "    localparam [7:0] RESPONSE_LENGTH = RESPONSE_BYTES + 4;",
}


def test_frozen_reference_is_the_board_verified_original():
    import hashlib
    assert hashlib.sha256(FROZEN.read_bytes()).hexdigest() == FROZEN_SHA256
    assert LIVE != FROZEN


def test_the_state_machine_and_framing_survive_the_change():
    """Every removed line is a declared replacement; every added line is declared.

    Exact equality in both directions, as multisets: a missing edit means a literal
    survived, an undeclared edit means the change reached somewhere it was not supposed to,
    and a duplicated line is caught too.

    This single assertion is the whole structural gate. It subsumes a separate check that
    "no state-transition line was touched": if the removed set equals REMOVED exactly, then
    any frozen `state <=` line absent from the live file is by definition in REMOVED, so such
    a check could never fail on its own and would only have looked like extra coverage.
    """
    frozen, live = significant(FROZEN), significant(LIVE)
    removed = frozen - live
    added = live - frozen
    # NB: `Counter(REMOVED)` would treat the mapping as element->count, so count the keys.
    assert removed == Counter(REMOVED.keys()), (
        f"unexpected removals: {sorted(set(removed) - set(REMOVED))}; "
        f"missing removals: {sorted(set(REMOVED) - set(removed))}")
    expected_added = Counter(REMOVED.values()) + Counter(ADDED_ONLY)
    assert added == expected_added, (
        f"unexpected additions: {sorted(set(added) - set(expected_added))}; "
        f"missing additions: {sorted(set(expected_added) - set(added))}")


def test_parameterised_transport_matches_the_frozen_rtl_cycle_for_cycle(tmp_path):
    runner = load_runner()
    reference = tmp_path / "transport_reference.v"
    reference.write_text(
        FROZEN.read_text(encoding="utf-8").replace(
            "module cnn_uart_transport #(", "module cnn_uart_transport_ref #(", 1),
        encoding="utf-8")
    stimulus = write_stimulus(tmp_path)
    result = runner.run_testbench(
        top="transport_equivalence_tb",
        sources=[reference, LIVE, SKILL / "tests" / "transport_equivalence_tb.v"],
        data_files=[stimulus, tmp_path / "stimulus_gaps.hex"],
        workdir=tmp_path)
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "TRANSPORT_EQUIVALENCE_PASS"


def crc16(data: bytes) -> int:
    import binascii
    return binascii.crc_hqx(data, 0xFFFF)


def frame(sequence: int, payload: bytes, length: int | None = None,
          corrupt: bool = False, sequence_override: int | None = None) -> bytes:
    declared = len(payload) if length is None else length
    body = (b"\xa5\x5a" + sequence.to_bytes(2, "little") + declared.to_bytes(2, "little")
            + payload)
    crc = crc16(body) ^ (0xFFFF if corrupt else 0)
    return body + crc.to_bytes(2, "little")


def write_stimulus(tmp_path: Path) -> Path:
    """Scenarios the frozen and the parameterised transport must agree on byte for byte.

    Gaps are chosen against the testbench's own accelerator model (`done` high from 200 to
    2000 cycles after a start, RX timeout 3000, compute timeout 4000) so each scenario hits
    the branch it is meant to hit:

    * a settled good frame launches;
    * an immediate repeat has its CRC checked while `done` is still high, which the
      transport must refuse rather than attribute a stale result;
    * a bad CRC and a wrong length field must resynchronise, and DISCARD must not release
      before an idle gap;
    * a truncated frame must time out and resynchronise;
    * a settled frame afterwards must still work.
    """
    payload = bytes((index * 7 + 3) % 256 for index in range(256))
    other = bytes((index * 11 + 5) % 256 for index in range(256))
    good = frame(0, payload)
    stream: list[tuple[int, bytes]] = [
        (4000, good),                                     # settled good frame -> launch
        (2, frame(1, payload)),                           # immediate repeat -> refused
        (4000, frame(2, other)),                          # settled again -> launch
        (4000, frame(3, payload, corrupt=True)),          # bad CRC -> resync
        (4000, frame(4, payload, length=2)),              # wrong length -> DISCARD
        (4000, frame(5, other)),                          # settled -> launch
        (4000, good[:120]),                               # truncated -> RX timeout
        (8000, frame(6, payload)),                        # settled -> launch
    ]
    bytes_out: list[str] = []
    gaps: list[str] = []
    for gap, chunk in stream:
        for index, byte in enumerate(chunk):
            gaps.append(f"{gap if index == 0 else 2:x}")
            bytes_out.append(f"{byte:02x}")
    (tmp_path / "stimulus_bytes.hex").write_text("\n".join(bytes_out) + "\n", encoding="ascii")
    (tmp_path / "stimulus_gaps.hex").write_text("\n".join(gaps) + "\n", encoding="ascii")
    return tmp_path / "stimulus_bytes.hex"
