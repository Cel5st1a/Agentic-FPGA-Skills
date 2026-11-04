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

S15-opt 规则优先读 [ORDER.md](../ORDER.md)。本版 auto 保留完整原网络，拒绝 logits-only 裁剪。
配置审计和实际 RF 替换如实记录为诊断，不删除生成工程。转换证据含逐层参数网格观察；
先定位小权重、范围或累加问题，再局部调整，不固定统一位宽。可选的
`softmax_exp_read_factor` 只修改已识别模板的表读取调度，不修改指数或归一化。

### 局部参数精度试验（显式工具，非默认转换）

`scripts/local_precision_search.py` 复用原模型加载与配置，只在新输出目录内试验。
支持普通 Keras 文件/JSON权重模型，以及保留原量化器的QKeras JSON权重模型；
接口仍限`io_parallel`、单输入/输出。普通模型单层角色包括
`weight`、`bias`、`result`、`accum`、`table`、`exp_table`、`inv_table`、`inv_inp`。
后端实际节点未声明的角色会拒绝，不把注入但未使用的类型属性当已生效。
先用现有类型编译生成的 C++，与完整原 CPU 输出比较；基线已通过便停止。
否则按调用方给定顺序逐个只改目标参数角色，选第一个在已提供输入上满足
`.001+.001*abs(ref)` 的候选，不声称最小资源或全输入域最优。示例工具参数：
`--deployment <yaml> --output <new-dir> --layer <actual-layer> --role weight
--candidates "ap_fixed<24,6>" --compiler <host-g++>`。

全部尝试保留原配置、有效配置、实际参数类型、源码哈希、编译命令和原始日志、
输出与比较；编译失败的数值结果为未知，不用Python定点近似替代。原模型、输入和
契约哈希变化时结论失效。主机 C++ 通过不等于 RTL 或板端通过，不修改正式实验条件。
`--trace-layers` 对比可对应的原Keras边界与实际C++层输出，标记最早观察偏差；
融合激活等无法对应处保持未知，不推断唯一根因或内部累加轨迹。多输入/输出、
自动最优类型求解不在支持范围，不声称整网自动最优。
QKeras读取原QActivation和kernel/bias量化器，只试内部accum/表角色；
weight、bias、result角色会拒绝，防止改变训练量化语义。关联权重也纳入原文件哈希检查。
同一生成C++沿用版本绑定的po2兼容修复和输出接口网格；未知模板不自动改写。
`--trace-layers`包含可对应的QActivation边界；数值失败如实保留，定位不是自动修复。
合成量化Dense/Conv2D回归验证了原量化层零差和Softmax失败定位，尚无opt3板测。
具体调用及限制见 [opt3工具说明](../docs/opt3-tools.md)。

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

QKeras 保留内部量化，只固定解析后的最终 HLS 图输出到契约网格。使用 exponential product 的新工程自动执行已验证的 hls4ml 1.3.0 po2 乘积位宽修复；补丁只写工程副本，版本/模板哈希未知即拒绝。证据包含 `output_grid_pin` 与 `compatibility_repairs`。可选C++诊断须比较完整原输出；不把CSim作为额外板端验收门槛。

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

## opt2-wide host diagnostic efficiency

Read [model-family efficiency](../docs/efficiency-by-model-family.md) before selecting a
frontend/tool. The local ordinary-Keras helper now obtains all eligible stateless layer
references with one multi-output observer, preserving mapped boundaries and numeric values.
Stateful graphs retain legacy per-layer behavior; this does not authorize new graph support.
The helper still refuses quantized QKeras, ONNX and unsupported I/O modes: use the original
frontend and its CPU reference path, never remove quantizers/branches to fit a helper.
For missing private/group references, inspect or regenerate CPU references from this group's
original model and exact inputs; missing reference files alone do not require rerunning EDA.
Do not treat this convenience as replacing real board numerical acceptance.

## Opt4 streaming pooling compatibility
Keras conversion with hls4ml 1.3.0 and the Vitis backend installs a hash-checked generated-project repair. Supported 2x2 stride-2 unpadded MaxPooling2D with one complete channel vector per stream packet uses explicit indexed row storage. Other geometries retain the stock function. The conversion evidence records the repair. No model or weight files are patched.
If CSim passes while RTL differs, compare tensors across the earliest pooling boundary before broad precision/RF searches. Reuse existing captures. Confirm the first differing layer and preserve failed attempts.
