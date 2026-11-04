"""Full raw payloads stay required; optional header/counter diagnostics cannot reject values."""

import json
import struct
import sys

import pytest
from test_s15_numeric_policy import ROOT, load

sys.path.insert(0, str(ROOT / "board-host-io/tests"))
from geometry_fixtures import wrapper_record  # noqa: E402
from test_s15_chunk_transport import chunk_geometry, response_frame  # noqa: E402


def test_auto_framing_only_chunks_when_full_sample_exceeds_single_frame():
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    contract = {"precision": "ap_fixed<8,8>"}
    small = geo.geometry_from_contract(contract, wrapper=wrapper_record(7, 11, "ap_fixed<8,8>"))
    large = geo.geometry_from_contract(
        contract, wrapper=wrapper_record(66000, 66003, "ap_fixed<8,8>")
    )
    assert small.framing == "single" and large.framing == "chunked_v1"
    assert large.chunk_bytes == 4096


def test_single_frame_numeric_decode_records_bad_optional_fields_without_repairing_payload():
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    protocol = load("board-host-io/scripts/protocol.py", "diagnostic_protocol")
    geometry = geo.geometry_from_contract(
        {"precision": "ap_fixed<8,8>"}, wrapper=wrapper_record(1, 3, "ap_fixed<8,8>")
    )
    # Measured byte length is complete, but all optional metadata is deliberately wrong.
    frame = b"??" + struct.pack("<HHI", 99, 1, 0) + bytes([1, 254, 3]) + b"xx"
    diagnostics = []
    cycles, actual = protocol.decode_response(
        frame, 7, geometry, strict=False, on_diagnostic=diagnostics.append
    )
    assert cycles == 0 and actual == [1.0, -2.0, 3.0]
    assert len(diagnostics) == 5
    with pytest.raises(ValueError):
        protocol.decode_response(frame, 7, geometry)
    with pytest.raises(ValueError, match="partial"):
        protocol.decode_response(frame[:-1], 7, geometry, strict=False)


def test_chunk_numeric_decode_uses_known_frame_schedule_not_corrupt_length_offsets():
    geometry = chunk_geometry(7, 11, 3)
    protocol = load("board-host-io/scripts/protocol.py", "diagnostic_chunk_protocol")
    wire = []
    for end in [3, 6, 7]:
        wire.append(response_frame(23, end, 7, 0, b""))
    for offset in range(0, 11, 3):
        wire.append(response_frame(23, offset, 11, 0, bytes(range(11))[offset : offset + 3]))
    corrupted = []
    for frame in wire:
        bad = bytearray(frame)
        bad[2:6] = struct.pack("<HH", 99, 1)
        bad[6:14] = struct.pack("<II", 123, 456)
        bad[-1] ^= 1
        corrupted.append(bytes(bad))
    diagnostics = []
    count, values = protocol.decode_response(
        b"".join(corrupted), 23, geometry, strict=False, on_diagnostic=diagnostics.append
    )
    assert count == 0 and values == list(map(float, range(11))) and diagnostics
    with pytest.raises(ValueError, match="incomplete|partial|length"):
        protocol.decode_response(b"".join(corrupted)[:-1], 23, geometry, strict=False)


def test_capture_requests_numeric_only_protocol_mode_and_keeps_diagnostics(tmp_path, monkeypatch):
    import serial
    from test_attempt_persistence import capture_fixture, verify

    deployment, board, bit, scope, programmed, output = capture_fixture(monkeypatch, tmp_path)

    class Port:
        in_waiting = 0

        def open(self):
            pass

        def close(self):
            pass

    def exchange(*args, **kwargs):
        assert kwargs["strict_protocol"] is False
        kwargs["on_diagnostic"]({"field": "CRC", "scope": "software fixture only"})
        return b"", b"fixture", 0, [0.0]

    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(verify, "exchange", exchange)
    result = verify.capture(
        deployment, board, bit, scope, programmed, "SOFTWARE_STUB", "NOT_A_BOARD", output
    )
    assert result["received_samples"] == 256
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["kind"] == "protocol_diagnostic"]) == 256


@pytest.mark.parametrize("chunked", [False, True])
def test_actual_exchange_keeps_raw_payload_when_only_metadata_is_wrong(chunked):
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    protocol = load("board-host-io/scripts/protocol.py", "diagnostic_exchange")
    if chunked:
        geometry = chunk_geometry(7, 11, 3)
        frames = [response_frame(23, end, 7, 0, b"") for end in (3, 6, 7)]
        results = [
            response_frame(23, i, 11, 0, bytes(range(11))[i : i + 3]) for i in range(0, 11, 3)
        ]
        all_frames = []
        for frame in frames + results:
            bad = bytearray(frame)
            bad[2:6] = struct.pack("<HH", 99, 1)
            bad[-1] ^= 1
            all_frames.append(bytes(bad))
        ack_count = 3
        values = list(range(7))
        expected = list(map(float, range(11)))
    else:
        geometry = geo.geometry_from_contract(
            {"precision": "ap_fixed<8,8>"}, wrapper=wrapper_record(1, 3, "ap_fixed<8,8>")
        )
        all_frames = [b"??" + struct.pack("<HHI", 99, 1, 0) + bytes([1, 254, 3]) + b"xx"]
        ack_count = 1
        values = [0]
        expected = [1.0, -2.0, 3.0]

    class SoftwarePort:
        def __init__(self):
            self.pending = bytearray()
            self.index = 0

        @property
        def in_waiting(self):
            return len(self.pending)

        def write(self, frame):
            assert not self.pending
            if chunked:
                self.pending.extend(all_frames[self.index])
                self.index += 1
                if self.index == ack_count:
                    self.pending.extend(b"".join(all_frames[ack_count:]))
            else:
                self.pending.extend(all_frames[0])
            return len(frame)

        def read(self, count):
            value = bytes(self.pending[: min(count, 2)])
            del self.pending[: len(value)]
            return value

    diagnostics = []
    raw = []
    _, rx, count, actual = protocol.exchange(
        SoftwarePort(),
        values,
        23,
        geometry,
        strict_protocol=False,
        on_diagnostic=diagnostics.append,
        on_receive=raw.append,
    )
    assert actual == expected and count == 0 and diagnostics
    assert rx == b"".join(all_frames) == b"".join(raw)
