"""The board facts a host-path build is bound to: one reader, one source.

A board-facing pin, a clock topology, an I/O standard or a JTAG identity is never
allowed to be remembered in code. It is read from the board profile
(`board-profiles/<BOARD>/board_profile.yaml`), which must *declare* it and must
carry provenance for it.

That rule exists because of what the second board taught this tree. The AX7A200B
and the Digilent Nexys Video both carry an xc7a200t die, and `R4` is a pin name in
both profiles — but on the AX7A200B it is the P side of a 200 MHz differential
`DIFF_SSTL15` pair, and on the Nexys Video it is a 100 MHz single-ended
`LVCMOS33` input. A consumer that keys off the pin name, or that assumes "the
clock is differential because this is an Artix-7", produces a design that cannot
be placed, and it produces it silently. So:

* a fact the profile does not declare is **refused**, never defaulted;
* a profile that declares both clock forms is **refused** as ambiguous;
* a fact without provenance is **refused**: adding a board must mean stating where
  its pins came from, and redefining a board must break a test.

This module is the only place those fields are read. `make_host_board.py`,
`audit_ports.py`, `program_jtag.py` and `package_host_bitstream.py` all consume
`BoardFacts`, so the four gates cannot drift apart from one another or from the
profile the build was made with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CLOCK_KINDS = ("differential", "single_ended")

#: Top-level port names by clock topology. The port names are part of the generated
#: interface, so they are stated once here rather than spelled out per generator.
CLOCK_PORTS = {"differential": ("sys_clk_p", "sys_clk_n"), "single_ended": ("sys_clk",)}

HOST_PORTS = ("rst_n", "uart_rx", "uart_tx")


class BoardFactError(ValueError):
    """A board fact is missing, ambiguous, or unsourced."""


def _require(mapping: dict, key: str, where: str):
    value = mapping.get(key)
    if value in (None, "", [], {}):
        raise BoardFactError(f"{where} declares no {key!r}: the board profile must state it, "
                             f"and a board fact is never guessed")
    return value


def _provenance(block: dict, where: str) -> str:
    value = block.get("provenance")
    if not isinstance(value, str) or not value.strip():
        raise BoardFactError(
            f"{where} carries no provenance: every board-facing fact must say where it came "
            f"from (board manual, vendor XDC, or a measured bench), because that is what "
            f"distinguishes a declared board from a remembered one")
    return value.strip()


def _usb_id(value, where: str) -> int:
    """A USB id as an int, from '10c4', '0x10c4' or 0x10c4 alike."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    try:
        return int(text, 16)
    except ValueError:
        raise BoardFactError(f"{where} is {value!r}, which is not a USB id") from None


def _clock_block(profile: dict, where: str) -> dict:
    """The clock declaration, from `board_pins.clock` or the legacy `clock_sources.sys_clk`."""
    declared = (profile.get("board_pins") or {}).get("clock") or {}
    if declared:
        return dict(declared)
    legacy = (profile.get("clock_sources") or {}).get("sys_clk") or {}
    if legacy:
        # The older shape spells the frequency out and names the pins P/N, with no
        # topology key; both being present is what makes it a differential pair.
        converted = {key: value for key, value in legacy.items()
                     if key in {"frequency_mhz", "iostandard", "provenance",
                                "requires_operator_confirmation", "operator_confirmation"}}
        if legacy.get("fpga_pin_p"):
            converted["clk_p"] = legacy["fpga_pin_p"]
        if legacy.get("fpga_pin_n"):
            converted["clk_n"] = legacy["fpga_pin_n"]
        if legacy.get("fpga_pin"):
            converted["pin"] = legacy["fpga_pin"]
        return converted
    raise BoardFactError(f"{where} declares no board_pins.clock and no clock_sources.sys_clk")


def _clock_kind(declared: dict, where: str) -> tuple[str, tuple[str, ...]]:
    """Resolve the clock topology, refusing anything the declaration leaves ambiguous."""
    kind = declared.get("kind")
    single = declared.get("pin")
    pair = (declared.get("clk_p"), declared.get("clk_n"))
    if kind is not None and kind not in CLOCK_KINDS:
        raise BoardFactError(f"{where} declares clock kind {kind!r}; known kinds are "
                             f"{list(CLOCK_KINDS)}")
    if single and any(pair):
        raise BoardFactError(
            f"{where} declares both a single-ended pin {single!r} and a differential pair "
            f"{pair}: the clock topology is ambiguous, and guessing it would silently pick "
            f"the wrong input buffer")
    if kind == "differential" or (kind is None and all(pair)):
        if not all(pair):
            raise BoardFactError(f"{where} declares a differential clock without both clk_p "
                                 f"and clk_n")
        return "differential", (pair[0], pair[1])
    if kind == "single_ended" or (kind is None and single):
        if not single:
            raise BoardFactError(f"{where} declares a single-ended clock without its pin")
        return "single_ended", (single,)
    raise BoardFactError(f"{where} declares no clock pins at all")


@dataclass(frozen=True)
class BoardFacts:
    """Board-facing facts, with the profile they came from and their provenance."""

    profile_path: Path
    board_id: str
    part: str
    clock_kind: str
    clock_pins: tuple[str, ...]
    clock_iostandard: str
    reference_mhz: float
    core_frequency_mhz: float | None
    reset_pin: str
    reset_iostandard: str
    uart_rx_pin: str
    uart_tx_pin: str
    uart_iostandard: str
    uart_usb_vid: int | None = None
    uart_usb_pid: int | None = None
    uart_bridge_serial: str | None = None
    jtag_target_name: str | None = None
    jtag_cable_serial: str | None = None
    jtag_mode: str | None = None
    provenance: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- derived views

    @property
    def clock_ports(self) -> tuple[str, ...]:
        return CLOCK_PORTS[self.clock_kind]

    @property
    def host_ports(self) -> tuple[str, ...]:
        """The generated board top's port list, clock first, exactly as it is declared."""
        return self.clock_ports + HOST_PORTS

    def to_record(self) -> dict:
        return {
            "board_id": self.board_id, "part": self.part,
            "pins_profile": str(self.profile_path),
            "clock": {"kind": self.clock_kind, "pins": list(self.clock_pins),
                      "ports": list(self.clock_ports),
                      "iostandard": self.clock_iostandard,
                      "reference_mhz": self.reference_mhz,
                      "core_frequency_mhz": self.core_frequency_mhz},
            "reset": {"pin": self.reset_pin, "iostandard": self.reset_iostandard},
            "uart": {"rx_pin": self.uart_rx_pin, "tx_pin": self.uart_tx_pin,
                     "iostandard": self.uart_iostandard,
                     "host_bridge": {"usb_vid": self.uart_usb_vid,
                                     "usb_pid": self.uart_usb_pid,
                                     "serial_number": self.uart_bridge_serial}},
            "jtag": {"target_name": self.jtag_target_name,
                     "cable_serial": self.jtag_cable_serial, "mode": self.jtag_mode},
            "provenance": dict(self.provenance),
        }


def load_facts(profile: dict, profile_path: Path | str) -> BoardFacts:
    """Read and validate every board fact a host-path build needs."""
    path = Path(profile_path)
    where = f"board profile {path}"
    board_id = str(_require(profile, "board_id", where)) if profile.get("board_id") \
        else str(_require(profile, "id", where))
    fpga = profile.get("fpga") or {}
    part = fpga.get("part") or profile.get("part")
    if not part:
        raise BoardFactError(f"{where} declares no fpga.part")

    clock = _clock_block(profile, where)
    kind, pins = _clock_kind(clock, f"{where} board_pins.clock")
    clock_standard = _require(clock, "iostandard", f"{where} clock")
    reference_mhz = float(_require(clock, "frequency_mhz", f"{where} clock"))
    if reference_mhz <= 0:
        raise BoardFactError(f"{where} declares clock frequency_mhz={reference_mhz}")
    core_frequency_mhz = clock.get("core_frequency_mhz")
    if core_frequency_mhz is not None:
        core_frequency_mhz = float(core_frequency_mhz)
        if core_frequency_mhz <= 0:
            raise BoardFactError(f"{where} declares core_frequency_mhz={core_frequency_mhz}")
    clock_provenance = _provenance(clock, f"{where} clock")

    reset = (profile.get("board_pins") or {}).get("reset") or {}
    reset_pin = _require(reset, "pin", f"{where} board_pins.reset")
    reset_standard = _require(reset, "iostandard", f"{where} board_pins.reset")
    reset_provenance = _provenance(reset, f"{where} board_pins.reset")

    uart = (profile.get("peripherals") or {}).get("uart") or {}
    uart_provenance = _provenance(uart, f"{where} peripherals.uart")
    uart_rx = _require(uart, "fpga_rxd", f"{where} peripherals.uart")
    uart_tx = _require(uart, "fpga_txd", f"{where} peripherals.uart")
    uart_standard = _require(uart, "iostandard", f"{where} peripherals.uart")
    # Which USB bridge the FPGA's UART is wired to is a board fact, not a host fact: the
    # AX7A200B reaches a CP2102 (10c4:ea60) and the Nexys Video an FT232R (0403:6001), so a
    # capture that accepted whichever bridge happened to be on the port could be measuring
    # the wrong board's traffic.
    bridge = uart.get("host_bridge") or {}
    bridge_provenance = _provenance(bridge, f"{where} peripherals.uart.host_bridge")
    usb_vid = _require(bridge, "usb_vid", f"{where} peripherals.uart.host_bridge")
    usb_pid = _require(bridge, "usb_pid", f"{where} peripherals.uart.host_bridge")
    bridge_serial = bridge.get("serial_number")

    jtag = profile.get("jtag") or {}
    return BoardFacts(
        profile_path=path, board_id=board_id, part=str(part),
        clock_kind=kind, clock_pins=pins, clock_iostandard=str(clock_standard),
        reference_mhz=reference_mhz, core_frequency_mhz=core_frequency_mhz,
        reset_pin=str(reset_pin), reset_iostandard=str(reset_standard),
        uart_rx_pin=str(uart_rx), uart_tx_pin=str(uart_tx),
        uart_iostandard=str(uart_standard),
        uart_usb_vid=_usb_id(usb_vid, f"{where} peripherals.uart.host_bridge.usb_vid"),
        uart_usb_pid=_usb_id(usb_pid, f"{where} peripherals.uart.host_bridge.usb_pid"),
        uart_bridge_serial=str(bridge_serial) if bridge_serial else None,
        jtag_target_name=jtag.get("target_name"),
        jtag_cable_serial=jtag.get("cable_serial"),
        jtag_mode=jtag.get("mode"),
        provenance={"clock": clock_provenance, "reset": reset_provenance,
                    "uart": uart_provenance, "uart_host_bridge": bridge_provenance},
    )


def load_profile(path: Path | str) -> tuple[dict, BoardFacts]:
    import yaml

    profile_path = Path(path)
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8-sig")) or {}
    return profile, load_facts(profile, profile_path)


def facts_for_wrapper(wrapper: dict) -> BoardFacts:
    """The facts of the board a build record was made for.

    The profile path is read from the record rather than from a command line: the
    gate must check the pins against the board the artifact was built for, and a
    caller that could point the check at a different profile could pass a
    mismatched artifact off as verified.
    """
    board = wrapper.get("board") or {}
    profile_path = board.get("pins_profile")
    if not profile_path:
        raise BoardFactError(
            "build record carries no board.pins_profile: the board-facing pins cannot be "
            "checked against the profile they were generated from, and a check against a "
            "remembered pinout is not a check")
    return load_profile(Path(profile_path))[1]


def require_routed_uart(ports: dict, facts: BoardFacts) -> None:
    """The routed top must carry this board's UART pins in this board's standards."""
    for name, pin in (("uart_rx", facts.uart_rx_pin), ("uart_tx", facts.uart_tx_pin)):
        actual = (ports.get(name) or {}).get("pin")
        if actual != pin:
            raise ValueError(
                f"routed {name} is on {actual!r}, and the board profile "
                f"{facts.profile_path} ({facts.board_id}) declares {pin!r}")
        standard = (ports.get(name) or {}).get("iostandard")
        if standard != facts.uart_iostandard:
            raise ValueError(
                f"routed {name} IOSTANDARD is {standard!r}, and the board profile "
                f"{facts.profile_path} ({facts.board_id}) declares {facts.uart_iostandard!r}")


def require_usb_bridge(device, facts: BoardFacts) -> None:
    """The serial port being used must be this board's UART bridge, not another board's.

    `device` is a pyserial `ListPortInfo`.
    """
    serial_number = getattr(device, "serial_number", None)
    if facts.uart_bridge_serial and serial_number != facts.uart_bridge_serial:
        raise ValueError(
            f"the USB-UART on {getattr(device, 'device', '?')} reports serial "
            f"{serial_number!r}, and the board profile {facts.profile_path} "
            f"({facts.board_id}) declares {facts.uart_bridge_serial!r}")
    if (getattr(device, "vid", None), getattr(device, "pid", None)) != (
            facts.uart_usb_vid, facts.uart_usb_pid):
        vid, pid = getattr(device, "vid", None), getattr(device, "pid", None)
        found = (f"{vid:04x}:{pid:04x}" if isinstance(vid, int) and isinstance(pid, int)
                 else f"{vid}:{pid}")
        raise ValueError(
            f"the USB-UART on {getattr(device, 'device', '?')} is {found}, and the board "
            f"profile {facts.profile_path} ({facts.board_id}) declares this board's bridge "
            f"as {facts.uart_usb_vid:04x}:{facts.uart_usb_pid:04x}")


def require_recorded_board(record_board: dict, facts: BoardFacts) -> None:
    """The build record's own board facts must agree with the profile it names.

    The record is what every downstream gate reads, so a record that drifted from
    its profile would let a mismatch through every later check while still looking
    self-consistent.
    """
    def refuse(field, recorded, declared):
        raise ValueError(
            f"build record {field} is {recorded!r}, and the board profile "
            f"{facts.profile_path} ({facts.board_id}) declares {declared!r}")

    recorded_pins = record_board.get("pins") or {}
    recorded_clock = tuple(filter(None, (recorded_pins.get("clk_p"), recorded_pins.get("clk_n"))))
    if recorded_clock != facts.clock_pins:
        refuse("board.pins (clock)", recorded_clock, facts.clock_pins)
    recorded_mhz = recorded_pins.get("clk_mhz")
    if recorded_mhz is not None and float(recorded_mhz) != facts.reference_mhz:
        refuse("board.pins.clk_mhz", recorded_mhz, facts.reference_mhz)
    standard = (record_board.get("clock_pair") or {}).get("iostandard")
    if standard != facts.clock_iostandard:
        refuse("clock_pair.iostandard", standard, facts.clock_iostandard)
    reset = record_board.get("reset") or {}
    if reset.get("pin") != facts.reset_pin:
        refuse("reset.pin", reset.get("pin"), facts.reset_pin)
    if reset.get("iostandard") != facts.reset_iostandard:
        refuse("reset.iostandard", reset.get("iostandard"), facts.reset_iostandard)
    if record_board.get("part") not in (None, facts.part):
        refuse("part", record_board.get("part"), facts.part)
