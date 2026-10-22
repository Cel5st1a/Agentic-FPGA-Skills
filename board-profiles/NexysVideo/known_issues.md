# Nexys Video — known issues and traps (measured 2026-10-01)

## 1. "Same core" is not "same bitstream"

The AX7A200B and the Nexys Video both carry an Artix-7 `xc7a200t` die and both
report JTAG `idcode 13636093`. The parts are nevertheless different:

| fact | AX7A200B | Nexys Video |
| --- | --- | --- |
| part | `xc7a200tfbg484-2` | `xc7a200tsbg484-1` |
| package | FBG484 | **SBG484** |
| speed grade | `-2` | **`-1`** |
| system clock | R4/T4, 200 MHz **differential**, DIFF_SSTL15 | R4, 100 MHz **single-ended**, LVCMOS33 |
| UART (FPGA side) | rx L14 / tx L15 | rx **V18** / tx **AA19** |
| reset | F15, LVCMOS33 | **G4**, LVCMOS15 |
| JTAG | external HS1 on a 10-pin header | onboard Digilent USB-JTAG |

Consequences that must not be papered over:

* A bitstream built for one package is not a valid configuration for the other —
  the package decides which ball each port lands on.
* The `-1` speed grade is slower than `-2`. A design that met 10 ns on the
  AX7A200B is not thereby proven to meet it here; the timing report for *this*
  part is the only evidence.
* Therefore every model needs a *new build* for this board. Re-using an AX7A200B
  `.bit` is not a shortcut, it is a different (and wrong) artifact.

## 2. R4 is the sharpest trap

`R4` is a pin name in both profiles and means different things:

* AX7A200B: the **P side of a 200 MHz differential pair** (T4 is N), bank 34 at
  1.5 V, constrained `DIFF_SSTL15`.
* Nexys Video: a **single-ended 100 MHz LVCMOS33** input, bank 34 at 3.3 V.

Any code path that keys off the pin name alone, or that assumes "the clock is
differential because the board is an Artix-7", silently produces a design that
cannot be placed. The clock topology is declared by the profile
(`board_pins.clock.kind`) and must be validated, not inferred from the pin name.

## 3. Two-cable bench

On 2026-10-01 both cables were plugged in: the Nexys Video onboard cable
(`Digilent Nexys Video 210276BA00BFB`) and the AX7A200B's external
`Digilent JTAG-HS1 210512180081`, whose chain answered
`(error DR shift through all ones)` because that board was not powered.

A target must be selected **under the named cable**, not by die name across the
whole chain. Selecting by `name == "xc7a200t"` alone is ambiguous the moment both
boards are alive, and "there is exactly one `xc7a200t` in the chain" is a
statement about the bench, not about the board.

## 4. Volatile configuration

Configuration over the onboard USB-JTAG is volatile. The board boots from QSPI
Flash on power-up, so a power cycle (or a `PROG` press) discards the JTAG image
and the UART host path stops answering until the board is configured again. This
is the same board-session rule the AX7A200B work measured: configure (which *is*
the reset), prove liveness with one probe frame, then capture once.

## 5. COM number is not a board fact

The UART bridge is an FT232R that enumerates as a fresh Windows serial device
(`AV0KR4KQ` → `COM4` on 2026-10-01). The COM number can change between sessions;
the FPGA-side pins (V18 / AA19) cannot. Re-measure the port at the start of every
board session and prefer the FTDI serial number over the COM number when opening
it.
