---
name: tensorflow-to-fpga
description: >
  TensorFlow/Keras model to FPGA deployment pipeline. Handles Keras v2/v3
  compatibility detection, model export, quantization with QKeras, and
  hls4ml conversion. Use for TF/Keras-based FPGA inference deployment,
  QKeras quantized models, or migrating between Keras versions.
allowed-tools: Bash, Read, Write, Edit, Grep
---

# TensorFlow to FPGA

## ⚠️ This skill has no executable helpers

`scripts/detect_keras.py` and `scripts/convert_keras_to_hls.py` **do not exist in this
tree**. The path they described is implemented, but elsewhere:

| what this skill described | where it actually lives |
| --- | --- |
| Keras v2 / v3 detection | `scripts/line/run_line.py::detect_keras_major` — reads `metadata.json` from the `.keras` container |
| choosing the right interpreter | `scripts/line/run_line.py::resolve_interpreter` — a Keras 3 archive needs the Keras 3 env |
| Keras model → HLS project | `hls4ml-convert/scripts/convert_keras.py`, driven by the deployment contract |

So this skill is a **conceptual guide, not a command list**. For the executable path use
`hls4ml-convert` plus the line driver.

## Purpose
Reliable Keras/TensorFlow model → FPGA HLS pipeline. This is the most mature
hls4ml path. Handles Keras v2 (tf.keras) and Keras v3 detection.

**Keras 3 note (measured 2026-09-28).** A `.keras` container's Keras major version is not
implied by its name: `metadata.json` records `keras_version`, and a Keras 3 container cannot
be deserialised by Keras 2.13 at all — the failure looks like a broken model rather than a
wrong interpreter. `resolve_interpreter` reads the archive and routes accordingly, so the
frontend's default interpreter is no longer trusted for archives. Verified by
`_line/tests/test_frontend_interpreter.py`.

## When to Use
- User has a trained Keras/TF model (.h5, .keras, SavedModel)
- QKeras quantized models
- User says "TF to HLS", "Keras to FPGA"
- The hls4ml PyTorch path is failing

## When NOT to Use
- PyTorch models (use `pytorch-to-fpga`)
- Training new models (use training environment)
- Post-conversion HLS build (use `vitis-build-agent`)

## Prerequisites
- Conversion env: `/d/python_envs/hls4ml_conv` (TF 2.13.1, hls4ml 1.3.0, QKeras 0.9.0)

## Pipeline

### Step 1: Keras Version Detection
No script to run — the detection is in the line driver and reads the artefact itself:

```bash
# what the driver does, in one call (prints its reasoning):
python C:/Users/Administrator/Agentic_FPGA_Deployment_skills/scripts/line/run_line.py \
    --deployment <contract.yaml> --run <run-dir> --stages convert --dry-run
```

`run_line.py::detect_keras_major` opens the `.keras` zip and reads `metadata.json`. It
detects:
- Keras v2 (tf.keras) vs Keras v3 (standalone keras) — from `keras_version`
- the model format (.h5, .keras, JSON+weights, SavedModel), from the contract's frontend
- QKeras layers, from the frontend (`qkeras_json_weights`) and the model's own quantizers

### Step 2: QKeras Support
For quantized models:
```python
# Quantized model uses QKeras layers:
# QDense, QConv2D, QActivation, QBatchNormalization
# These are natively supported by hls4ml
from qkeras import QDense, QConv2D, QActivation
```

### Step 3: Conversion

There is no `scripts/convert_keras_to_hls.py`. Conversion is driven by the **deployment
contract**, not by flags — precision, reuse factor, strategy, backend, io_type and clock all
come from the contract, so a run is reproducible from it:

```bash
python C:/Users/Administrator/Agentic_FPGA_Deployment_skills/hls4ml-convert/scripts/convert_keras.py \
    --deployment <contract.yaml> \
    --output <run>/p2-convert/project \
    --evidence <run>/evidence/convert-evidence.json
```

Run it through `run_line.py` in practice: an invalid reuse factor must **fail** the
conversion rather than be silently substituted, and the driver enforces that (`NOT_RUN` is
never `PASSED`, and a substitution is a refusal).

## Model Format Support

| Format | Keras v2 | Keras v3 | Notes |
|--------|---------|---------|-------|
| .h5 | ✅ | ⚠️ | Best for Keras v2 |
| .keras | ✅ | ✅ | Standard format |
| SavedModel | ✅ | ✅ | TF serving format |
| QKeras .h5 | ✅ | ❌ | TF+Keras v2 only |

## QKeras Layer Support

| QKeras Layer | hls4ml Support | Notes |
|-------------|----------------|-------|
| QDense | ✅ Full | Quantized linear |
| QConv2D | ✅ Full | Quantized convolution |
| QActivation | ✅ Full | Quantized activation |
| QBatchNormalization | ✅ Full | Fused at conversion |
| QDepthwiseConv2D | ⚠️ Limited | HW dependent |
| QLSTM | 🔴 Unsupported | No RNN in hls4ml |

## Gotchas
- **Keras v2 is preferred** for hls4ml; Keras v3 support is experimental
- QKeras requires `tensorflow_model_optimization` (not keras-model-optimization)
- `pyparsing` must be manually installed (QKeras undeclared dependency)
- TF 2.13.1 with protobuf 3.20.3 is the golden combination
- `model.build()` must be called before conversion for Keras Functional API models

## Rollback
- Original models unchanged
- Output written to separate project directory
