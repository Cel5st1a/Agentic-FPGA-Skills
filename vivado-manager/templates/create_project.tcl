# create_project.tcl -- rendered by vivado-manager/scripts/run_vivado.py
# Placeholders: {{PROJECT_NAME}} {{ROOT}} {{PART}} {{TOP}} {{RTL_DIR}} {{XDC}}
set root [file dirname [file normalize [info script]]]
cd $root
set_param general.maxThreads {{THREADS}}
create_project {{PROJECT_NAME}} $root/implementation -part {{PART}}
set files [glob {{RTL_DIR}}/*.v]
add_files $files
read_xdc {{XDC}}
set_property top {{TOP}} [get_filesets sources_1]
update_compile_order -fileset sources_1
puts "AFD_PROJECT_CREATED"
exit
