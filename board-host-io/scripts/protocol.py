"""Framing for the host UART path, in terms of a derived `Geometry`.

Every length, width and scale comes from `host_geometry`, which derives them from the
deployment contract. This module keeps no pinned constant of its own: at the verified
64x4 geometry it reproduces the earlier fixed implementation byte for byte (pinned by the
golden frames under `tests/data/`), and at any other geometry it follows the derivation.
"""

from __future__ import annotations

import binascii
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_geometry import (  # noqa: E402
    Geometry,
    host_range,
    pack_field,
    quantise,
    unpack_field,
)


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)


def fixed_words(values, geometry: Geometry) -> list[int]:
    """Quantise one sample onto the core's grid, refusing anything the field cannot hold.

    Values outside the representable range are an error rather than a silent clamp: the
    contract's rows are the operator's own input, and quietly changing them would make the
    host request differ from the ROM path that the same rows feed.
    """
    low, high = host_range(geometry.in_element_bits, geometry.in_integer_bits)
    words = []
    for value in values:
        number = float(value)
        if (
            not number == number
            or number in (float("inf"), float("-inf"))
            or not low <= number < high
        ):
            raise ValueError(
                f"input outside ap_fixed<{geometry.in_element_bits},"
                f"{geometry.in_integer_bits}> range [{low}, {high})"
            )
        words.append(pack_field(number, geometry.in_element_bits, geometry.in_integer_bits))
    return words


def pack_payload(values, geometry: Geometry) -> bytes:
    """Element i of a packed array port occupies bits [i*W +: W]; element 0 is the low word."""
    packed = 0
    for index, word in enumerate(values):
        packed |= word << (geometry.in_element_bits * index)
    return packed.to_bytes(geometry.payload_bytes, "little")


def encode_request(values, sequence: int, geometry: Geometry) -> bytes:
    if geometry.framing == "chunked_v1":
        from chunk_protocol import request_frames

        return b"".join(request_frames(values, sequence, geometry))
    if len(values) != geometry.in_elements:
        raise ValueError(f"expected {geometry.in_elements} input values, got {len(values)}")
    if not 0 <= sequence <= 65535:
        raise ValueError("sequence must be a uint16")
    payload = pack_payload(fixed_words(values, geometry), geometry)
    body = (
        b"\xa5\x5a"
        + struct.pack("<H", sequence)
        + struct.pack("<H", geometry.request_length_field)
        + payload
    )
    return body + struct.pack("<H", crc16(body))


def diagnostic_check(condition, message, strict, on_diagnostic, **details):
    if condition:
        return
    if on_diagnostic is not None:
        on_diagnostic(dict(message=message, **details))
    if strict:
        raise ValueError(message)


def decode_response(
    frame: bytes, sequence: int, geometry: Geometry, *, strict=True, on_diagnostic=None
) -> tuple[int, list[float]]:
    if geometry.framing == "chunked_v1":
        from chunk_protocol import decode_frames

        return decode_frames(frame, sequence, geometry, strict=strict, on_diagnostic=on_diagnostic)
    if len(frame) != geometry.response_frame_bytes:
        raise ValueError(
            f"response length {len(frame)} != {geometry.response_frame_bytes}; "
            "partial or extra frame"
        )
    for observed, expected, message in [
        (frame[:2].hex(), "5aa5", "invalid response magic"),
        (struct.unpack("<H", frame[2:4])[0], sequence, "response sequence mismatch"),
        (
            struct.unpack("<H", frame[4:6])[0],
            geometry.response_length_field,
            "response payload length field mismatch",
        ),
        (struct.unpack("<H", frame[-2:])[0], crc16(frame[:-2]), "response CRC mismatch"),
    ]:
        diagnostic_check(
            observed == expected,
            message,
            strict,
            on_diagnostic,
            observed=observed,
            expected=expected,
        )
    cycles = struct.unpack("<I", frame[6:10])[0]
    diagnostic_check(cycles != 0, "response compute cycles are zero", strict, on_diagnostic)
    packed = int.from_bytes(frame[10:-2], "little")
    mask = (1 << geometry.out_element_bits) - 1
    values = [
        unpack_field(
            (packed >> (geometry.out_element_bits * index)) & mask,
            geometry.out_element_bits,
            geometry.out_integer_bits,
        )
        for index in range(geometry.out_elements)
    ]
    return cycles, values


def read_exact(serial, count: int, deadline: float, on_receive=None) -> bytes:
    """Read exactly `count` bytes, or raise when the deadline passes.

    This reader never touches `serial.timeout`. The port's read timeout belongs to the
    session that opened the port, set once, before the first request -- not to the reader
    and not per transaction.

    That is a measured rule, not a style preference. On Windows, assigning
    `serial.timeout` re-applies COMMTIMEOUTS, and the FTDI VCP driver drops bytes that are
    in flight when that happens. Measured on the Digilent Nexys Video (FT232R) on
    2026-10-01, same frozen frame format, same bitstream, one host:

        timeout re-assigned every loop pass (this function's first version)  1 lost in 7..28
        timeout assigned once per transaction                                1 lost in 70
        timeout assigned once at open, never again                           0 lost in 450

    A lost transaction looks like a silent FPGA: the request is cut short on the wire, the
    transport rejects the frame, and nothing comes back. The loss was independent of frame
    length (264-byte and 48-byte requests failed at the same rate), the board answered
    correctly immediately afterwards every time, and the same host code lost only 1 in
    ~1300 through a CP210x bridge -- which is what pointed at the driver rather than at
    the wire, the bridge or the design.

    The deadline is still honoured: it is checked on every pass, so a timeout overshoots
    by at most one read.
    """
    data = bytearray()
    while len(data) < count:
        if time.monotonic() > deadline:
            raise TimeoutError(f"partial response: {len(data)}/{count} bytes")
        chunk = serial.read(count - len(data))
        if chunk:
            data.extend(chunk)
            if on_receive is not None:
                on_receive(chunk)
    return bytes(data)


def exchange(
    serial,
    values,
    sequence: int,
    geometry: Geometry,
    timeout_s: float = 2.0,
    on_receive=None,
    on_transmit=None,
    strict_protocol=True,
    on_diagnostic=None,
) -> tuple[bytes, bytes, int, list[float]]:
    """One request/response; stale bytes, partial frames and duplicate IDs fail closed."""
    if geometry.framing == "chunked_v1":
        from chunk_protocol import exchange_chunks

        return exchange_chunks(
            serial,
            values,
            sequence,
            geometry,
            timeout_s,
            on_receive,
            on_transmit,
            strict_protocol=strict_protocol,
            on_diagnostic=on_diagnostic,
        )
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if serial.in_waiting:
        raise ValueError("stale bytes before request; refusing cross-frame pairing")
    request = encode_request(values, sequence, geometry)
    written = serial.write(request)
    if on_transmit is not None and isinstance(written, int) and 0 < written <= len(request):
        on_transmit(request[:written])
    if written != geometry.request_bytes:
        raise OSError("incomplete serial write")
    response = read_exact(
        serial, geometry.response_frame_bytes, time.monotonic() + timeout_s, on_receive
    )
    cycles, outputs = decode_response(
        response, sequence, geometry, strict=strict_protocol, on_diagnostic=on_diagnostic
    )
    if serial.in_waiting:
        raise ValueError("extra bytes after response; refusing cross-frame pairing")
    return request, response, cycles, outputs


__all__ = [
    "Geometry",
    "crc16",
    "decode_response",
    "encode_request",
    "exchange",
    "fixed_words",
    "pack_field",
    "pack_payload",
    "quantise",
    "read_exact",
    "unpack_field",
]
