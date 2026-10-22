# hls4ml Convert - Known Issues

## #1: ONNX Gemm → Dense Mapping Failure
**Status**: Unresolved in hls4ml ≤ 1.3.0
**Symptom**: `Exception: ERROR: Unsupported operation type: Gemm`
**Cause**: PyTorch exports `nn.Linear` as ONNX `Gemm` op, but hls4ml only handles `MatMul` or Keras `Dense`
**Workaround**: Rebuild model architecture in Keras, load weights, use Keras path
**Fix expected**: hls4ml 1.4+ may add Gemm handler

## #2: PyTorch Backend write() Bug
**Status**: Unresolved in hls4ml 1.3.0
**Symptom**: `TypeError: 'VivadoInput' object is not subscriptable` at hls_model.write()
**Cause**: Template engine passes wrong type to input layer handler
**Workaround**: Use Keras path or ONNX bridge
**Fix expected**: Check hls4ml GitHub for fix in 1.4+

## #3: Keras 3 Backend Confusion
**Status**: Design limitation
**Symptom**: TF 2.16+ errors when using `from tensorflow import keras`
**Cause**: TF 2.16+ removed built-in Keras, uses standalone `keras` (Keras 3)
**Prevention**: Pin TF ≤ 2.15 or set `TF_USE_LEGACY_KERAS=1`

## #4: pyparsing Missing
**Status**: Undeclared dependency of QKeras 0.9.0
**Symptom**: `ModuleNotFoundError: No module named 'pyparsing'` when importing QKeras
**Fix**: `pip install pyparsing`
**Prevention**: Add to requirements.txt alongside hls4ml

## #5: sympy Missing for Optimizer
**Status**: Undeclared dependency
**Symptom**: `WARN: Unable to import optimizer(s) from expr_templates.py: No module named 'sympy'`
**Impact**: Minor — optimizer expressions won't be simplified
**Fix**: `pip install sympy`

## #6: Incorrect Model API Call
**Status**: User error pattern
**Symptom**: `TypeError: convert_from_pytorch_model() missing 1 required positional argument`
**Cause**: Forgot to pass `hls_config={'InputShape': (1, N)}`
**Fix**: Always include InputShape in hls_config dict for PyTorch path

## #7: protobuf Binary Incompatibility
**Status**: Environment issue
**Symptom**: `TypeError: Descriptors cannot be created directly`
**Cause**: protobuf version mismatch between TF, ONNX, and hls4ml
**Fix**: Pin protobuf==3.20.3 for TF 2.13 era
