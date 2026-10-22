# Program AX7A200B via JTAG
# Usage: vivado -mode batch -source program_board.tcl -tclargs bitstream.bit

set bitfile [lindex $argv 0]
if {$bitfile eq ""} { set bitfile "./bitstream/top.bit" }

puts "=== AX7A200B Programmer ==="
puts "Target: xc7a200tfbg484-2"
puts "Bitstream: $bitfile"

open_hw
connect_hw_server
set targets [get_hw_targets]

if {[llength $targets] == 0} {
    puts "ERROR: No hardware targets found. Check JTAG cable and board power."
    exit 1
}

puts "Found target: [lindex $targets 0]"
open_hw_target [lindex $targets 0]

set devices [get_hw_devices]
if {[llength $devices] == 0} {
    puts "ERROR: No devices found in JTAG chain."
    exit 1
}

set device [lindex $devices 0]
puts "Programming device: $device"
set_property PROGRAM.FILE $bitfile $device
program_hw_devices $device

puts "Program complete. Check DONE LED (should be ON)."
close_hw
