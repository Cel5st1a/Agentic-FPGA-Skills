---
name: pytorch-to-fpga
description: >
  End-to-end PyTorch model to FPGA deployment pipeline. Handles torch.fx trace,
  compatibility scanning (unsupported ops, dynamic shapes, control flow),
  ONNX export, quantization recommendation, and hls4ml conversion handoff.
  Use when deploying PyTorch models to FPGA, converting training artifacts
  to HLS, or evaluating FPGA inference feasibility.
allowed-tools: Bash, Read, Write, Edit, Grep
---

# PyTorch to FPGA

## ⚠️ This skill has no executable helpers, and there is no PyTorch converter

None of the scripts this skill names exist in the tree —
`validate_model.py`, `trace_model.py`, `scan_compat.py`, `assess_quantization.py`,
`export_onnx.py`, `handoff_to_hls4ml.py` — and neither does
`hls4ml-convert/scripts/convert_pytorch.py`. `hls4ml-convert/scripts/` holds only
`convert_keras.py` and `convert_onnx.py`.

`run_line.py` therefore **refuses** a `pytorch`/`torch` frontend with an explicit message
rather than dispatching to a missing script. The supported route for a PyTorch model is:

1. export it to **ONNX** yourself, and
2. use the `onnx` frontend, which is implemented and verified — it needs the workspace env
   (Keras 3 / TF 2.21 / onnxruntime / qonnx) because qonnx is what can execute a
   channels-last custom-domain graph to build the Python reference.

Treat everything below as background on the pipeline shape, **not** as commands to run. The
steps and their status:

| this skill's step | script it names | status / what to do instead |
| --- | --- | --- |
| 1. Validate model | `scripts/validate_model.py` | does not exist — load it yourself (e.g. `torch.load`) |
| 2. torch.fx trace | `scripts/trace_model.py` | does not exist — `torch.fx.symbolic_trace(model)` |
| 3. Compatibility scan | `scripts/scan_compat.py` | does not exist — no automated scan in this tree |
| 4. Quantization advice | `scripts/assess_quantization.py` | does not exist |
| 5. ONNX export | `scripts/export_onnx.py` | does not exist — `torch.onnx.export(...)` |
| 6. hls4ml handoff | `scripts/handoff_to_hls4ml.py` | does not exist — use the `onnx` frontend via `hls4ml-convert` |

Only token 6 has a supported path, and it starts from an ONNX file you produced yourself.

## Purpose
Bridge PyTorch training artifacts to FPGA inference. The intended pipeline is:
PyTorch model → torch.fx trace → compatibility scan → ONNX export →
hls4ml conversion → HLS code. Only the ONNX→hls4ml step is implemented in this tree.

## When to Use
- User has a trained PyTorch model and wants FPGA deployment
- Evaluating whether a PyTorch architecture is FPGA-compatible
- User says "deploy to FPGA", "PyTorch to HLS", "torch to FPGA"
- Quantization-aware training workflow with Brevitas or QAT

## When NOT to Use
- Training models (use training environment `/d/python_envs/pt_train`)
- Keras/TF models (use `tensorflow-to-fpga` instead)
- Already have ONNX model (skip to `hls4ml-convert`)
- Post-conversion HLS build (use `vitis-build-agent`)

## Prerequisites
- Training env: `/d/python_envs/pt_train` (PyTorch 2.11+cu128, GPU)
- Conversion env: `/d/python_envs/hls4ml_conv` (hls4ml 1.3.0, TF 2.13)
- GPU: RTX 5060 Ti or equivalent with CUDA 12.8+

## Pipeline Steps

### Step 1: Model Validation
```bash
source /d/python_envs/pt_train/Scripts/activate
python scripts/validate_model.py --model model.pt --input-shape "1,3,224,224"
```

### Step 2: torch.fx Trace
```python
# scripts/trace_model.py
import torch
model = torch.load('model.pt')
model.eval()
traced = torch.fx.symbolic_trace(model)
# Check: no dynamic control flow, no data-dependent shapes
```

### Step 3: Compatibility Scan
```bash
python scripts/scan_compat.py --traced traced_model.pt
```

Checks for:
- Unsupported operations (e.g., `Gemm`, `LSTM`, custom ops)
- Dynamic shapes (`torch.where`, dynamic indexing)
- Control flow (`if/while` in forward)
- Unsupported activations (custom, non-standard)
- Quantization compatibility (QKeras, Brevitas, QAT)

### Step 4: Quantization Assessment
```bash
python scripts/assess_quantization.py --model model.pt
```

Options:
| Method | Library | Status |
|--------|---------|--------|
| Post-training quant | PyTorch built-in | ✅ Simple |
| QAT | Brevitas | ✅ hls4ml supported |
| QAT | torch.ao | ⚠️ Limited |
| No quantization | N/A | 🟡 May need higher precision |

### Step 5: ONNX Export
```bash
python scripts/export_onnx.py --model model.pt --output model.onnx --opset 14
```

### Step 6: hls4ml Handoff
```bash
source /d/python_envs/hls4ml_conv/Scripts/activate
python scripts/handoff_to_hls4ml.py --onnx model.onnx --output ./hls_project
```

## Output

```
pytorch_fpga_output/
├── compatibility_report.md    # Op support, risks, recommendations
├── quantization_report.md     # Precision analysis, QAT suggestions
├── model.onnx                 # Exported ONNX model
├── hls4ml_config.yaml         # Auto-generated config
└── hls_project/               # Generated HLS code
```

## Compatibility Matrix (PyTorch → FPGA)

| Operation | hls4ml Support | Notes |
|-----------|---------------|-------|
| `nn.Linear` (Dense) | ✅ Full | Via Keras Dense or ONNX MatMul |
| `nn.Conv2d` | ✅ Full | Standard conv, padding, stride |
| `nn.ReLU` | ✅ Full | |
| `nn.Sigmoid` | ✅ Full | |
| `nn.Tanh` | ✅ Full | |
| `nn.BatchNorm2d` | ✅ Full | Fused at export |
| `nn.MaxPool2d` | ✅ Full | |
| `nn.AvgPool2d` | ✅ Full | |
| `nn.Flatten` | ✅ Full | |
| `nn.Dropout` | ✅ (no-op) | Removed at inference |
| `nn.AdaptiveAvgPool2d` | ⚠️ Limited | Fixed-size only |
| `nn.LSTM` / `nn.GRU` | 🔴 Unsupported | No RNN support in PyTorch path |
| `nn.MultiheadAttention` | 🔴 Unsupported | Custom transformer needed |
| `torch.add` / `torch.cat` | ⚠️ Limited | HW dependent |
| Custom `torch.autograd.Function` | 🔴 Unsupported | |

## Failure Handling

| Failure | Cause | Recovery |
|---------|-------|----------|
| torch.fx trace fails | Dynamic control flow, data-dependent shapes | Simplify model, remove if/while |
| ONNX export fails | Unsupported op, dtype mismatch | Set `dynamo=False`, use opset_version=14 |
| `Unsupported op: Gemm` | hls4ml can't handle ONNX Gemm | Rebuild model in Keras (recommended) |
| Out of memory (GPU) | Model too large | Use CPU, reduce batch, or downsample |
| Precision too high | DSP/LUT overload | Increase reuse factor, reduce precision |

## Gotchas
- **ONNX `Gemm` ≠ Keras `Dense`**: hls4ml doesn't map Gemm → Dense. Use Keras bridge.
- **Blackwell GPU (SM120)**: Needs PyTorch 2.7+ with CUDA 12.6+ for training
- **torch.fx `symbolic_trace`** fails on `@torch.jit.script` and data-dependent control flow
- **Brevitas models** need special export; check `export_in_onnx` or `export_in_qonnx`
- Always set `PYTHONIOENCODING=utf-8` on Windows to avoid emoji encoding crashes

## Rollback
- All outputs written to separate `pytorch_fpga_output/` directory
- Original model untouched
- Delete output directory to rollback
