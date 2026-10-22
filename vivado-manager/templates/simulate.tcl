# simulate.tcl -- rendered by vivado-manager/scripts/run_board_sim.py
# Runs the board wrapper's testbench in xsim and leaves board_results.log in the
# simulation run directory. The testbench reads the captured result words out of the
# wrapper hierarchically, because the board top only exposes board-legal pins.
set root [file dirname [file normalize [info script]]]
cd $root
set_param general.maxThreads {{THREADS}}
create_project {{PROJECT_NAME}}_sim $root/sim -part {{PART}} -force
set files [glob {{RTL_DIR}}/*.v]
set tb {{RTL_DIR}}/{{TB}}
set files [lsearch -all -inline -not -exact $files $tb]
add_files $files
read_xdc {{XDC}}
set_property top {{TOP}} [get_filesets sources_1]
add_files -fileset sim_1 $tb
set_property file_type SystemVerilog [get_files $tb]
set_property top {{TB_TOP}} [get_filesets sim_1]
# Without this the simulator stops at its default 1000 ns runtime, which is less than one
# sample of this core, and the testbench's result file stays empty.
set_property -name {xsim.simulate.runtime} -value {all} -objects [get_filesets sim_1]
# Weight ROM images must sit beside the simulator's working directory, and the generated
# RTL asks for them with a relative path. The hook copies them there and then runs the
# simulation; without it every ROM reads nothing and the transaction never finishes.
set_property xsim.simulate.custom_tcl {{SIM_RUN_TCL}} [get_filesets sim_1]
launch_simulation
close_sim
exit
