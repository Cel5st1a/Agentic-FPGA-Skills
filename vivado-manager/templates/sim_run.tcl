# sim_run.tcl -- xsim custom hook, rendered by vivado-manager/scripts/run_board_sim.py
# The generated RTL loads its weight ROMs with $readmemh("./<name>.dat"), a path the
# simulator resolves against its own working directory. Those files live in the RTL
# directory, so without this copy every ROM reads nothing, the core's state is X and the
# transaction never finishes (measured: testbench timed out at 50 ms with an empty
# result file). The custom Tcl runs inside the xsim session, so this is the right place.
# `puts` is unusable here: inside the xsim custom-Tcl context the stdout channel is not
# defined, and a failed puts aborts this hook before `run all` (measured: the simulation
# then closed without ever running the testbench).
set rtl_dir "{{RTL_DIR}}"
foreach data_file [glob -nocomplain -directory $rtl_dir *.dat] {
    file copy -force $data_file [file tail $data_file]
}
run all
