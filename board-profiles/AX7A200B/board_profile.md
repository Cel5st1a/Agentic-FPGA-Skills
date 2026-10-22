# AX7A200B Board Profile

> **Default target for all FPGA+AI development.**
> Source: 黑金AX7A200B开发板用户手册REV1.0.pdf (49 pages)
> Location: D:\FPGA_AX7A200_develop\AX7A200\CD\

---

## Quick Reference

| Field | Value |
|-------|-------|
| Board Name | 黑金 AX7A200B |
| Vendor | ALINX (芯驿电子科技) |
| FPGA Core | XC7A200T-2FBG484I |
| Logic Cells | 215,360 |
| DSP Slices | 740 |
| BRAM (Kb) | 13,140 |
| GTP Transceivers | 4 (6.6 Gb/s each) |
| DDR3 | 8Gbit (2x 4Gbit, 32-bit, 800Mbps) |
| QSPI Flash | 128Mbit |
| System Clock | 200 MHz LVDS (R4/T4) |
| GTP Ref Clock | 125 MHz LVDS (F6/E6) |
| Default Flow | TensorFlow → hls4ml → Vitis → AX7A200B |

---

## FPGA Part String

```
xc7a200tfbg484-2
```

Vivado project part:
```tcl
set_part xc7a200tfbg484-2
```

---

## Clock Map

| Signal | Freq | Type | P Pins | N Pins | Bank |
|--------|------|------|--------|--------|------|
| SYS_CLK | 200 MHz | LVDS | R4 | T4 | BANK34 MRCC |
| MGT_CLK0 | 125 MHz | LVDS | F6 | E6 | BANK216 MGTREFCLK0 |

---

## Key Peripheral Pin Map

### LEDs
| LED | Pin | Note |
|-----|-----|------|
| LED1 (core user) | W5 | Active-high, BANK34 |
| LED2-5 (ext) | TBD | Extension board |

### UART
| Signal | Pin | Direction |
|--------|-----|-----------|
| UART1_RXD | L14 | FPGA ← PC |
| UART1_TXD | L15 | FPGA → PC |

### JTAG
| Signal | Pin |
|--------|-----|
| TCK | V12 |
| TDI | R13 |
| TDO | U13 |
| TMS | T13 |

### QSPI Flash
| Signal | Pin |
|--------|-----|
| QSPI_CLK | L12 (CCLK_0) |
| QSPI_CS | T19 (FCS_B_14) |
| QSPI_DQ0 | P22 (D00_MOSI_14) |
| QSPI_DQ1 | R22 (D01_DIN_14) |
| QSPI_DQ2 | P21 |
| QSPI_DQ3 | R21 |

### I2C (EEPROM / Temp)
| Signal | Pin |
|--------|-----|
| I2C_SCL | M15 |
| I2C_SDA | M16 |

---

## Default hls4ml Config

```yaml
Backend: Vivado
Part: xc7a200tfbg484-2
ClockPeriod: 5.0
ClockUncertainty: 12.5%
IOType: io_parallel
```

---

## Development Flow

```
TensorFlow/Keras model
  → hls4ml convert (part=xc7a200tfbg484-2)
  → HLS C++ code generated
  → Vitis HLS (csim → csynth → cosim)
  → Vivado (implementation → bitstream)
  → JTAG download to AX7A200B
  → USB-UART verify (115200 baud)
```

---

## Power Notes

- Input: DC 5V via ext board or core board J2
- DO NOT power both core J2 and ext board simultaneously
- Power sequence: 1.0V → 1.8V → (1.5V, 3.3V, VCCO)
- GTP: 1.0V → MGTAVCC → MGTAVTT

---

## Unknown / TBD

| Item | Status |
|------|--------|
| Extension board LED2-5 exact pins | TBD — need ALINX ext board schematic |
| 4 user keys + 1 reset key pins | TBD |
| 40-pin expansion header (J11/J12) pinout | TBD |
| ALINX-provided .xdc constraint file | TBD — check CD directory |
