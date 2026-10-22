---
name: log-analyzer
description: >
  Parse and classify errors from Vivado, Vitis HLS, hls4ml, TensorFlow, and
  PyTorch logs. Extracts ERROR, CRITICAL WARNING, timing failures, segfaults,
  license issues, OOM, and unsupported ops. Provides prioritized fix
  recommendations. Use when build/conversion fails or user pastes error logs.
allowed-tools: Bash, Read, Grep, Glob
---

# Log Analyzer

## Purpose
Quickly parse FPGA+AI toolchain logs and extract actionable error information
with prioritized fix recommendations. Reduce log-diving from minutes to seconds.

## When to Use
- Any build or conversion failure
- User pastes error log output
- "What does this error mean?"
- CI pipeline failed and need root cause
- Large log files that are hard to scan manually

## When NOT to Use
- Warnings without errors (informational only)
- Performance analysis (use `timing-closure-debugger`)
- Resource analysis (use `resource-estimator`)

## Execution

```bash
# Parse a log file
bash scripts/analyze_log.sh path/to/logfile.log

# Parse piped input
cat build.log | bash scripts/analyze_log.sh -

# Where the logs of a run actually are:
#   <run>/logs/                      line console + csim/board raw output
#   <run>/p3-build/board/vivado.log  synthesized/implemented run log
#   <run>/p3-build/csynth/stdout.txt CSynth log
# `scripts/parse_vitis_log.py` does not exist in this tree; feed the log to
# analyze_log.sh instead.
```

## Error Patterns & Fixes

### Vitis HLS / Vivado HLS

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `ERROR: [HLS 200-70]` | Unsupported C construct | Remove or rewrite the construct |
| `ERROR: [HLS 200-111]` | Function not inlined | Add INLINE pragma |
| `ERROR: [SYNCHK 200-...]` | Interface synth check fail | Check top-level function signature |
| `CRITICAL WARNING: [HLS 200-...]` | Usually Vivado HLS specific | Check UG902 for the message code |
| `Timing not met` / `negative slack` | Clock constraint violation | See `timing-closure-debugger` |
| `cannot find license` | License issue | Check LM_LICENSE_FILE, FlexLM |
| `Segmentation fault` | Memory bug in C testbench | Run with valgrind or add bounds checks |

### hls4ml

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `Unsupported operation type: XXX` | ONNX op not handled | Use Keras path or register custom handler |
| `ModelGraph.__init__() takes N args` | API mismatch | Check hls4ml version compatibility |
| `'VivadoInput' object is not subscriptable` | PyTorch backend write() bug | Use Keras bridge |
| `No module named 'pyparsing'` | Missing QKeras dependency | `pip install pyparsing` |
| `No module named 'tensorflow.keras'` | Keras v2/v3 conflict or TF corrupt | Rebuild venv, pin TF 2.13 |
| `ImportError: Keras cannot be imported` | TF 2.15+ needs `tf_keras` | Use TF ≤2.13 for Keras v2 built-in |

### TensorFlow / PyTorch

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `Could not find cudnn64_8.dll` | Missing cuDNN | Install cuDNN or use CPU-only build |
| `CUDA out of memory` | GPU memory exhausted | Reduce batch size, use gradient accumulation |
| `NVIDIA GeForce RTX 5060 Ti with CUDA capability sm_120 is not compatible` | PyTorch too old for Blackwell | Upgrade to torch 2.7+ (SM120 support) |
| `protobuf version conflict` | TF/ONNX/hls4ml protobuf range mismatch | Pin protobuf==3.20.3 |

### Vivado

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `[Place 30-574]` | IO placement invalid | Check pin constraints in XDC |
| `[DRC 23-20]` | Design rule violation | Review DRC details |
| `[Timing 38-282]` | Clock not properly defined | Add create_clock in XDC |
| `[Opt 31-67]` | Optimization failed | Check synthesis settings |

### Cosimulation (xsim)

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `ERROR: [XSIM 43-...]` | Simulation engine error | Check testbench, signal naming |
| `Mismatch at time` | Cosim output doesn't match C | RTL functional bug — check precision, pipeline depth |
| `xsim: command not found` | Vivado Simulator not on PATH | Source settings64.sh or switch to ModelSim |
| `No license found for xsim` | Vivado Simulator license missing | Use ModelSim or check WebPack eligibility |
| `FATAL_ERROR: Iterator_overflow` | Cosim timeout or infinite loop | Increase timeout, check loop bounds |
| `# ** Error: (vsim-...)` | ModelSim-specific error | Check ModelSim license and installation |

### Board Communication (UART/JTAG)

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `SerialException: could not open port` | COM port busy or wrong | Check device manager, close other terminals |
| `PermissionError: Access is denied` | Port in use by another app | Close PuTTY/TeraTerm/Vivado HW Manager |
| `Timeout waiting for response` | FPGA not responding | Check power, reset, baud rate (115200), UART pins |
| `Checksum mismatch` | Data corruption in transmission | Reduce baud rate, check cable, add retry |
| `PROGRAM_FAILED` | JTAG programming failed | Check JTAG connection, power cycle FPGA |
| `connect_hw_server failed` | Vivado HW server not running | Check hw_server.exe is running, firewall not blocking |

### Physics Validation

| Pattern | Explanation | Fix |
|---------|------------|-----|
| `Physics: peak_error > 5%` | Amplitude reconstruction poor | Check precision, check activation function fidelity |
| `Physics: timing_error > 1 sample` | Temporal alignment off | Check pipeline latency accounting, sync signals |
| `Physics: integral_error > 2%` | Charge measurement inaccurate | Increase fixed-point integer bits, check bias terms |
| `Physics: pileup_fidelity < 0.9` | Overlapping pulses not resolved | Increase model capacity, check pulse separation logic |
| `Physics: noise floor elevated` | Quantization noise dominant | Increase bit width, check dithering |

## Output

```
=== Log Analysis Report ===
Source: vitis_hls.log (2,847 lines)
Scan time: 0.3s

ERROR: 3
  [HLS 200-111] Function 'compute_kernel' not inlined
    → Fix: Add #pragma HLS INLINE to function
  [HLS 200-70] Unsupported operation in line 156
    → Fix: Remove pointer reinterpret_cast
  Timing not met (-0.42 ns WNS) at 200 MHz
    → Fix: Increase pipeline depth or reduce clock

CRITICAL WARNING: 2
  [SYNTH 8-...] Black-box module 'dma_engine' has no implementation
    → Info: Will be resolved during IP integration

WARNING: 15 (non-critical, see full report)

Summary: 3 errors found. Top priority fix: inline compute_kernel.
Estimated fix time: 5-10 min.
```

## Severity Classification

| Severity | Pattern | Action |
|----------|---------|--------|
| FATAL | Segfault, OOM, license missing | Stop immediately |
| ERROR | Synth fail, missing file, API incompat | Fix then retry |
| CRITICAL WARNING | Usually hard error in disguise | Review each |
| WARNING | May indicate suboptimal result | Review batch |
| INFO | Informational | Ignore unless debugging |

## Gotchas
- "CRITICAL WARNING" in Vivado is often a hard error — don't ignore
- hls4ml PyTorch backend warnings about "Failed to import handlers" are normal
- TensorBoard deprecation warnings in TF 2.13 are benign
- oneAPI warning messages differ from Vivado; separate parser needed
