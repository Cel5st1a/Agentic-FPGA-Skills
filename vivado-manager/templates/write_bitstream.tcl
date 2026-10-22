# write_bitstream.tcl -- rendered by vivado-manager/scripts/run_vivado.py
# The routed checkpoint must still meet timing when it is opened here: the bitstream is
# written only from a design whose slack is non-negative.
set root [file dirname [file normalize [info script]]]
open_checkpoint $root/routed.dcp
set setup_paths [get_timing_paths -delay_type max -max_paths 1]
set hold_paths  [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup_paths] == 0 || [llength $hold_paths] == 0} { error "Missing timing evidence" }
if {[get_property SLACK $setup_paths] < 0 || [get_property SLACK $hold_paths] < 0} {
    error "Timing violation"
}
write_bitstream -force {{BITSTREAM}}
puts "AFD_BITSTREAM_WRITTEN"
exit
