---
name: hls4ml-convert
description: >
  Convert trained models (Keras, ONNX, PyTorch) to hls4ml HLS projects.
  Auto-generates hls4ml config with precision, reuse factor, strategy settings.
  Supports Vivado and Vitis backends. Use when converting ML models to FPGA
  HLS code, optimizing precision/throughput, or generating HLS projects from
  trained neural networks.
allowed-tools: Bash, Read, Write, Edit, Grep
---

# hls4ml Converter

## Purpose
Reliable model-to-HLS conversion with automatic configuration, backend
selection, and HLS code generation. Primary path: Keras/TF model → hls4ml.
Secondary: PyTorch → ONNX → hls4ml or PyTorch → hls4ml (experimental).

Supports precision-aware conversion: takes `precision` and `layer_precisions` from the
deployment contract (and, for a quantized model, the model's own per-layer quantizers).
Writes `convert-evidence.json` with configuration/provenance for the
downstream stages, whose conclusions belong to `vitis-build-agent` (CSim/CSynth) and
`vivado-manager` (board sim / resource gate / timing).

## When to Use
- After training a model and before HLS synthesis
- After a precision has been chosen for the deployment contract
- User says "convert to HLS", "hls4ml", "FPGA inference model"
- Migrating between precision strategies or reuse factors
- Batch converting multiple model variants

## When NOT to Use
- Training models (use training environment)
- HLS synthesis/build (use `vitis-build-agent` after conversion)
- Debugging synthesis failures (use `timing-closure-debugger`)
- Resource validation (use `vivado-manager`'s routed resource gate; for predicting the
  multiplier cost of a layer and choosing a reuse factor, `reuse-factor-resource-tuning`)

## Prerequisites
- Activated conversion environment: `/d/python_envs/hls4ml_conv`
- Trained Keras model (.h5/.keras) or ONNX model (.onnx)
- Vivado 2025.2 or Vitis 2025.2 on PATH
- Default target board: **ALINX AX7A200B (XC7A200T-2FBG484I)** — see `board-profiles/AX7A200B/`

## Input Formats

| Format | Extension | Maturity | Recommended? |
|--------|-----------|----------|-------------|
| Keras v2 model | .h5 / .keras | ✅ Mature | ✅ YES |
| ONNX model | .onnx | ⚠️ Developing | 🟡 Fallback |
| PyTorch model | .pt / .pth | ⚠️ Limited | 🟡 Experimental |

## Execution

### Path A: Keras → hls4ml (Recommended)
```bash
source /d/python_envs/hls4ml_conv/Scripts/activate
python scripts/convert_keras.py --deployment deployment.yaml --output ./hls_project
```

### Path B: ONNX → hls4ml
```bash
source /d/python_envs/hls4ml_conv/Scripts/activate
python scripts/convert_onnx.py --deployment deployment.yaml --output ./hls_project
```

### Path C: PyTorch → hls4ml (NOT IMPLEMENTED)
```bash
# `scripts/convert_pytorch.py` does not exist in this tree. There is no PyTorch converter
# here; the documented route for a PyTorch model is to export it and convert the export.
```
`scripts/` currently holds only `convert_keras.py` and `convert_onnx.py`. `run_line.py`
refuses a `pytorch`/`torch` frontend with an explicit message rather than dispatching to a
missing script, so this gap fails loudly instead of half-way through a run.

## Configuration Template

转换器同时合并契约 `hls_config` 的 `Model/LayerName/LayerType`；null 视为未设置，冲突的全局/逐层 RF、全局精度、Strategy 或 softmax skip 必须失败。`configuration_audit` 记录请求、传给 hls4ml 的配置、生成参数与类型及其哈希。对 hls4ml 1.3.0，`TableType` 保留并告警，真正决定 table typedef 的键是 `Table_t`；不得静默替换冻结字段。

QKeras 保留内部量化，只固定解析后的最终 HLS 图输出到契约网格。使用 exponential product 的新工程自动执行已验证的 hls4ml 1.3.0 po2 乘积位宽修复；补丁只写工程副本，版本/模板哈希未知即拒绝。证据包含 `output_grid_pin` 与 `compatibility_repairs`。转换完成仍须完整 CSim 数值门。

失败诊断与恢复步骤见 [转换与上板恢复规则](../docs/deployment-recovery.md)。

```yaml
# hls4ml_config.yaml
Model:
  Precision:
    default: ap_fixed<16,6>
    # Per-layer overrides:
    # fc1: ap_fixed<8,4>
  ReuseFactor: 1
  Strategy: Latency       # Latency | Resource
  BramFactor: 1000000000
  ChannelsLastConversion: full
  TransposeOutputs: false
  TraceOutput: false

Backend: Vivado            # Vivado | Vitis | Quartus
Board: null               # or e.g. "pynq-z2"
Part: null                # or e.g. "xczu3eg-sbva484-1-e"
ClockPeriod: 5             # ns
ClockUncertainty: 12.5     # percent
IOType: io_parallel        # io_parallel | io_stream
```

## Precision Guide

| Model Type | Recommended Precision | Rationale |
|-----------|----------------------|-----------|
| Simple MLP | `ap_fixed<16,6>` | Balance accuracy/area |
| CNN | `ap_fixed<16,6>` to `ap_fixed<8,4>` | Can tolerate lower precision |
| QKeras quantized | Match QKeras config | Preserve quantization |
| Brevitas quantized | Match Brevitas config | Preserve quantization |

## Reuse Factor Guide

| ReuseFactor | Throughput | Resource | Latency |
|-------------|-----------|----------|---------|
| 1 | Maximum | Maximum | Minimum |
| 2 | 1/2 | ~1/2 | ~2x |
| 4 | 1/4 | ~1/4 | ~4x |
| N | 1/N | ~1/N | ~Nx |

## Output

```
hls_project/
├── hls4ml_config.yml     # Generated config
├── build_prj.tcl          # Vivado/Vitis build script
├── build_lib.sh           # Linux build script
├── project.tcl            # Project TCL
├── keras_model.keras      # Saved model snapshot
└── firmware/              # HLS C++ source
    ├── myproject.cpp
    ├── myproject.h
    ├── parameters.h
    └── weights.h
```

## Failure Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `Unsupported operation type: Gemm` | ONNX op not mapped | Use Keras path or add custom op handler |
| `ModelGraph.__init__() takes N args` | hls4ml version API mismatch | Pin to hls4ml==1.3.0 |
| `VivadoInput not subscriptable` | PyTorch backend write() bug | Use Keras bridge path |
| No backend found | Vivado/Vitis not on PATH | Source settings64.sh first |
| `protobuf version conflict` | TF/hls4ml/ONNX protobuf range clash | Pin protobuf==3.20.3 |

## Gotchas
- hls4ml 1.3.0 PyTorch path: model interpretation works, but write() fails
- ONNX `Gemm` op (from nn.Linear) is NOT supported; use Keras `Dense` instead
- Always run `model.build()` before converting Keras models
- `pyparsing` and `sympy` are runtime deps not declared by hls4ml — install manually

## Rollback
- Conversion is read-only on input model
- Output written to separate directory
- Delete output directory to rollback
