#!/bin/bash
# ============================================================
# fpga_env.sh — FPGA + AI Development Environment Loader
# One command to source all AMD tools and Python environments.
#
# Usage: source ~/.claude/skills/scripts/fpga_env.sh [train|convert|both]
# ============================================================

ENV_MODE="${1:-both}"

echo "========================================"
echo "FPGA + AI Environment Setup"
echo "Target Board: ALINX AX7A200B (XC7A200T-2FBG484I)"
echo "Vivado/Vitis 2025.2 | hls4ml 1.3.0 | TF 2.13 | PT 2.11"
echo "========================================"

# --- Vivado / Vitis (set via Windows env vars, verify here) ---
echo ""
echo "--- AMD Tools ---"
if [[ -x "/d/vitis/2025.2/Vivado/bin/vivado" ]]; then
    echo "OK: Vivado 2025.2"
else
    echo "WARN: Vivado binary not found. Check D:\\vitis\\2025.2"
fi

if [[ -x "/d/vitis/2025.2/Vitis/bin/vitis" ]]; then
    echo "OK: Vitis 2025.2"
else
    echo "WARN: Vitis binary not found"
fi

# On Windows Git Bash, XILINX env vars come from Windows User env vars.
# Verify they're set:
if [[ -n "${XILINX_VITIS:-}" ]]; then
    echo "OK: XILINX_VITIS = $XILINX_VITIS"
else
    echo "WARN: XILINX_VITIS not set. Vivado/Vitis may not find components."
fi

# --- Python Environment ---
echo ""
echo "--- Python Environment ---"

activate_training() {
    if [[ -f "/d/python_envs/pt_train/Scripts/activate" ]]; then
        source "/d/python_envs/pt_train/Scripts/activate"
        echo "OK: Training env activated (PyTorch 2.11+cu128)"
        python -c "import torch; print('   PyTorch:', torch.__version__, '| CUDA:', torch.cuda.is_available())" 2>/dev/null || true
    else
        echo "FAIL: Training env not found at /d/python_envs/pt_train"
        return 1
    fi
}

activate_conversion() {
    if [[ -f "/d/python_envs/hls4ml_conv/Scripts/activate" ]]; then
        source "/d/python_envs/hls4ml_conv/Scripts/activate"
        echo "OK: Conversion env activated (TF 2.13 + hls4ml 1.3.0)"
        python -c "import tensorflow as tf; import hls4ml; print('   TF:', tf.__version__, '| hls4ml:', hls4ml.__version__)" 2>/dev/null || true
    else
        echo "FAIL: Conversion env not found at /d/python_envs/hls4ml_conv"
        return 1
    fi
}

case "$ENV_MODE" in
    train)
        activate_training
        ;;
    convert)
        activate_conversion
        ;;
    both)
        echo "NOTE: Training and conversion envs use different Python venvs."
        echo "      Activate ONE at a time in your working terminal."
        echo ""
        echo "  For training:   source ~/.claude/skills/scripts/fpga_env.sh train"
        echo "  For conversion: source ~/.claude/skills/scripts/fpga_env.sh convert"
        ;;
    *)
        echo "Usage: source fpga_env.sh [train|convert|both]"
        ;;
esac

# --- Verify tools ---
echo ""
echo "--- Quick Verification ---"
vivado -version 2>/dev/null | head -1 || echo "  vivado: verify manually"
echo "  VS Code: $(code --version 2>/dev/null | head -1 || echo 'not on PATH')"
echo "  gcc: $(gcc --version 2>/dev/null | head -1 || echo 'not found')"
echo ""
echo "========================================"
echo "Environment ready."
echo "  act-train    → switch to training env"
echo "  act-hls4ml   → switch to conversion env"
echo "========================================"
