# run_synth.tcl -- rendered by vivado-manager/scripts/run_vivado.py
# Placeholders: {{ROOT}} {{PART}} {{TOP}} {{RTL_DIR}} {{XDC}} {{THREADS}}
set root [file dirname [file normalize [info script]]]
cd $root
set_param general.maxThreads {{THREADS}}
create_project {{PROJECT_NAME}} $root/implementation -part {{PART}} -force
set files [glob {{RTL_DIR}}/*.v]
add_files $files
read_xdc {{XDC}}
set_property top {{TOP}} [get_filesets sources_1]
update_compile_order -fileset sources_1
cd {{RTL_DIR}}
synth_design -top {{TOP}} -part {{PART}}
report_utilization -file $root/utilization_synth.rpt
report_timing_summary -file $root/timing_synth.rpt
write_checkpoint -force $root/synth.dcp
puts "AFD_SYNTH_DONE"
exit
