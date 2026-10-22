---
name: zynq-deploy
description: >
  Deploy bitstream and firmware to Zynq/Zynq MPSoC devices. Handles connection,
  programming, boot verification, and serial console. Supports JTAG, SD card,
  and QSPI boot modes. Use when deploying to PYNQ, Ultra96, ZCU104, or custom
  Zynq boards.
allowed-tools: Bash, Read, Grep
---

# Zynq Deploy

## ⚠️ Not used by this line, and it has no executable helpers

`ORDER.md` states that this skill is **explicitly not run** on this line: nothing is
programmed, no JTAG download happens, and every delivered package keeps
`programmed: false` and `hardware_verified: false`. A bitstream is not hardware
verification.

None of the scripts below exist in this tree either — `detect_device.tcl`,
`prepare_sd.sh`, `program_jtag.sh`, `verify_deployment.sh`. Treat this skill as background on
what on-board deployment *would* involve, not as a procedure to execute.

## Purpose
Reliable deployment of FPGA bitstreams and ARM firmware to Zynq devices.
Verify device is programmed correctly and booting as expected.

## When to Use
- After `bitstream-package` generates deployment artifacts
- User says "deploy to FPGA", "program device", "flash bitstream"
- Board bring-up after hardware connection
- Verifying device is alive and responding

## When NOT to Use
- Before bitstream is generated (generate first)
- Simulation/testing (use `vitis-build-agent`)
- Non-Zynq devices (pure FPGA devices need different flow)

## Prerequisites
- Vivado 2025.2 with Hardware Manager
- USB-JTAG cable connected and drivers installed
- Target board powered on
- Bitstream file (.bit) and optional .hwh/.xsa

## Boot Modes

| Mode | Requirements | Speed | Persistence |
|------|-------------|-------|-------------|
| JTAG | USB cable, Vivado HW Manager | Fast | Volatile |
| SD Card | FAT32 SD, BOOT.BIN | Medium | Persistent |
| QSPI Flash | JTAG + flash programmer | Slow | Persistent |

## Execution

**None of the scripts in this section exist in this tree, and this line does not deploy.**
They are shown to describe what on-board deployment would involve, not to be run.

### JTAG Deployment
```bash
# scripts/program_jtag.sh does not exist in this tree.
bash scripts/program_jtag.sh --bit top.bit --device xczu3_0
```

### SD Card Deployment
```bash
# scripts/prepare_sd.sh does not exist in this tree.
bash scripts/prepare_sd.sh --bit top.bit --hwh top.hwh --firmware app.elf --drive E:
```

### Verification
```bash
# scripts/verify_deployment.sh does not exist in this tree.
bash scripts/verify_deployment.sh --serial COM3 --baud 115200
```

## Verification Steps

1. **Device detection**: `vivado -mode batch -source scripts/detect_device.tcl` — that script
   does not exist in this tree; the line never programs a device.
2. **Program**: `program_hw_devices [lindex [get_hw_devices] 0]`
3. **Boot check**: Read serial output, look for "boot done" or custom heartbeat
4. **Register check**: Read known register values via AXI
5. **Loopback test**: Send test vector, verify output

## Gotchas
- Windows COM port numbering changes when connecting to different USB ports
- Zynq MPSoC requires both FSBL and bitstream for full boot
- SD card must be FAT32, not exFAT
- PYNQ boards need specific .hwh filename: `<board_name>.hwh`
- USB-JTAG drivers on Windows may need Zadig or WinDriver
- Xilinx Platform Cable USB II is most reliable; generic FTDI adapters may not work
