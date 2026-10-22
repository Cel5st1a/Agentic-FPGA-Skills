# AX7A200B Default Constraints
# Source: 黑金AX7A200B开发板用户手册REV1.0.pdf
# FPGA: XC7A200T-2FBG484I, FBG484-2

# ---- System Clock (200MHz LVDS oscillator) ----
# NOTE: the manual's own XDC says LVDS, but bank 34's VCCO is 1.5 V and Vivado rejects
# LVDS there, leaving both ports at IOSTANDARD DEFAULT and blocking write_bitstream.
# board_profile.yaml carries the validated override (DIFF_SSTL15, from the vendor's
# uart_test.xdc / IO report) with provenance; tools read it from there (defect D-019).
set_property PACKAGE_PIN R4 [get_ports SYS_CLK_P]
set_property IOSTANDARD LVDS [get_ports SYS_CLK_P]
set_property PACKAGE_PIN T4 [get_ports SYS_CLK_N]
set_property IOSTANDARD LVDS [get_ports SYS_CLK_N]

create_clock -period 5.0 -name sys_clk [get_ports SYS_CLK_P]

# ---- Reset (active-low, push button) ----
# TBD: pin from ext board; uncomment when known
# set_property PACKAGE_PIN ? [get_ports RESET_N]
# set_property IOSTANDARD LVCMOS33 [get_ports RESET_N]
# set_property PULLUP true [get_ports RESET_N]

# ---- UART ----
set_property PACKAGE_PIN L15 [get_ports UART_TXD]
set_property IOSTANDARD LVCMOS33 [get_ports UART_TXD]
set_property PACKAGE_PIN L14 [get_ports UART_RXD]
set_property IOSTANDARD LVCMOS33 [get_ports UART_RXD]

# ---- User LED (core board LED1) ----
set_property PACKAGE_PIN W5 [get_ports LED1]
set_property IOSTANDARD LVCMOS15 [get_ports LED1]

# ---- QSPI Flash (for boot configuration) ----
set_property PACKAGE_PIN L12 [get_ports QSPI_CLK]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_CLK]
set_property PACKAGE_PIN T19 [get_ports QSPI_CS]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_CS]
set_property PACKAGE_PIN P22 [get_ports QSPI_DQ0]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_DQ0]
set_property PACKAGE_PIN R22 [get_ports QSPI_DQ1]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_DQ1]
set_property PACKAGE_PIN P21 [get_ports QSPI_DQ2]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_DQ2]
set_property PACKAGE_PIN R21 [get_ports QSPI_DQ3]
set_property IOSTANDARD LVCMOS33 [get_ports QSPI_DQ3]

# ---- I2C (EEPROM + Temp Sensor shared bus) ----
set_property PACKAGE_PIN M15 [get_ports I2C_SCL]
set_property IOSTANDARD LVCMOS33 [get_ports I2C_SCL]
set_property PACKAGE_PIN M16 [get_ports I2C_SDA]
set_property IOSTANDARD LVCMOS33 [get_ports I2C_SDA]

# ---- JTAG ----
set_property PACKAGE_PIN V12 [get_ports FPGA_TCK]
set_property IOSTANDARD LVCMOS33 [get_ports FPGA_TCK]
set_property PACKAGE_PIN R13 [get_ports FPGA_TDI]
set_property IOSTANDARD LVCMOS33 [get_ports FPGA_TDI]
set_property PACKAGE_PIN U13 [get_ports FPGA_TDO]
set_property IOSTANDARD LVCMOS33 [get_ports FPGA_TDO]
set_property PACKAGE_PIN T13 [get_ports FPGA_TMS]
set_property IOSTANDARD LVCMOS33 [get_ports FPGA_TMS]

# ---- FPGA Configuration ----
set_property BITSTREAM.CONFIG.CCLK CLK12 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property CONFIG_MODE SPIx4 [current_design]
