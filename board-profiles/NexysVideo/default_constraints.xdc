# Nexys Video Default Constraints
# Source: Digilent Nexys-Video-Master.xdc (sha256 2debf0afb537c0a9f8f2c1569aa99e0366629a354f2fbdf369bd5142670086cc)
#         + board_files/nexys_video/A.0/1.2/part0_pins.xml (sha256 38c849c6c750b2ba6faaf7ad74e37a0ad9a177620587638f96d1213e4a5501e1)
# FPGA: XC7A200T-1SBG484C, SBG484-1
#
# Port names here are placeholders a generated top may rename; this file exists so
# that pin/IOSTANDARD pairs can be read back as data instead of retyped. The
# authoritative copy of these facts, with provenance, is board_profile.yaml.

# ---- System Clock (100 MHz single-ended oscillator) ----
# Single-ended by construction: there is no clk_n. Driving this pin through an
# IBUFDS (as the AX7A200B profile does, where R4 is the P side of a 200 MHz
# differential pair) would be wrong on this board even though the pin name matches.
set_property PACKAGE_PIN R4 [get_ports SYS_CLK]
set_property IOSTANDARD LVCMOS33 [get_ports SYS_CLK]

create_clock -period 10.0 -name sys_clk [get_ports SYS_CLK]

# ---- Reset (red CPU RESET button, active-low, bank 35 = 1.5 V) ----
set_property PACKAGE_PIN G4 [get_ports RESET_N]
set_property IOSTANDARD LVCMOS15 [get_ports RESET_N]

# ---- UART (FT232R bridge; named from the FPGA's side) ----
# uart_tx_in  = FPGA input  (host -> FPGA) -> V18
# uart_rx_out = FPGA output (FPGA -> host) -> AA19
set_property PACKAGE_PIN V18 [get_ports UART_RXD]
set_property IOSTANDARD LVCMOS33 [get_ports UART_RXD]
set_property PACKAGE_PIN AA19 [get_ports UART_TXD]
set_property IOSTANDARD LVCMOS33 [get_ports UART_TXD]

# ---- User LED (the status LED that keeps the model logic alive) ----
set_property PACKAGE_PIN T14 [get_ports LED1]
set_property IOSTANDARD LVCMOS25 [get_ports LED1]
