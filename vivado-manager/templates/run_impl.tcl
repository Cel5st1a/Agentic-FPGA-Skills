# run_impl.tcl -- rendered by vivado-manager/scripts/run_vivado.py
# Opens the synthesis checkpoint, implements, and gates on timing and DRC: a routed
# design with negative slack or DRC violations is not a deliverable, so the stage fails
# instead of recording a bitstream.
set root [file dirname [file normalize [info script]]]
cd $root
set_param general.maxThreads {{THREADS}}
open_checkpoint $root/synth.dcp
opt_design
place_design
phys_opt_design
route_design
report_timing_summary -report_unconstrained -file $root/timing_routed.rpt
report_utilization -file $root/utilization_routed.rpt
report_drc -file $root/drc_routed.rpt
report_clocks -file $root/clocks.rpt
write_checkpoint -force $root/routed.dcp

set setup_paths [get_timing_paths -delay_type max -max_paths 1]
set hold_paths  [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup_paths] == 0 || [llength $hold_paths] == 0} { error "Missing timing paths" }
set setup_slack [get_property SLACK $setup_paths]
set hold_slack  [get_property SLACK $hold_paths]
puts "AFD_SETUP_SLACK $setup_slack"
puts "AFD_HOLD_SLACK $hold_slack"
set drc_errors [get_drc_violations -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
puts "AFD_DRC_ERRORS [llength $drc_errors]"
if {$setup_slack < 0 || $hold_slack < 0} { error "Timing requirements not satisfied" }
if {[llength $drc_errors] != 0} { error "DRC errors remain" }
puts "AFD_ROUTE_COMPLETE"
exit
