---
name: compatibility-checker
description: >
  Pre-build compatibility check for FPGA+AI toolchains. Validates version
  compatibility across Vivado, Vitis, hls4ml, TensorFlow, PyTorch, Python,
  Keras (v2/v3), protobuf, numpy, ONNX. Flags supported/experimental/risky/
  broken combinations. Use before any hls4ml conversion, HLS build, or
  environment change.
allowed-tools: Bash, Read, Grep, WebSearch
---

# Compatibility Checker

## Purpose
Prevent build failures by validating toolchain compatibility BEFORE running
hls4ml conversion or HLS synthesis. Maintains a version matrix based on
official docs and verified configurations.

## When to Use
- Before `hls4ml convert` or `vitis-run --mode hls`
- After upgrading any component (TF, PyTorch, hls4ml, Vivado)
- When switching between projects with different version requirements
- Troubleshooting import errors or conversion failures

## When NOT to Use
- For full system audit (use `fpga-env-audit`)
- For single-package version check (use `pip show`)
- Post-build analysis (use `log-analyzer`)

## Version Compatibility Matrix

### TensorFlow ↔ hls4ml

| TF Version | Keras | hls4ml | Protobuf | Status |
|-----------|-------|--------|----------|--------|
| 2.21.x | Keras 3 | 1.3.0+ | 3.19.6+ | ⚠️ experimental |
| 2.16.x | Keras 3 | 1.2.0+ | 3.20.3+ | ⚠️ experimental |
| 2.15.x | Keras 2 | 1.0.0+ | 3.20.3 | ✅ supported |
| **2.13.x** | **Keras 2** | **0.8.1-1.3.0** | **3.20.3** | ✅ **recommended** |
| 2.10.x | Keras 2 | 0.8.1 | <3.20 | ✅ supported |
| 2.8.x | Keras 2 | 0.7.x | 3.19.x | ✅ supported |
| <2.8 | Keras 2 | 0.6.x | 3.9-3.19 | ⚠️ risky |

### PyTorch ↔ hls4ml

| PyTorch | hls4ml | ONNX Bridge | Status |
|---------|--------|-------------|--------|
| **2.11.0+cu128** | **1.3.0** | ONNX 1.14-1.21 | ⚠️ **PyTorch direct path buggy** |
| 2.7-2.10 | 1.0.0+ | ONNX 1.14+ | ⚠️ limited (MLP/CNN only) |
| 2.5-2.6 | 0.8.1 | ONNX 1.14 | ⚠️ limited |
| <2.5 | 0.7.x | ONNX 1.13 | 🔴 broken |

### Python ↔ TF/PyTorch

| Python | TF Max Stable | PyTorch Max Stable | Status |
|--------|-------------|-------------------|--------|
| 3.13 | nightly only | 2.11+ | 🔴 avoid for TF |
| 3.12 | nightly only | 2.9+ | 🟡 risky (TF needs nightly) |
| 3.11 | 2.15.x | 2.11+ | ✅ supported |
| **3.10** | **2.15.x** | **2.11+** | ✅ **recommended** |
| 3.9 | 2.15.x | 2.9+ | ✅ supported |
| 3.8 | 2.13.x | 2.5+ | ⚠️ EOL |

### Vivado/Vitis ↔ hls4ml

| Vivado/Vitis | hls4ml Backend | HLS Mode | Status |
|-------------|----------------|----------|--------|
| **2025.2** | Vivado / Vitis | vitis-run | ✅ **current** |
| 2024.x | Vivado / Vitis | vitis-run | ✅ supported |
| 2023.x | Vivado / Vitis | vitis_hls | ✅ supported |
| 2022.x | Vivado / Vitis | vitis_hls | ✅ supported |
| 2021.x | Vivado only | vivado_hls | ⚠️ legacy |
| <2021 | Vivado only | vivado_hls | 🔴 unsupported |

## Execution

```bash
bash scripts/check_compat.sh
```

That is the only helper in this skill — there is no `scripts/compat_matrix.py` in this tree,
and `check_compat.sh` is what `bootstrap_run.py` writes into each run's RUN.md. The matrix
below is a reference table to read, not a program to run.

## Output

```
=== Compatibility Report ===
Target: PyTorch 2.11 + hls4ml 1.3.0 + Vivado 2025.2

| Check                    | Result        | Recommendation              |
|--------------------------|---------------|-----------------------------|
| Python 3.10 ↔ TF 2.13    | supported     | Keep; Keras v2 built-in     |
| Python 3.10 ↔ PT 2.11    | supported     | GPU confirmed RTX 5060 Ti   |
| TF 2.13 ↔ hls4ml 1.3.0   | supported     | Keras v2 path verified      |
| PT 2.11 ↔ hls4ml 1.3.0   | experimental  | Use Keras bridge for now    |
| Protobuf 3.20.3           | supported     | Golden pin for TF 2.13      |
| NumPy 1.24.3              | supported     | <2.0 required for TF        |
| Keras v2/v3 conflict      | CLEAN         | Single Keras v2 in env      |
| VT 2025.2 ↔ hls4ml 1.3.0  | supported     | Vitis backend available     |

Risk Level: LOW — 1 experimental flagged, 0 broken
```

## Critical Rules

1. **Keras v2 and Keras v3 CANNOT coexist** — separate environments required
2. **Protobuf 3.20.3** is the golden pin for TF 2.13–2.15 era
3. **NumPy < 2.0** required for TF < 2.16 (use `numpy==1.24.3` or `numpy<2.0`)
4. **PyTorch Blackwell (SM120)** needs PyTorch 2.7+ with CUDA 12.6+
5. **hls4ml PyTorch backend** is limited; prefer Keras or ONNX bridge

## Gotchas
- `pip list` shows TF version but doesn't reveal Keras v2/v3 split
- `torch.cuda.is_available()` may return True with warnings if SM version mismatch
- Conda-forge hls4ml is OUTDATED; always use `pip install hls4ml`
- Vitis 2025.2 uses `vitis-run --mode hls`, not the old `vivado_hls` command

## Rollback
- Checker is read-only; no changes made
- Report saved to `~/compat_report_YYYYMMDD.md`
