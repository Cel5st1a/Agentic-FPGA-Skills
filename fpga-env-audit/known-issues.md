# FPGA Environment Audit - Known Issues

## #1: TensorFlow __init__.py Missing (FATAL)
**Symptom**: `import tensorflow` succeeds but `tf.__version__` fails
**Cause**: Corrupted installation — `__init__.py` missing from site-packages/tensorflow/
**Fix**: Completely uninstall TF, recreate venv, reinstall
**Prevention**: Always use `pip install` within venv, not `--target` or `--user`

## #2: Keras v2/v3 Conflict
**Symptom**: `ImportError: Keras cannot be imported` or `No module named 'tensorflow.keras'`
**Cause**: Installing `keras` (v3) alongside `tensorflow` (Keras v2)
**Fix**: Use separate environments. In conversion env: TF ≤ 2.15 only. Never `pip install keras` in a TF env.
**Detection**: `pip list | grep keras` shows both `keras` and `tf-keras`

## #3: PyTorch CPU-Only Wheel
**Symptom**: `torch.cuda.is_available()` returns False
**Cause**: Installed CPU-only wheel instead of CUDA wheel
**Fix**: Uninstall and reinstall from `https://download.pytorch.org/whl/cu128`
**Detection**: `pip show torch` shows `+cpu` suffix in version

## #4: Blackwell GPU (SM120) Incompatibility
**Symptom**: Warning about "sm_120 is not compatible with current PyTorch"
**Cause**: PyTorch < 2.7 doesn't support Blackwell compute capability
**Fix**: Upgrade to PyTorch 2.7+ with CUDA 12.6+
**Affected GPUs**: RTX 5060, 5070, 5080, 5090 series

## #5: Duplicate Vivado/Vitis Installations
**Symptom**: Two 2025.2 installations found (e.g., /d/vitis + /d/vivado)
**Cause**: Installing Vivado standalone AND Vitis (which includes Vivado)
**Fix**: Keep the Vitis installation (full suite), remove standalone Vivado
**Savings**: ~52GB

## #6: Broken PATH Entries
**Symptom**: Commands not found despite being installed
**Cause**: Partial paths (e.g., "atlab/runtime/win64" missing "D:\m" prefix)
**Fix**: Use PowerShell: `[Environment]::SetEnvironmentVariable("PATH", ...)`
**Prevention**: Always verify PATH after installs with `echo $PATH | tr ':' '\n'`

## #7: Protobuf Version Conflict
**Symptom**: `pip` refuses to install TF+hls4ml+onnx together
**Cause**: TF 2.10 wants protobuf < 3.20, ONNX wants ≥ 3.20
**Fix**: Use TF 2.13+ which accepts protobuf ≥ 3.20. Pin to 3.20.3.
**Golden pin**: `protobuf==3.20.3`

## #8: hls4ml PyTorch Backend write() Bug
**Symptom**: `'VivadoInput' object is not subscriptable` during hls_model.write()
**Cause**: Bug in hls4ml 1.3.0 PyTorch backend template
**Workaround**: Use Keras model path instead of PyTorch direct path
**Status**: Known issue, likely fixed in 1.4+

## #9: tf_keras Cascade Upgrade
**Symptom**: Installing `tf_keras` upgrades TF from 2.13 to 2.21 and Keras to v3
**Cause**: `tf_keras` has loose dependency on `tensorflow`
**Fix**: Never install `tf_keras` unless you specifically want Keras v3
**Prevention**: Use `tensorflow>=2.13,<2.14` pin
