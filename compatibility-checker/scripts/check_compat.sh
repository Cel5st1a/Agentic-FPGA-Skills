#!/bin/bash
# Compatibility Checker - Quick version matrix validation
# Usage: bash check_compat.sh [--env pt_train|hls4ml_conv]

set -euo pipefail

ENV_DIR="${1:-/d/python_envs/hls4ml_conv}"
PY="$ENV_DIR/Scripts/python"

echo "=== Compatibility Check ==="
echo "Environment: $ENV_DIR"
echo ""

check_python() {
    local ver=$("$PY" --version 2>&1)
    echo "Python: $ver"
    case "$ver" in
        *3.10*) echo "  Status: supported" ;;
        *3.11*) echo "  Status: supported" ;;
        *3.12*) echo "  Status: risky (TF needs nightly)" ;;
        *3.13*) echo "  Status: broken (no stable TF)" ;;
        *) echo "  Status: unknown" ;;
    esac
}

check_tf() {
    if "$PY" -c "import tensorflow as tf; print(tf.__version__)" 2>/dev/null; then
        local tf_ver=$("$PY" -c "import tensorflow as tf; print(tf.__version__)" 2>/dev/null)
        echo "TensorFlow: $tf_ver"
        case "$tf_ver" in
            2.13*) echo "  Status: supported (recommended)" ;;
            2.14*|2.15*) echo "  Status: supported" ;;
            2.1[6-9]*|2.2[0-9]*) echo "  Status: experimental (Keras v3)" ;;
            2.1[0-2]*) echo "  Status: supported (check protobuf)" ;;
            *) echo "  Status: unknown" ;;
        esac

        # Check Keras type
        if "$PY" -c "from tensorflow import keras; print('Keras v2')" 2>/dev/null; then
            echo "Keras type: v2 (tf.keras, built-in)"
        elif "$PY" -c "import keras; print('Keras v3')" 2>/dev/null; then
            echo "Keras type: v3 (standalone multi-backend)"
        else
            echo "Keras type: WARN - could not determine"
        fi
    else
        echo "TensorFlow: NOT INSTALLED"
    fi
}

check_pytorch() {
    if "$PY" -c "import torch; print(torch.__version__)" 2>/dev/null; then
        local pt_ver=$("$PY" -c "import torch; print(torch.__version__)" 2>/dev/null)
        local cuda_ok=$("$PY" -c "import torch; print(torch.cuda.is_available())" 2>/dev/null)
        echo "PyTorch: $pt_ver | CUDA: $cuda_ok"
        case "$pt_ver" in
            2.11*) echo "  Status: supported (Blackwell OK)" ;;
            2.7*|2.8*|2.9*|2.10*) echo "  Status: supported" ;;
            2.5*|2.6*) echo "  Status: limited (no Blackwell SM120)" ;;
            *) echo "  Status: unknown" ;;
        esac
    else
        echo "PyTorch: NOT INSTALLED"
    fi
}

check_hls4ml() {
    if "$PY" -c "import hls4ml; print(hls4ml.__version__)" 2>/dev/null; then
        local ver=$("$PY" -c "import hls4ml; print(hls4ml.__version__)" 2>/dev/null)
        echo "hls4ml: $ver"
        local backends=$("$PY" -c "from hls4ml.backends import get_available_backends; print(' '.join(get_available_backends()))" 2>/dev/null)
        echo "  Backends: $backends"
    else
        echo "hls4ml: NOT INSTALLED or FAILED to import"
    fi
}

check_protobuf() {
    if "$PY" -c "import google.protobuf" 2>/dev/null; then
        local ver=$("$PY" -c "from google.protobuf import __version__; print(__version__)" 2>/dev/null || echo "unknown")
        echo "protobuf: $ver"
        case "$ver" in
            3.20.3) echo "  Status: golden pin" ;;
            3.19*|3.20*|4.*) echo "  Status: check TF compatibility" ;;
            *) echo "  Status: verify manually" ;;
        esac
    else
        echo "protobuf: FAIL - NOT INSTALLED"
    fi
}

check_numpy() {
    if "$PY" -c "import numpy; print(numpy.__version__)" 2>/dev/null; then
        local ver=$("$PY" -c "import numpy; print(numpy.__version__)" 2>/dev/null)
        echo "numpy: $ver"
        case "$ver" in
            1.*) echo "  Status: OK (TF compatible)" ;;
            2.*) echo "  Status: WARN - NumPy 2.0 may break TF < 2.16" ;;
        esac
    fi
}

check_vivado() {
    if vivado -version 2>/dev/null; then
        echo "Vivado: OK ($(vivado -version 2>&1 | head -1))"
    else
        echo "Vivado: NOT FOUND on PATH"
    fi
}

echo "--- Python ---"
check_python
echo ""

echo "--- Frameworks ---"
check_tf
check_pytorch
echo ""

echo "--- hls4ml ---"
check_hls4ml
echo ""

echo "--- Dependencies ---"
check_protobuf
check_numpy
echo ""

echo "--- AMD Tools ---"
check_vivado
echo ""

echo "=== Risk Assessment ==="
# Count risk factors
RISKS=0
"$PY" -c "import keras" 2>/dev/null && { echo "WARNING: standalone keras found (Keras v3)"; RISKS=$((RISKS+1)); }
"$PY" -c "import qkeras" 2>/dev/null && "$PY" -c "import tensorflow as tf; assert tf.__version__ < '2.16'" 2>/dev/null || \
    { echo "INFO: QKeras with Keras v2 (supported)"; }

if (( RISKS == 0 )); then
    echo "Risk Level: LOW"
else
    echo "Risk Level: MEDIUM ($RISKS risk factors)"
fi
