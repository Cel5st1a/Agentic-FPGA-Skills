---
name: fpga-env-audit
description: >
  Full-system audit of FPGA+AI development environment. Checks OS, shell, PATH,
  Python, venv, CUDA, GPU driver, Vivado, Vitis, Vitis HLS, hls4ml, TensorFlow,
  PyTorch, ONNX, CMake, gcc/g++, VS Code, licenses, USB/JTAG. Outputs OK/WARN/
  FAIL/UNKNOWN table with remediation steps. Use when setting up a new machine,
  after toolchain updates, troubleshooting import errors, or before starting
  any FPGA+AI project.
allowed-tools: Bash, Read, Grep, Glob, WebSearch
---

# FPGA Environment Audit

## Purpose
Audit the complete FPGA+AI development environment in a single pass. Detect
version conflicts, missing dependencies, path pollution, duplicate installs,
and license issues before they cause build failures.

## When to Use
- First-time setup of an FPGA development machine
- After installing or upgrading Vivado/Vitis/hls4ml
- When `import hls4ml`, `import tensorflow`, or `import torch` fails
- Before starting a new FPGA+AI project
- CI/CD pipeline environment validation

## When NOT to Use
- During active development (use `compatibility-checker` for quick checks)
- For single-package version queries (use `pip show` directly)
- On machines without AMD tools installed

## Prerequisites
- Bash shell (Git Bash on Windows, or Linux shell)
- Python 3.8+ on PATH
- Vivado/Vitis installed (or explicitly skipped)

## Execution Steps

### Phase A: System Baseline
```bash
# Run the audit script
bash scripts/audit_system.sh
```

Checks:
1. OS version, architecture, memory, disk free space
2. Shell type and version ($SHELL, bash --version)
3. PATH entries (duplicates, broken paths, order conflicts)
4. XILINX_VIVADO, XILINX_VITIS env vars (source and correctness)
5. NVIDIA driver, CUDA version, GPU compute capability

### Phase B: AMD Toolchain
```bash
# `scripts/audit_amd_tools.sh` does not exist in this tree. Check the toolchain directly --
# and note the evidence each of these produces is what the run's p1-env/ records:
vivado -version                       # Vivado binary + version
vitis -version                        # Vitis / vitis-run availability
echo "$XILINX_VIVADO $XILINX_VITIS"   # settings sourced, and whether legacy XILINX_* leak
```

Checks:
1. Vivado binary exists and `vivado -version` succeeds
2. Vitis binary exists and launch check passes
3. Vitis HLS (`vitis-run --mode hls`) availability
4. settings64.sh/.bat existence and correct paths
5. Old Xilinx env var pollution (check for legacy `XILINX_*` paths)
6. Duplicate installations (compare /d/vitis vs /d/vivado etc)
7. License status (FlexLM vars, WebPack eligibility)

### Phase C: Python & AI Environment
```bash
bash scripts/audit_python_env.sh
```

Checks:
1. Python version(s) on PATH and on disk
2. pip/conda/venv availability
3. TensorFlow version, GPU support, Keras type (v2/v3)
4. PyTorch version, CUDA availability, compute capability match
5. hls4ml version, available backends, converter status
6. protobuf version and TF/ONNX compatibility
7. numpy version (<2.0 for TF compat)
8. Environment isolation (venv/conda separation)
9. Package conflicts (Keras v2/v3, protobuf, numpy)

### Phase D: Tools & Connectivity
```bash
# `scripts/audit_tools.sh` does not exist in this tree. Nothing in the line depends on it:
# the audit commands the line actually runs are `audit_system.sh`, `audit_python_env.sh`
# (both in this skill) and `compatibility-checker/scripts/check_compat.sh`, which is what
# `bootstrap_run.py` writes into each run's RUN.md.
bash scripts/audit_system.sh
bash scripts/audit_python_env.sh
bash ../compatibility-checker/scripts/check_compat.sh
```

## Output Format

```
=== FPGA+AI Environment Audit Report ===
Date: YYYY-MM-DD HH:MM
Machine: HOSTNAME

| Category      | Item              | Status | Detail              |
|---------------|-------------------|--------|---------------------|
| OS            | Windows 11 Pro    | OK     | 10.0.22631 x64      |
| Memory        | 32GB              | OK     | 18GB available      |
| GPU           | RTX 5060 Ti       | OK     | Driver 581.29       |
| CUDA          | Toolkit           | FAIL   | nvcc not in PATH    |
| Vivado        | 2025.2            | OK     | v2025.2, tool 2025.11|
| Vitis         | 2025.2            | OK     | Launch confirmed     |
| Vitis HLS     | Integrated        | OK     | vitis-run --mode hls|
| Python        | 3.10.11           | OK     | System install       |
| TF (convert)  | 2.13.1            | OK     | Keras v2, pb 3.20.3 |
| PyTorch       | 2.11.0+cu128      | OK     | CUDA GPU confirmed   |
| hls4ml        | 1.3.0             | WARN   | PyTorch backend bug  |
| VS Code       | 1.109.5           | OK     | Python/C++/CMake     |
| License       | N/A               | UNKNOWN| No FlexLM vars       |
| JTAG/USB      | N/A               | UNKNOWN| No device connected  |

Summary: X OK / Y WARN / Z FAIL / W UNKNOWN
```

## Failure Handling

| Failure | Likely Cause | Fix |
|---------|-------------|-----|
| `vivado: command not found` | PATH not set | Source settings64.sh or check Windows env vars |
| `import tensorflow` fails | Corrupted install, missing __init__.py | Reinstall in clean venv |
| CUDA not available | CPU-only PyTorch wheel | Install cu128 wheel from pytorch.org |
| hls4ml import fails | Missing protobuf, pyparsing, TF version mismatch | See known-issues.md |
| Duplicate Vivado installs | Vitis+Vivado vs Vivado-only | Remove redundant (keep full Vitis install) |

## Gotchas
- On Windows Git Bash, `XILINX_VITIS` set via Windows User env vars, not shell init
- TF 2.13's `keras.__version__` is not accessible directly; use `tf.__version__`
- NVIDIA driver CUDA version ≠ installed CUDA Toolkit version
- PyTorch 2.11 cu128 supports Blackwell SM120; older versions do NOT
- hls4ml PyTorch backend has `VivadoInput` subscriptable bug in v1.3.0 write()

## Rollback
- Audit is read-only; no files modified
- Report saved to `~/fpga_audit_YYYYMMDD.md`
- Previous audits kept for comparison
