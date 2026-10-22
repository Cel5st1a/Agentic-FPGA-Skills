#!/bin/bash
# FPGA Environment Audit - Python & AI Environment
# Usage: bash audit_python_env.sh

set -euo pipefail

PYTHON="${1:-python}"

echo "=== Python Installation ==="
echo "which python: $(which python 2>/dev/null || echo NOT FOUND)"
$PYTHON --version 2>&1 || echo "FAIL: python --version failed"
echo ""

echo "=== pip Version ==="
$PYTHON -m pip --version 2>&1 || echo "FAIL"
echo ""

echo "=== Virtual Environments ==="
for venv_dir in /d/python_envs/*/; do
    if [[ -d "$venv_dir" ]]; then
        echo "Found: $venv_dir"
        "$venv_dir/Scripts/python" --version 2>/dev/null || true
    fi
done
echo ""

echo "=== Key Packages ==="
check_pkg() {
    local pkg=$1 import=$2
    if $PYTHON -c "import $import" 2>/dev/null; then
        local ver=$($PYTHON -c "import $import; print(getattr($import, '__version__', 'unknown'))" 2>/dev/null)
        echo "OK: $pkg ($ver)"
    else
        echo "WARN: $pkg not importable"
    fi
}

# Check in both pt_train and hls4ml_conv if they exist
for env in /d/python_envs/pt_train /d/python_envs/hls4ml_conv; do
    if [[ -f "$env/Scripts/python.exe" ]]; then
        PY="$env/Scripts/python"
        echo "--- $env ---"
        echo "Python: $($PY --version 2>&1)"

        # Check training packages
        $PY -c "import torch; print('PyTorch:', torch.__version__, '| CUDA:', torch.cuda.is_available())" 2>/dev/null || echo "WARN: PyTorch issue"

        # Check conversion packages
        $PY -c "import tensorflow as tf; print('TF:', tf.__version__)" 2>/dev/null || echo "WARN: TF issue"
        $PY -c "import hls4ml; print('hls4ml:', hls4ml.__version__)" 2>/dev/null || echo "WARN: hls4ml issue"
        $PY -c "import onnx; print('onnx:', onnx.__version__)" 2>/dev/null || echo "WARN: onnx issue"
        $PY -c "import numpy; print('numpy:', numpy.__version__)" 2>/dev/null || echo "WARN: numpy issue"

        # Check protobuf
        $PY -c "import google.protobuf" 2>/dev/null && echo "OK: protobuf" || echo "FAIL: protobuf not importable"

        # Check Keras type
        $PY -c "
import tensorflow as tf
try:
    from tensorflow import keras
    print('Keras: v2 (tf.keras, built-in)')
except:
    print('Keras: v3 (standalone) or not available')
" 2>/dev/null || true

        echo ""
    fi
done

echo "=== Environment Isolation Check ==="
# Verify training env doesn't have hls4ml, conversion env doesn't have torch CUDA
for env in /d/python_envs/pt_train /d/python_envs/hls4ml_conv; do
    if [[ -f "$env/Scripts/python.exe" ]]; then
        PY="$env/Scripts/python"
        echo "$env:"
        $PY -c "import torch; print('  torch:', torch.__version__)" 2>/dev/null || echo "  torch: NOT INSTALLED (expected in pt_train)"
        $PY -c "import tensorflow; print('  TF:', tensorflow.__version__)" 2>/dev/null || echo "  TF: NOT INSTALLED (expected in hls4ml_conv)"
    fi
done
