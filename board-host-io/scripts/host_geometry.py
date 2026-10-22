"""Read hardware geometry from build records and materialise UART framing once.

Contract precision is an acceptance-grid constraint, never an element-count source.
Consumers cannot repair a missing build fact by interpreting shapes or references.
"""

from __future__ import annotations

import json
import hashlib
import math
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from line.geometry import parse_precision, positive_int, quantise, sample_rows


#: The frame header/footer the verified transport has always used.
#: The request is A5 5A + seq(2) + len(2) + payload + CRC(2) = payload + 8. The handoff
#: document wrote "+10" and then listed exactly these eight bytes; the on-disk verified
#: frame is 264 = 256 + 8, so 8 is the value that reproduces the existing constant.
REQUEST_OVERHEAD_BYTES = 8
RESPONSE_OVERHEAD_BYTES = 12     # 5A A5 + seq(2) + len(2) + cycles(4) + CRC(2)
#: The response length field counts compute cycles plus the packed result bytes.
RESPONSE_LENGTH_EXTRA_BYTES = 4

#: The UART assets that carry real board evidence were built and timed at this rate.
VERIFIED_BAUDRATE = 115200
MAX_UINT16 = 65535
MAX_LENGTH_FIELD = 255

AP_VLD_STYLE = "ap_vld_array_with_ap_ctrl_hs"
AXIS_STYLE = "axis_stream_with_ap_ctrl_hs"
SUPPORTED_STYLES = (AP_VLD_STYLE, AXIS_STYLE)


def host_range(total_bits: int, integer_bits: int) -> tuple[float, float]:
    """The closed-open float range a signed ``ap_fixed<W,I>`` field can carry."""
    return -(2.0 ** (integer_bits - 1)), 2.0 ** (integer_bits - 1)


def pack_field(value: float, total_bits: int, integer_bits: int) -> int:
    """One element as its ``total_bits``-wide two's-complement field."""
    return quantise(value, total_bits, integer_bits) & ((1 << total_bits) - 1)


def unpack_field(field: int, total_bits: int, integer_bits: int) -> float:
    """Inverse of `pack_field`."""
    if field < 0 or field >= (1 << total_bits):
        raise ValueError(f"field {field} does not fit {total_bits} bits")
    signed = field - (1 << total_bits) if field >> (total_bits - 1) else field
    return signed / float(1 << (total_bits - integer_bits))


@dataclass(frozen=True)
class Geometry:
    """Every wire-level number the host path needs, derived once."""

    precision: str
    total_bits: int
    integer_bits: int
    in_elements: int
    out_elements: int
    in_element_bits: int
    in_integer_bits: int
    out_element_bits: int
    out_integer_bits: int
    payload_bytes: int
    response_bytes: int
    in_words_bits: int
    out_words_bits: int
    baudrate: int
    baudrate_source: str
    geometry_source: str

    #: Padding-free width of the core's packed input bus.
    @property
    def in_bits(self) -> int:
        return self.in_elements * self.in_element_bits

    #: Padding-free width of the core's packed result bus.
    @property
    def out_bits(self) -> int:
        return self.out_elements * self.out_element_bits

    @property
    def request_bytes(self) -> int:
        return self.payload_bytes + REQUEST_OVERHEAD_BYTES

    @property
    def response_frame_bytes(self) -> int:
        return self.response_bytes + RESPONSE_OVERHEAD_BYTES

    @property
    def request_length_field(self) -> int:
        return self.payload_bytes

    @property
    def response_length_field(self) -> int:
        return self.response_bytes + RESPONSE_LENGTH_EXTRA_BYTES

    @property
    def scale_in(self) -> int:
        return 1 << (self.in_element_bits - self.in_integer_bits)

    @property
    def scale_out(self) -> int:
        return 1 << (self.out_element_bits - self.out_integer_bits)

    def to_dict(self) -> dict:
        record = asdict(self)
        record.update({
            "in_bits": self.in_bits, "out_bits": self.out_bits,
            "request_bytes": self.request_bytes,
            "response_frame_bytes": self.response_frame_bytes,
            "request_length_field": self.request_length_field,
            "response_length_field": self.response_length_field,
            "scale_in": self.scale_in, "scale_out": self.scale_out,
        })
        return record


def _baudrate(contract: dict, board_profile: dict | None) -> tuple[int, str]:
    declared = (contract.get("host_transport") or {}).get("baudrate")
    if declared is not None:
        source = "contract.host_transport.baudrate"
    else:
        uart = ((board_profile or {}).get("peripherals") or {}).get("uart") or {}
        declared = uart.get("baudrate")
        source = "board_profile.peripherals.uart.baudrate"
    if declared is None:
        return VERIFIED_BAUDRATE, "verified_default"
    rate = positive_int(declared, source)
    return rate, source


def _fail(field: str, expected, actual):
    raise ValueError(f"{field}: expected {expected}; actual {actual!r}")


def _mapping(value, field: str) -> dict:
    if not isinstance(value, dict):
        _fail(field, "build-record mapping (regenerate missing records in a new run)", value)
    return value


def _equal(field: str, actual, expected):
    if type(actual) is not type(expected) or actual != expected:
        _fail(field, expected, actual)


def _grid(value, field: str) -> tuple[int, int]:
    value = _mapping(value, field)
    bits = positive_int(value.get("total_bits"), field + ".total_bits")
    integer = positive_int(value.get("integer_bits"), field + ".integer_bits")
    if integer > bits:
        _fail(field, "1 <= integer_bits <= total_bits", value)
    if value.get("signed", True) is not True:
        _fail(field + ".signed", True, value.get("signed"))
    return bits, integer


def _layout(wrapper: dict, name: str) -> int:
    layout = _mapping(wrapper.get(name), name)
    _equal(name + ".flatten", layout.get("flatten"), "c_order")
    return positive_int(layout.get("elements"), name + ".elements")


def _core_facts(contract: dict, wrapper: dict, references=None) -> tuple:
    """Validate recorded facts; no declaration or sample can replace a missing fact."""
    precision = contract.get("precision")
    total, integer = parse_precision(precision)
    interface = _mapping(wrapper.get("interface"), "interface")
    style = interface.get("style")
    if style not in SUPPORTED_STYLES:
        _fail("interface.style", f"one of {SUPPORTED_STYLES}; a new style needs an explicit rule", style)
    in_elements = _layout(wrapper, "input_layout")
    rom = _mapping(wrapper.get("rom_image"), "rom_image")
    in_grid = _grid(rom.get("grid"), "rom_image.grid")
    out_grid = _grid(wrapper.get("output_grid"), "output_grid")
    for field, grid in (("rom_image.grid", in_grid), ("output_grid", out_grid)):
        if grid != (total, integer):
            _fail(field, f"contract precision {precision}", f"ap_fixed<{grid[0]},{grid[1]}>")
    if style == AP_VLD_STYLE:
        ports = interface.get("result_outputs")
        if not isinstance(ports, list) or not ports or any(not isinstance(p, str) or not p for p in ports):
            _fail("interface.result_outputs", "nonempty ordered scalar-port list", ports)
        if len(set(ports)) != len(ports):
            _fail("interface.result_outputs", "unique scalar ports", ports)
        widths = _mapping(interface.get("output_bits"), "interface.output_bits")
        for name in ports:
            _equal(f"interface.output_bits.{name}", widths.get(name), out_grid[0])
        recorded_outputs = len(ports)  # The verified scalar-port rule, never a generic fallback.
        _equal("interface.input_bits", interface.get("input_bits"), in_elements * in_grid[0])
        _equal("interface.elements", interface.get("elements"), in_elements)
    else:
        types = _mapping(interface.get("element_types"), "interface.element_types")
        for side, expected in (("input", in_grid), ("output", out_grid)):
            actual = _grid(types.get(side), f"interface.element_types.{side}")
            if actual != expected:
                _fail(f"interface.element_types.{side}", f"recorded grid {expected} and contract precision {precision}", actual)
        stream = _mapping(interface.get("stream"), "interface.stream")
        recorded_outputs = positive_int(stream.get("output_elements"), "interface.stream.output_elements")
        _equal("interface.stream.input_elements", stream.get("input_elements"), in_elements)
    out_elements = (_layout(wrapper, "output_layout") if "output_layout" in wrapper else recorded_outputs)
    _equal("output_layout.elements / interface result elements", out_elements, recorded_outputs)
    if "ports" in (wrapper.get("output_layout") or {}):
        _equal("output_layout.ports", wrapper["output_layout"]["ports"], interface.get("result_outputs"))
    if references is not None:
        rows = sample_rows(references, "references")
        if len(rows[0]) != out_elements:
            _fail("references result elements", out_elements, len(rows[0]))
    if style == AXIS_STYLE:
        # These are adapter preconditions, independent of how counts were recorded.
        lanes = positive_int(stream.get('input_lanes_per_beat'), 'interface.stream.input_lanes_per_beat')
        beats = positive_int(stream.get('input_beats_per_sample'), 'interface.stream.input_beats_per_sample')
        if in_elements % lanes:
            _fail('interface.stream.input_elements / input_lanes_per_beat', 'exact division', in_elements / lanes)
        _equal('interface.stream.input_beats_per_sample', beats, in_elements // lanes)
        for key, expected in (
            ("output_beats_per_sample", 1),
            ("output_lanes_per_beat", out_elements),
            ("lane_bits", in_grid[0]), ("element_bits", in_grid[0]),
            ("output_element_bits", out_grid[0]),
            ("tdata_bits", lanes * in_grid[0]),
            ("output_tdata_bits", out_elements * out_grid[0])):
            _equal("interface.stream." + key, stream.get(key), expected)
        _equal("interface.stream.lane_bits for output", stream["lane_bits"], out_grid[0])
        for key in ("tdata_bits", "output_tdata_bits"):
            if stream[key] % 8:
                _fail("interface.stream." + key, "byte-aligned TDATA", stream[key])
    return in_elements, out_elements, in_grid, out_grid


def geometry_from_contract(contract: dict, references=None, interface: dict | None = None,
                           board_profile: dict | None = None, *, wrapper: dict | None = None) -> Geometry:
    """Materialise framing from a complete build record, constrained by the contract.

    The old name is retained for callers, but contract-only inference is forbidden.
    """
    _mapping(contract, "contract")
    if not contract.get("precision"):
        _fail("contract.precision", "ap_fixed<W,I>", contract.get("precision"))
    wrapper = _mapping(wrapper, "wrapper")
    if interface is not None and interface != wrapper.get("interface"):
        _fail("interface", "the wrapper's interface (no alternate source)", interface)
    in_elements, out_elements, in_grid, out_grid = _core_facts(contract, wrapper, references)
    in_bits, in_int = in_grid
    out_bits, out_int = out_grid
    payload_bytes = (in_elements * in_bits + 7) // 8
    response_bytes = (out_elements * out_bits + 7) // 8
    if payload_bytes > MAX_UINT16:
        _fail("host_geometry.payload_bytes", f"<= {MAX_UINT16}", payload_bytes)
    if response_bytes + RESPONSE_LENGTH_EXTRA_BYTES > MAX_LENGTH_FIELD:
        _fail("host_geometry.response_length_field", f"<= {MAX_LENGTH_FIELD}", response_bytes + RESPONSE_LENGTH_EXTRA_BYTES)
    baudrate, source = _baudrate(contract, board_profile)
    return Geometry(
        precision=str(contract["precision"]), total_bits=in_bits, integer_bits=in_int,
        in_elements=in_elements, out_elements=out_elements,
        in_element_bits=in_bits, in_integer_bits=in_int,
        out_element_bits=out_bits, out_integer_bits=out_int,
        payload_bytes=payload_bytes, response_bytes=response_bytes,
        in_words_bits=8 * payload_bytes, out_words_bits=8 * response_bytes,
        baudrate=baudrate, baudrate_source=source,
        geometry_source="wrapper.input_layout/interface/rom_image.grid/output_grid")


def load_board_profile(path) -> dict | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        _fail("board.pins_profile", "existing board profile", str(candidate))
    return yaml.safe_load(candidate.read_text(encoding="utf-8-sig"))


def reference_rows_from_deployment(deployment, contract: dict):
    entry = (contract.get("verification") or {}).get("references")
    if not entry:
        return None
    path = (Path(deployment).parent / entry).resolve()
    if not path.is_file():
        _fail("verification.references", "existing reference file", str(path))
    return json.loads(path.read_text(encoding="utf-8-sig"))


def protocol_record(geometry: Geometry) -> dict:
    """A projection of the saved geometry, not a second independently chosen protocol."""
    return {
        "request_bytes": geometry.request_bytes, "response_bytes": geometry.response_frame_bytes,
        "baudrate": geometry.baudrate, "baudrate_source": geometry.baudrate_source,
        "geometry_source": geometry.geometry_source,
        "input_values": geometry.in_elements, "output_values": geometry.out_elements,
        "input_bits": geometry.in_element_bits, "output_bits": geometry.out_element_bits,
        "scale": geometry.scale_in, "output_scale": geometry.scale_out,
    }


def _contract_identity(deployment, contract, wrapper):
    if "contract" not in wrapper and "contract_sha256" not in wrapper and wrapper.get("skill") != "board-host-io":
        return  # Pure, explicit synthetic records have no file identity. CLI producers do.
    path = Path(deployment)
    if not path.is_file():
        _fail("contract", "existing deployment file", str(path))
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    _equal("contract_sha256", wrapper.get("contract_sha256"), actual)
    _equal("contract mapping", contract, yaml.safe_load(path.read_text(encoding="utf-8-sig")))


def _host_core_record(wrapper: dict) -> dict:
    """Legacy hosts dropped rom_image; recover facts only from their hash-bound producer."""
    source_path = Path(wrapper.get("source_wrapper") or "")
    if not source_path.is_file():
        _fail("source_wrapper", "existing hash-bound producer record", str(source_path))
    raw = source_path.read_bytes()
    _equal("source_wrapper_sha256", wrapper.get("source_wrapper_sha256"), hashlib.sha256(raw).hexdigest())
    source = json.loads(raw.decode("utf-8-sig"))
    _equal("source_wrapper.contract_sha256", source.get("contract_sha256"), wrapper.get("contract_sha256"))
    for key in ("interface", "input_layout", "output_grid", "output_layout"):
        if key in wrapper:
            _equal(key, wrapper[key], source.get(key))
    if "rom_image" in wrapper:
        _equal("rom_image.grid", wrapper["rom_image"].get("grid"), (source.get("rom_image") or {}).get("grid"))
    return source


def _stored_geometry(contract: dict, wrapper: dict) -> Geometry:
    record = _mapping(wrapper.get("host_geometry"), "host_geometry")
    source = _host_core_record(wrapper)
    in_elements, out_elements, in_grid, out_grid = _core_facts(contract, source)
    required = [field.name for field in fields(Geometry)]
    values = {}
    for name in required:
        value = record.get(name)
        if name in ("precision", "baudrate_source", "geometry_source"):
            if not isinstance(value, str) or not value:
                _fail("host_geometry." + name, "nonempty string", value)
        else:
            positive_int(value, "host_geometry." + name)
        values[name] = value
    expected = {
        "precision": str(contract["precision"]),
        "total_bits": in_grid[0], "integer_bits": in_grid[1],
        "in_elements": in_elements, "out_elements": out_elements,
        "in_element_bits": in_grid[0], "in_integer_bits": in_grid[1],
        "out_element_bits": out_grid[0], "out_integer_bits": out_grid[1],
        "payload_bytes": (in_elements * in_grid[0] + 7) // 8,
        "response_bytes": (out_elements * out_grid[0] + 7) // 8,
    }
    expected["in_words_bits"] = 8 * expected["payload_bytes"]
    expected["out_words_bits"] = 8 * expected["response_bytes"]
    for name, value in expected.items():
        _equal("host_geometry." + name, record.get(name), value)
    geometry = Geometry(**values)
    # Only check identities of stored derived fields; never replace a recorded value.
    for name, value in geometry.to_dict().items():
        _equal("host_geometry." + name, record.get(name), value)
    if geometry.payload_bytes > MAX_UINT16:
        _fail("host_geometry.payload_bytes", f"<= {MAX_UINT16}", geometry.payload_bytes)
    if geometry.response_length_field > MAX_LENGTH_FIELD:
        _fail("host_geometry.response_length_field", f"<= {MAX_LENGTH_FIELD}", geometry.response_length_field)
    declared = (contract.get("host_transport") or {}).get("baudrate")
    if declared is not None:
        _equal("host_geometry.baudrate", geometry.baudrate, positive_int(declared, "contract.host_transport.baudrate"))
        _equal("host_geometry.baudrate_source", geometry.baudrate_source, "contract.host_transport.baudrate")
    elif geometry.baudrate_source == "verified_default":
        _equal("host_geometry.baudrate", geometry.baudrate, VERIFIED_BAUDRATE)
    elif geometry.baudrate_source != "board_profile.peripherals.uart.baudrate":
        _fail("host_geometry.baudrate_source", "board profile or verified default", geometry.baudrate_source)
    protocol = _mapping(wrapper.get("host_protocol"), "host_protocol")
    for name, value in protocol_record(geometry).items():
        _equal("host_protocol." + name, protocol.get(name), value)
    return geometry


def geometry_for_wrapper(deployment, contract: dict, wrapper: dict,
                         board_profile: dict | None = None, references=None) -> Geometry:
    """Build once for a ROM record; load and validate for a materialised host record."""
    try:
        _mapping(wrapper, "wrapper")
        _contract_identity(deployment, contract, wrapper)
        if wrapper.get("skill") == "board-host-io" or "host_geometry" in wrapper:
            geometry = _stored_geometry(contract, wrapper)
            if board_profile is not None:
                baudrate, source = _baudrate(contract, board_profile)
                _equal("host_geometry.baudrate", geometry.baudrate, baudrate)
                _equal("host_geometry.baudrate_source", geometry.baudrate_source, source)
            if references is not None:
                rows = sample_rows(references, "references")
                _equal("references result elements", len(rows[0]), geometry.out_elements)
            return geometry
        if board_profile is None:
            board_profile = load_board_profile((wrapper.get("board") or {}).get("pins_profile"))
        if references is None:
            references = reference_rows_from_deployment(deployment, contract)
        return geometry_from_contract(contract, references=references, wrapper=wrapper,
                                      board_profile=board_profile)
    except ValueError as exc:
        source = (wrapper.get('source_wrapper', wrapper.get('core_verilog', 'inline wrapper'))
                  if isinstance(wrapper, dict) else 'invalid wrapper')
        raise ValueError(f"geometry for {deployment} (source {source}): {exc}") from exc
