# AX7A200B Bringup Guide

## First-Time Board Setup

### 1. Inspect Board
- [ ] No visible damage on core board (60x60mm) or ext board
- [ ] JTAG connector (10-pin 2.54mm) accessible
- [ ] UART Mini-USB port accessible
- [ ] Power jumper set correctly (ext board powered, NOT J2)

### 2. Connect Cables
- [ ] Xilinx Platform Cable USB II → JTAG header
- [ ] Mini-USB cable → UART port
- [ ] 5V DC power → ext board power jack

### 3. Power On Sequence
- [ ] Connect power — PWR LED (red) should light immediately
- [ ] FPGA remains unconfigured until bitstream loaded
- [ ] After JTAG program: DONE LED (red) lights up

### 4. Verify USB-UART
```bash
# Windows: Check COM port in Device Manager
# Should see "Silicon Labs CP210x USB to UART Bridge (COMx)"
# Default: 115200-8-N-1
```

### 5. JTAG Detection
```bash
# From Vivado Hardware Manager:
open_hw
connect_hw_server
get_hw_targets
# Should see: xc7a200t_0
```

### 6. First Bitstream Test
```bash
# Program FPGA
vivado -mode batch -source scripts/program_ax7a200b.tcl
```

### 7. LED Test
- After programming: LED1 (core board user LED, W5) toggles if design drives it
- Extension board user LEDs (LED2-5) TBD

### 8. UART Loopback Test
- Send "Hello AX7A200B" from PC → FPGA should echo back (if design includes UART loopback)

## Known Working Configs

| Vivado | Part | JTAG Cable | Status |
|--------|------|-----------|--------|
| 2025.2 | xc7a200tfbg484-2 | Xilinx Platform Cable USB II | ✅ |

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| PWR LED off | No power | Check 5V supply, power switch |
| Device not found in HW Manager | JTAG cable issue | Check cable, drivers, try different USB port |
| DONE LED stays off after program | Program failed | Check bitstream, part compatibility |
| No UART output | Wrong COM port or baud | Check Device Manager, verify 115200 |
| FPGA overheating | Short circuit or overcurrent | Disconnect power immediately, inspect board |
