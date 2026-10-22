# AX7A200B Known Issues

## #1: No Zynq PS — Pure FPGA Only
**Status**: Design constraint
**Impact**: Zynq-deploy skill NOT applicable. No ARM CPU, no PYNQ, no Linux on PS.
**Mitigation**: Use pure FPGA bitstream deployment. For soft-CPU use MicroBlaze.

## #2: BANK34 IO at 1.5V
**Status**: Hardware design
**Impact**: 15 IO pins on CON1 are 1.5V (DDR3 bank shared). Cannot use as 3.3V GPIO.
**Pins affected**: CON1 pins 35-48, 54-58

## #3: BANK15/BANK16 Voltage via LDO
**Status**: Adjustable
**Impact**: Default 3.3V via SPX3819M5-3-3. Can be changed by swapping LDO chip.
**Note**: Changing LDO requires soldering.

## #4: Dual Power Warning
**Status**: Hardware risk
**Impact**: DO NOT power core board J2 and ext board simultaneously. May cause damage.

## #5: GTP Reference Clock
**Status**: Design note
**Impact**: GTP refclk is 125MHz from SiT9102 on core board. PCIe requires 100MHz from host slot. Different transceivers may need different refclks.

## #6: LED/Key Pins Not in Core Manual
**Status**: Incomplete documentation
**Impact**: Extension board LED2-5 and 4 user keys + reset key FPGA pins are NOT in the core board manual (pages 1-27).
**Mitigation**: Check ext board schematic in CD directory or ALINX-provided examples.

## #7: Default Part in Vivado 2025.2
**Status**: Verified
**Impact**: `xc7a200tfbg484-2` IS installed in Vivado 2025.2 (Artix-7 family).
**Verification**: Run `get_parts` in Vivado Tcl console.
