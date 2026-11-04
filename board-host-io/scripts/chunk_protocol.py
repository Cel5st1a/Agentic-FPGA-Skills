"""Optional chunked_v1 framing. Reassemble one entire sample; never split computation."""

import struct
import time

from protocol import crc16, diagnostic_check, fixed_words, pack_payload, read_exact, unpack_field


def request_frames(values, sequence, geometry):
    if len(values) != geometry.in_elements:
        raise ValueError("input element count mismatch")
    if not 0 <= sequence <= 65535:
        raise ValueError("sequence must be uint16")
    payload = pack_payload(fixed_words(values, geometry), geometry)
    frames = []
    for offset in range(0, len(payload), geometry.chunk_bytes):
        block = payload[offset : offset + geometry.chunk_bytes]
        body = b"\xa6\x6a" + struct.pack("<HHII", sequence, len(block) + 8, offset, len(payload))
        body += block
        frames.append(body + struct.pack("<H", crc16(body)))
    return frames


def parse_response(frame, sequence, chunk_bytes, *, strict=True, on_diagnostic=None):
    if len(frame) < 20:
        raise ValueError("invalid chunk response header")
    seq, length, offset, total, cycles = struct.unpack("<HHIII", frame[2:18])
    diagnostic_check(frame[:2] == b"\x6a\xa6", "chunk magic mismatch", strict, on_diagnostic)
    diagnostic_check(seq == sequence, "chunk sequence mismatch", strict, on_diagnostic)
    diagnostic_check(
        12 <= length <= 12 + chunk_bytes and len(frame) == length + 8,
        "chunk length field mismatch",
        strict,
        on_diagnostic,
    )
    diagnostic_check(
        crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0],
        "chunk CRC mismatch",
        strict,
        on_diagnostic,
    )
    return offset, total, cycles, frame[18:-2]


def decode_frames(wire, sequence, geometry, *, strict=True, on_diagnostic=None):
    cursor, frames = 0, []
    expected_sizes = [20] * geometry.input_chunks + [
        20 + min(geometry.chunk_bytes, geometry.response_bytes - offset)
        for offset in range(0, geometry.response_bytes, geometry.chunk_bytes)
    ]
    if not strict and len(wire) != sum(expected_sizes):
        raise ValueError("incomplete or extra raw chunk length")
    while cursor < len(wire):
        if len(wire) - cursor < 6:
            raise ValueError("partial chunk header")
        size = (
            int.from_bytes(wire[cursor + 4 : cursor + 6], "little") + 8
            if strict
            else expected_sizes[len(frames)]
        )
        if size < 20 or size > geometry.chunk_bytes + 20:
            raise ValueError("invalid chunk length")
        frames.append(
            parse_response(
                wire[cursor : cursor + size],
                sequence,
                geometry.chunk_bytes,
                strict=strict,
                on_diagnostic=on_diagnostic,
            )
        )
        cursor += size
    if len(frames) != geometry.input_chunks + geometry.output_chunks:
        raise ValueError("incomplete or extra chunk frames")
    for index, (offset, total, cycles, data) in enumerate(frames[: geometry.input_chunks]):
        expected = min((index + 1) * geometry.chunk_bytes, geometry.payload_bytes)
        diagnostic_check(
            (offset, total, cycles, data) == (expected, geometry.payload_bytes, 0, b""),
            "input chunk ACK metadata mismatch",
            strict,
            on_diagnostic,
        )
    payload, counts = bytearray(), []
    for offset, total, cycles, data in frames[geometry.input_chunks :]:
        expected = min(geometry.chunk_bytes, geometry.response_bytes - len(payload))
        if len(data) != expected:
            raise ValueError("incomplete output chunk data")
        diagnostic_check(
            offset == len(payload) and total == geometry.response_bytes,
            "output offset/extent metadata mismatch",
            strict,
            on_diagnostic,
        )
        payload.extend(data)
        counts.append(cycles)
    if len(payload) != geometry.response_bytes:
        raise ValueError("incomplete output")
    diagnostic_check(len(set(counts)) == 1, "inconsistent counter", strict, on_diagnostic)
    packed, bits = int.from_bytes(payload, "little"), geometry.out_element_bits
    mask = (1 << bits) - 1
    values = [
        unpack_field((packed >> (bits * i)) & mask, bits, geometry.out_integer_bits)
        for i in range(geometry.out_elements)
    ]
    return counts[0] if len(set(counts)) == 1 else None, values


def exchange_chunks(
    serial,
    values,
    sequence,
    geometry,
    timeout_s,
    on_receive,
    on_transmit=None,
    *,
    strict_protocol=True,
    on_diagnostic=None,
):
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if serial.in_waiting:
        raise ValueError("stale bytes before request")
    requests, received = request_frames(values, sequence, geometry), []

    def receive(expected_data):
        deadline = time.monotonic() + timeout_s
        prefix = read_exact(serial, 6, deadline, on_receive)
        length = int.from_bytes(prefix[4:6], "little")
        if strict_protocol and not 12 <= length <= 12 + geometry.chunk_bytes:
            raise ValueError("invalid chunk response length")
        remaining = length + 2 if strict_protocol else expected_data + 14
        frame = prefix + read_exact(serial, remaining, deadline, on_receive)
        received.append(frame)
        return parse_response(frame, sequence, geometry.chunk_bytes, strict=strict_protocol)

    for index, frame in enumerate(requests):
        written = serial.write(frame)
        if on_transmit is not None and isinstance(written, int) and 0 < written <= len(frame):
            on_transmit(frame[:written])
        if written != len(frame):
            raise OSError("incomplete chunk write")
        ack = receive(0)
        end = min((index + 1) * geometry.chunk_bytes, geometry.payload_bytes)
        if strict_protocol and ack != (end, geometry.payload_bytes, 0, b""):
            raise ValueError("input chunk ACK mismatch")
    for offset in range(0, geometry.response_bytes, geometry.chunk_bytes):
        receive(min(geometry.chunk_bytes, geometry.response_bytes - offset))
    raw = b"".join(received)
    cycles, outputs = decode_frames(
        raw, sequence, geometry, strict=strict_protocol, on_diagnostic=on_diagnostic
    )
    if serial.in_waiting:
        raise ValueError("extra bytes after complete chunk output")
    return b"".join(requests), raw, cycles, outputs
