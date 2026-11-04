"""Complete sample chunking; synthetic data never establishes model/board evidence."""

import hashlib
import json
import struct
import sys

import pytest
from test_s15_numeric_policy import ROOT, load

sys.path.insert(0, str(ROOT / "board-host-io/tests"))
from geometry_fixtures import facts, wrapper_record  # noqa: E402


def chunk_geometry(in_count=66000, out_count=66003, chunk=1024):
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    contract = {
        "precision": "ap_fixed<8,8>",
        "host_transport": {"framing": "chunked_v1", "chunk_bytes": chunk},
    }
    record = wrapper_record(in_count, out_count, "ap_fixed<8,8>")
    return geo.geometry_from_contract(contract, wrapper=record)


def response_frame(sequence, offset, total, cycles, data):
    protocol = load("board-host-io/scripts/protocol.py", "chunk_response_protocol")
    body = (
        b"\x6a\xa6" + struct.pack("<HHIII", sequence, 12 + len(data), offset, total, cycles) + data
    )
    return body + struct.pack("<H", protocol.crc16(body))


def test_chunk_codec_preserves_complete_large_samples_and_tail():
    geometry = chunk_geometry()
    assert geometry.framing == "chunked_v1"
    codec = load("board-host-io/scripts/chunk_protocol.py", "chunk_codec")
    protocol = load("board-host-io/scripts/protocol.py", "chunk_public_protocol")
    values = [(i % 255) - 127 for i in range(geometry.in_elements)]
    requests = codec.request_frames(values, 37, geometry)
    assert len(requests) == 65
    assert len(requests[-1]) == 16 + 464
    assert protocol.encode_request(values, 37, geometry) == b"".join(requests)
    offsets = [
        min((i + 1) * geometry.chunk_bytes, geometry.payload_bytes) for i in range(len(requests))
    ]
    acks = [response_frame(37, end, geometry.payload_bytes, 0, b"") for end in offsets]
    raw = bytes((i * 17 + 9) % 256 for i in range(geometry.response_bytes))
    frames = [
        response_frame(37, i, len(raw), 29, raw[i : i + geometry.chunk_bytes])
        for i in range(0, len(raw), geometry.chunk_bytes)
    ]
    cycles, received = protocol.decode_response(b"".join(acks + frames), 37, geometry)
    assert cycles == 29 and len(received) == 66003
    assert received == [float(x if x < 128 else x - 256) for x in raw]
    for broken in [
        acks[:-1] + frames,
        acks + frames[1:],
        acks + frames + frames[-1:],
        acks + frames[::-1],
    ]:
        with pytest.raises(ValueError):
            protocol.decode_response(b"".join(broken), 37, geometry)
    corrupt = bytearray(b"".join(acks + frames))
    corrupt[-1] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        protocol.decode_response(bytes(corrupt), 37, geometry)


@pytest.mark.parametrize("fault", ["crc", "sequence", "offset", "total"])
def test_chunk_rtl_complete_large_sample_and_crc_recovery(tmp_path, fault):
    geometry = chunk_geometry()
    codec = load("board-host-io/scripts/chunk_protocol.py", "chunk_rtl_codec")
    protocol = load("board-host-io/scripts/protocol.py", "chunk_rtl_protocol")
    runner = load("board-host-io/scripts/run_rtl_tb.py", "chunk_rtl_runner")
    values = [(i % 255) - 127 for i in range(geometry.in_elements)]
    requests = codec.request_frames(values, 37, geometry)
    raw = b"".join(requests)
    (tmp_path / "request.hex").write_text("".join(f"{b:02x}\n" for b in raw))
    packed = protocol.pack_payload(protocol.fixed_words(values, geometry), geometry)
    (tmp_path / "expected.hex").write_text(packed[::-1].hex() + "\n")
    bench = (ROOT / "board-host-io/tests/chunk_transport_tb.v").read_text()
    prefix_acks = 0
    if fault != "crc":
        prefix = codec.request_frames(values, 36, geometry)
        bad = bytearray(prefix[1])
        if fault == "sequence":
            bad[2:4] = struct.pack("<H", 35)
        if fault == "offset":
            bad[6:10] = struct.pack("<I", 1025)
        if fault == "total":
            bad[10:14] = struct.pack("<I", 66001)
        bad[-2:] = struct.pack("<H", protocol.crc16(bad[:-2]))
        wire = prefix[0] + bytes(bad)
        (tmp_path / "negative-prefix.hex").write_text("".join(f"{b:02x}\n" for b in wire))
        bench = bench.replace("PREFIX_ACKS=0", "PREFIX_ACKS=1")
        prefix_acks = 1
    path = tmp_path / "chunk_transport_tb.v"
    path.write_text(bench)
    files = [tmp_path / "request.hex", tmp_path / "expected.hex"]
    if prefix_acks:
        files.append(tmp_path / "negative-prefix.hex")
    result = runner.run_testbench(
        "chunk_transport_tb",
        [ROOT / "board-host-io/assets/transport_chunked.v", path],
        tmp_path,
        data_files=files,
    )
    (tmp_path / "simulation-result.json").write_text(json.dumps(result, indent=2))
    assert result["returncode"] == 0, result["stdout"]
    frames = (tmp_path / "response.hex").read_text().splitlines()
    assert len(frames) == geometry.input_chunks + geometry.output_chunks + prefix_acks
    cycles, outputs = protocol.decode_response(
        bytes.fromhex("".join(frames[prefix_acks:])), 37, geometry
    )
    assert cycles > 0 and len(outputs) == geometry.out_elements
    raw_expected = bytes((i * 17 + 9) % 256 for i in range(geometry.response_bytes))
    assert outputs == [float(x if x < 128 else x - 256) for x in raw_expected]


def test_generator_and_saved_geometry_use_same_chunk_framing(tmp_path):
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    board = load("board-host-io/scripts/make_host_board.py", "chunk_board")
    simulator = load("board-host-io/scripts/simulate_host.py", "chunk_host_sim")
    contract = {
        "precision": "ap_fixed<8,8>",
        "host_transport": {"framing": "chunked_v1", "chunk_bytes": 3},
    }
    producer = wrapper_record(4, 6, "ap_fixed<8,8>")
    producer["interface"].update(input="x", valid="x_ap_vld")
    producer.update(
        top="toy",
        contract_sha256="development-fixture-only",
        clock={"core_mhz": 100, "reference_mhz": 200, "mmcm_mult": 5, "mmcm_divide": 10},
    )
    geometry = geo.geometry_from_contract(contract, wrapper=producer)
    text = board.render_top(producer, "parallel", geometry, facts())
    assert "cnn_uart_chunk_transport" in text and ".CHUNK_BYTES(3)" in text
    assert "CHUNK_BYTES = 3" in simulator.render_testbench(1, geometry, facts())
    source = tmp_path / "producer.json"
    source.write_text(json.dumps(producer))
    stored = {
        "host_geometry": geometry.to_dict(),
        "source_wrapper": str(source),
        "source_wrapper_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "contract_sha256": producer["contract_sha256"],
        "host_protocol": geo.protocol_record(geometry),
    }
    assert geo._stored_geometry(contract, stored).to_dict() == geometry.to_dict()


def test_exchange_waits_for_each_ack_and_reassembles_every_output():
    geometry = chunk_geometry(7, 11, 3)
    codec = load("board-host-io/scripts/chunk_protocol.py", "chunk_exchange_codec")
    protocol = load("board-host-io/scripts/protocol.py", "chunk_exchange_public")
    values = list(range(7))
    requests = codec.request_frames(values, 19, geometry)
    data = bytes(range(11))
    received = []
    sent = []

    class Port:
        def __init__(self):
            self.buffer = bytearray()
            self.sent = []

        @property
        def in_waiting(self):
            return len(self.buffer)

        def write(self, frame):
            assert not self.buffer and frame == requests[len(self.sent)]
            self.sent.append(frame)
            end = min(len(self.sent) * 3, 7)
            self.buffer.extend(response_frame(19, end, 7, 0, b""))
            if len(self.sent) == len(requests):
                for offset in range(0, 11, 3):
                    self.buffer.extend(
                        response_frame(19, offset, 11, 31, data[offset : offset + 3])
                    )
            return len(frame)

        def read(self, count):
            take = min(count, 2)
            value = bytes(self.buffer[:take])
            del self.buffer[:take]
            return value

    port = Port()
    tx, rx, cycles, outputs = protocol.exchange(
        port, values, 19, geometry, on_receive=received.append, on_transmit=sent.append
    )
    assert tx == b"".join(requests) and rx == b"".join(received)
    assert sent == requests
    assert cycles == 31 and outputs == list(map(float, data))

    class ShortWrite:
        in_waiting = 0

        def write(self, frame):
            return len(frame) - 2

    accepted = []
    with pytest.raises(OSError, match="incomplete"):
        protocol.exchange(ShortWrite(), values, 19, geometry, on_transmit=accepted.append)
    assert accepted == [requests[0][:-2]]


def test_chunk_boundaries_may_split_packed_elements_without_loss():
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    codec = load("board-host-io/scripts/chunk_protocol.py", "packed_chunk_codec")
    protocol = load("board-host-io/scripts/protocol.py", "packed_chunk_protocol")
    contract = {
        "precision": "ap_fixed<20,6>",
        "host_transport": {"framing": "chunked_v1", "chunk_bytes": 5},
    }
    geometry = geo.geometry_from_contract(contract, wrapper=wrapper_record(5, 7, "ap_fixed<20,6>"))
    requests = codec.request_frames([0.25, -0.5, 1.25, -2, 3], 9, geometry)
    payload = b"".join(frame[14:-2] for frame in requests)
    assert payload == protocol.pack_payload(
        protocol.fixed_words([0.25, -0.5, 1.25, -2, 3], geometry), geometry
    )
    acks = [
        response_frame(9, min((i + 1) * 5, geometry.payload_bytes), geometry.payload_bytes, 0, b"")
        for i in range(geometry.input_chunks)
    ]
    packed = sum((((i - 3) * 1024) & ((1 << 20) - 1)) << (i * 20) for i in range(7))
    data = packed.to_bytes(geometry.response_bytes, "little")
    frames = [response_frame(9, i, len(data), 0, data[i : i + 5]) for i in range(0, len(data), 5)]
    count, actual = protocol.decode_response(b"".join(acks + frames), 9, geometry)
    assert count == 0 and actual == [(i - 3) / 16 for i in range(7)]


@pytest.mark.parametrize("chunk", [0, -1, True, 65524])
def test_invalid_chunk_geometry_is_not_silently_truncated(chunk):
    with pytest.raises(ValueError):
        chunk_geometry(7, 11, chunk)


def test_chunk_capture_provenance_includes_executed_codec(tmp_path):
    verify = load("board-host-io/scripts/verify_board.py", "chunk_capture_provenance")
    sources = verify.provenance_sources(*([tmp_path / "fixture"] * 9), chunked=True)
    assert any(path.name == "chunk_protocol.py" for path in sources)
