---
name: vitis-build-agent
description: >
  Automated Vitis HLS build pipeline: csim, csynth, cosim, implementation,
  timing analysis. Parses solution reports, detects failures (timing,
  congestion, DSP overflow, memory bottleneck), and generates structured
  build reports. Use for any Vitis HLS build flow, after hls4ml conversion,
  or when debugging HLS synthesis failures.
allowed-tools: Bash, Read, Grep, Glob, Write
---

# Vitis Build Agent

按 [转换与上板恢复规则](../docs/deployment-recovery.md) 保留失败现场、检查 po2 中间类型、记录真实阶段状态并验证代理完成证据。对调度/流水候选核对实际 RTL 与 CSynth XML；选项接受或文件哈希变化不等于结构生效。数值失败停止后续阶段；不得放宽冻结容差或盲目增宽所有层。

## Purpose
Execute and monitor Vitis HLS builds with automatic failure detection and
report parsing. Cover the full flow: C simulation → C synthesis → co-simulation
→ implementation → timing analysis.

## When to Use
- After hls4ml generates HLS code (post-`hls4ml-convert`)
- User says "build HLS", "run synthesis", "csim", "csynth"
- HLS build failed and user needs root cause analysis
- Iterating on pragmas (pipeline, unroll, array_partition)

## When NOT to Use
- Model conversion (use `hls4ml-convert`)
- Vivado bitstream generation (use `bitstream-package`)
- Pure resource estimation (use `resource-estimator`)

## Prerequisites
- Vitis 2025.2 sourced: `source /d/vitis/2025.2/Vitis/settings64.sh` (or via Windows env)
- hls4ml-generated project with `build_prj.tcl`
- Target part known (e.g., `xczu3eg-sbva484-1-e`)

## Build Flow

### 1. C Simulation (csim)
```bash
python scripts/run_stage.py --stage csim --project hls_project --work csim_out \
    --deployment deployment.yaml --reference python-output.json
```
Validates functional correctness of generated HLS code against testbench.

### 2. C Synthesis (csynth)
```bash
python scripts/run_stage.py --stage csynth --project hls_project --work syn_out \
    --deployment deployment.yaml
```
Generates RTL, estimates resources and timing.

### 3. Co-Simulation (cosim, explicit diagnostic only)

This project has no successful traced-RTL cosim evidence. The default line excludes it;
`boardsim` supplies the per-value numerical RTL gate. Only request cosim explicitly:

```bash
python scripts/run_stage.py --stage cosim --project <hls-project> --work <cosim-work> \
    --deployment <deployment.yaml> --timeout 300
```

`run_line.py --stages cosim` uses a 300-second diagnostic budget by default
(`--timeout-cosim` overrides it). A C-wrapper-only result remains `NOT_RUN`, never RTL
`PASSED`. Keep its evidence even on timeout; do not retry to conceal a failed stage.

### 4. Implementation (impl)
```bash
python ../vivado-manager/scripts/run_vivado.py --help
```
Use `vivado-manager` for synthesis, route and bitstream of the board top. Follow its actual CLI and stage evidence; `vitis-run --impl` is not a supported invocation here.

### 5. Report Parsing

There is no `scripts/parse_reports.py` in this tree. Report parsing happens inside the
stages themselves, and the numbers land in their evidence files:

| what you want | where it is written |
| --- | --- |
| CSynth resource estimate | `evidence/csynth-evidence.json` → `resources_estimate` (labelled `csynth_estimate_not_the_resource_gate` — it is not the gate) |
| the CSynth report itself | `p2-convert/project/myproject_prj/solution1/syn/report/*_csynth.{rpt,xml}` |
| the gate verdict | `evidence/vivado-synth-evidence.json` / `vivado-impl-evidence.json` → `resource_gate` |

The **authority** on resources and timing is `vivado-manager`, not this skill. Note that
Vivado labels the LUT row `Slice LUTs*` (with a footnote asterisk) in some reports — a naive
exact-match parse drops it, which is exactly the defect recorded as D-N05.

## Output

```
build_results/
├── build_report.md          # Human-readable summary
├── build_report.json        # Machine-parseable status (for gate system)
├── timing_summary.txt        # Clock, WNS, TNS
├── resource_usage.csv        # DSP, BRAM, LUT, FF breakdown
├── latency_breakdown.txt     # Per-function/layer latency
└── warnings.txt              # All synthesis warnings parsed
```

### JSON Output Format (build_report.json)

Illustrative schema only: populate each status from its own run evidence. HLS estimates
are not the routed gate; an unrequested or C-wrapper-only cosim stays `NOT_RUN`.

```json
{
  "project": "my_hls_project",
  "target_part": "xc7a200tfbg484-2",
  "clock_period_ns": 5.0,
  "stages": {
    "csim": {"status": "PASS", "notes": ""},
    "csynth": {"status": "PASS", "notes": "HLS estimate only; see routed evidence for timing gate"},
    "cosim": {"status": "NOT_RUN", "notes": "Not requested; boardsim provides the RTL numerical gate"},
    "impl": {"status": "PASS", "notes": "WNS: 0.15 ns"}
  },
  "resources": {
    "dsp": {"used": 48, "available": 740, "pct": 6.5},
    "bram": {"used": 12, "available": 365, "pct": 3.3},
    "lut": {"used": 8234, "available": 134600, "pct": 6.1},
    "ff": {"used": 12456, "available": 269200, "pct": 4.6}
  },
  "performance": {
    "latency_cycles": 42,
    "latency_ns": 210,
    "ii": 1,
    "throughput_msps": null,
    "source": "HLS report; not measured adapter/UART performance"
  },
  "gate": "PASS"
}
```

Use `--json` flag to output only JSON to stdout.

## Report Format (build_report.md)

```markdown
=== Vitis HLS Build Report ===
Project: my_hls_project
Target Part: xczu3eg-sbva484-1-e
Clock: 5.0 ns (200 MHz)

| Stage      | Status  | Notes                        |
|------------|---------|------------------------------|
| csim       | PASS    | Output matches reference     |
| csynth     | PASS    | HLS estimate; routed gate is separate |
| cosim      | NOT_RUN | Not requested; use boardsim evidence |
| impl       | FAILED  | WNS: -0.12 ns; timing gate failed |

Resources:
| Resource | Used | Available | %     |
|----------|------|-----------|-------|
| DSP      | 48   | 252       | 19.0% |
| BRAM     | 12   | 216       | 5.6%  |
| LUT      | 8234 | 70560     | 11.7% |
| FF       | 12456| 141120    | 8.8%  |

HLS-reported latency: 42 cycles @ target 200 MHz = 210 ns
HLS-reported Initiation Interval: 1
Measured adapter/UART performance: NOT_RUN
```

## Failure Patterns

| Pattern | Symptom | Likely Cause | Fix |
|---------|---------|-------------|-----|
| Timing fail | WNS < 0 ns | Determine the actual critical path from routed reports | Keep the clock frozen; verify a new implementation attempt |
| DSP overflow | DSP used > contract limit | Attribute cost to compute or packaging | Evaluate legal per-layer RF in a new contract; preserve the model and gate |
| BRAM overflow | BRAM used > contract limit | Check ROM, FIFO and weight attribution | Diagnose packaging/storage first; preserve original model and quantization |
| csim/cosim fail | Output mismatch | Locate the first divergent intermediate type or layout | Verify the generated arithmetic against frozen model semantics; preserve precision and tolerance |
| csim hang | Infinite loop | Bug in testbench or HLS code | Check loop exit conditions |
| II violation | II > 1 | Data dependency in pipeline | Array partition, reshape |

## Gotchas
- Vitis 2025.2 uses `vitis-run --mode hls`, NOT the legacy `vivado_hls`
- On Windows, absolute paths in TCL scripts need forward slashes or escaped backslashes
- `cosim` requires Vivado Simulator (xsim) or ModelSim license
- First `csynth` run is slow (includes IP catalog update); subsequent runs are faster

## Rollback
- Keep failed build outputs and their evidence; retry affected stages in a fresh run rather than deleting the failure record.
- Original HLS source code is not modified
