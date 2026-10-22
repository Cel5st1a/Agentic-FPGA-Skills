---
name: vivado-manager
description: >
  Vivado project management: create projects, add sources, manage constraints,
  run synthesis/implementation, generate bitstreams. Executes Vivado Tcl
  commands non-interactively. Use for any Vivado IDE automation, batch
  processing of FPGA projects, or integrating Vivado into CI/CD pipelines.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# Vivado Manager

部署恢复与完成证据核验按 [转换与上板恢复规则](../docs/deployment-recovery.md) 执行。综合、实现/布线、bitstream 分别记录实际子进程与耗时，复合构建起点不能代替当前子阶段起点。流包装采用按元素排序的窄同步读 ROM；数组包装保持每样本一个宽字。面积或拥堵问题先核对层级报告与包装逻辑，不只看 HLS 估计；WNS/WHS 门禁保持冻结，负 WNS 不可接受。

## Purpose
Automate Vivado IDE operations via Tcl scripting. Create projects, add source
files, apply constraints, run implementation, and generate bitstreams —
all without launching the GUI.

## When to Use
- Creating a new Vivado project from HDL sources
- Adding XDC constraints, setting top module, running synth/impl
- Generating bitstream after HLS IP integration
- Batch-scripting Vivado workflows for CI/CD
- User says "Vivado project", "generate bitstream", "add constraints"

## When NOT to Use
- HLS/C model synthesis (use `vitis-build-agent`)
- Model conversion (use `hls4ml-convert`)
- Debugging timing failures (use `timing-closure-debugger`)

## Prerequisites
- Vivado 2025.2 on PATH (set via Windows env vars: `D:\vitis\2025.2\Vivado\bin`)
- Valid license or WebPack eligibility for target part
- Source files (Verilog, VHDL, SystemVerilog) or HLS IP
- Default target: **AX7A200B (XC7A200T-2FBG484I)** — constraints auto-loaded from `board-profiles/AX7A200B/`

## AX7A200B Default Configuration

When targeting AX7A200B, the following are automatically applied:
- Part: `xc7a200tfbg484-2`
- Default constraints: `board-profiles/AX7A200B/default_constraints.xdc`
- Clock: 200 MHz differential system clock, R4/T4, `DIFF_SSTL15` as declared with provenance in the profile (D-019)
- I/O standards per bank from board profile
- Post-implementation reports auto-generated: utilization, timing, DRC, power

## Quick Start

```bash
# The real entry point. It renders the Tcl templates with the run's paths, runs Vivado
# serially under the line's machine-level lock, and writes the resource/timing gate into
# the stage evidence.
python scripts/run_vivado.py --stage {synth|impl|bitstream} \
    --board <run>/p3-build/board \
    --deployment <run>/contract/<pkg>/deployment.yaml \
    --work <run>/p3-build/board \
    --evidence <run>/evidence/vivado-<stage>-evidence.json

# Board-level RTL simulation and the wrapper generator are separate scripts in this skill
python scripts/make_board_wrapper.py --deployment <contract> --verilog-dir <syn/verilog> \
    --top myproject --output <board> --board-profile <profile> --force
python scripts/run_board_sim.py --board <board> --deployment <contract> \
    --reference <csim log> --work <board> --evidence <evidence json>
```

**Do not run `templates/create_project.tcl` directly.** It is a template with `{{PLACEHOLDER}}`
values, rendered by `run_vivado.py`; there is no `scripts/create_project.tcl`.

```bash
# Open GUI (when visual debugging needed)
vivado &
```

## Tcl Templates

These live in `templates/` and are **rendered by `run_vivado.py`**, not run directly:
`create_project.tcl`, `run_synth.tcl`, `run_impl.tcl`, `write_bitstream.tcl`, `simulate.tcl`,
`sim_run.tcl`. There is no `templates/package_ip.tcl` in this tree.

### Create Project

```tcl
# templates/create_project.tcl — AX7A200B default
create_project my_project ./my_project -part xc7a200tfbg484-2
set_property target_language Verilog [current_project]
add_files -norecurse ./src/top.v
add_files -fileset constrs_1 -norecurse ./constraints/pinout.xdc
set_property top top_module [current_fileset]
update_compile_order -fileset sources_1
```

### Run Synthesis
```tcl
# templates/run_synth.tcl
open_project ./my_project/my_project.xpr
reset_run synth_1
launch_runs synth_1 -jobs 8
wait_on_run synth_1
open_run synth_1 -name synth_1
write_checkpoint -force ./checkpoints/post_synth.dcp
report_utilization -file ./reports/utilization.rpt
report_timing_summary -file ./reports/timing_synth.rpt
```

### Run Implementation & Bitstream
```tcl
# templates/run_impl.tcl
launch_runs impl_1 -jobs 8
wait_on_run impl_1
open_run impl_1 -name impl_1
write_checkpoint -force ./checkpoints/post_impl.dcp
report_timing_summary -file ./reports/timing_impl.rpt
report_utilization -file ./reports/utilization_impl.rpt
write_bitstream -force ./bitstream/top.bit
write_debug_probes -force ./bitstream/top.ltx
```

### Package as IP (not implemented in this tree)
```tcl
# There is no templates/package_ip.tcl. The line does not package IP: it builds a
# board-level top (`make_board_wrapper.py`) and writes a bitstream. The commands below are
# the standard Vivado sequence if IP packaging is ever wanted, but nothing here calls them.
ipx::package_project -root_dir ./ip_repo -vendor user.org -library hls -taxonomy /UserIP
set_property core_revision 1 [ipx::find_open_core user.org:hls:top_module:1.0]
ipx::create_xgui_files [ipx::find_open_core user.org:hls:top_module:1.0]
ipx::update_checksums [ipx::find_open_core user.org:hls:top_module:1.0]
ipx::save_core [ipx::find_open_core user.org:hls:top_module:1.0]
ipx::unload_core [ipx::find_open_core user.org:hls:top_module:1.0]
```

## Common Operations

| Task | Tcl Command |
|------|------------|
| List parts | `get_parts` |
| List boards | `get_boards` |
| Check license | `get_license` |
| Read IP catalog | `get_ipdefs` |
| Run DRC | `report_drc` |
| Open HW manager | `open_hw` |
| Program device | `program_hw_devices` |

## Failure Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `No license found` | Part not in WebPack | Switch to WebPack part, install license |
| `ERROR: [Synth 8-...]` | HDL syntax error | Fix source; run lint first |
| `[Place 30-...]` | Pin constraints wrong | Check XDC pin mapping |
| `[DRC ...]` | Design rule violation | Review DRC report |
| `write_bitstream` fails | Timing not met | Run `timing-closure-debugger` |

## Gotchas
- Vivado batch mode (`-mode batch`) is faster but gives less debug info than GUI
- Always `update_compile_order` after adding files
- Write checkpoint before major steps for rollback
- `jobs` number: match physical cores (8-16 typical for FPGA synth)
- XDC constraints are applied in order; later constraints can override earlier ones

## Rollback
- Checkpoint files (.dcp) at each major step enable rollback
- Project directory can be archived/deleted safely
- Source files and constraints are never modified by Vivado Manager
