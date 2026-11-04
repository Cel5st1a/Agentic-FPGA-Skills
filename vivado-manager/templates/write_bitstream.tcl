# write_bitstream.tcl -- rendered by vivado-manager/scripts/run_vivado.py
# Timing is retained as a diagnostic; write_bitstream's own failures propagate.
set root [file dirname [file normalize [info script]]]
open_checkpoint $root/routed.dcp
set setup_paths [get_timing_paths -delay_type max -max_paths 1]
set hold_paths  [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup_paths] == 0 || [llength $hold_paths] == 0} {
    puts "AFD_TIMING_DIAGNOSTIC missing_paths"
} else {
    puts "AFD_SETUP_SLACK [get_property SLACK $setup_paths]"
    puts "AFD_HOLD_SLACK [get_property SLACK $hold_paths]"
}
write_bitstream -force {{BITSTREAM}}
puts "AFD_BITSTREAM_WRITTEN"
exit
