# FPGA Environment Audit Checklist

## System Baseline
- [ ] OS version and architecture confirmed
- [ ] Memory ≥ 16GB (32GB recommended)
- [ ] Disk free space ≥ 50GB for builds
- [ ] NVIDIA driver installed and GPU detected
- [ ] CUDA driver version matches PyTorch requirements

## AMD Tools
- [ ] Vivado binary exists and `vivado -version` succeeds
- [ ] Vitis binary exists and launch check passes
- [ ] Vitis HLS available (`vitis-run --mode hls`)
- [ ] settings64.sh found at expected paths
- [ ] XILINX_VIVADO and XILINX_VITIS set correctly
- [ ] No legacy XILINX_* env var pollution
- [ ] Single installation (no duplicates across drives)
- [ ] License: FlexLM vars set OR WebPack eligible

## Python Environment
- [ ] Python 3.10.x installed (3.10 recommended)
- [ ] pip available and up-to-date
- [ ] Training env isolated from conversion env
- [ ] No Keras v2/v3 co-installation

## Training Environment (pt_train)
- [ ] PyTorch CUDA version installed (not CPU-only)
- [ ] `torch.cuda.is_available()` returns True
- [ ] GPU compute capability matches PyTorch support
- [ ] torchvision and torchaudio installed
- [ ] ONNX export works
- [ ] Jupyter available for notebooks

## Conversion Environment (hls4ml_conv)
- [ ] TensorFlow import succeeds
- [ ] Keras v2 (built-in, not standalone)
- [ ] hls4ml import succeeds
- [ ] hls4ml backends: Vivado, Vitis available
- [ ] QKeras import succeeds
- [ ] protobuf version compatible (3.20.3)
- [ ] numpy < 2.0
- [ ] pyparsing installed (QKeras dependency)

## Tools
- [ ] VS Code with Python/C++/CMake extensions
- [ ] CMake available (for non-FPGA builds)
- [ ] gcc/g++ or MSVC available (for host code)
- [ ] Git configured

## Connectivity
- [ ] USB-JTAG cable detected (if board connected)
- [ ] Serial port detected (if board connected)
- [ ] Board power and connection verified (if applicable)

## PATH and Shell
- [ ] PATH has no broken entries
- [ ] PATH has no harmful duplicates
- [ ] Shell init files (.bashrc/.bash_profile) exist
- [ ] Environment activation aliases defined

## Audit Completion
- [ ] Report generated: `audit_report_YYYYMMDD.md`
- [ ] All failures triaged with fix plan
- [ ] Warnings reviewed and accepted or fixed
- [ ] UNKNOWN items flagged for follow-up
