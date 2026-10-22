#!/bin/bash
# FPGA Environment Audit - System Baseline
# Usage: bash audit_system.sh [--json]

set -euo pipefail
OUTPUT_FORMAT="${1:-text}"

audit_os() {
    echo "=== OS & Hardware ==="
    case "$(uname -s)" in
        MINGW*|MSYS*)
            echo "OS: Windows (via Git Bash)"
            systeminfo 2>/dev/null | grep -E "OS Name|OS Version|System Type" | head -3 || \
                powershell -Command "(Get-CimInstance Win32_OperatingSystem).Caption"
            ;;
        Linux*)
            echo "OS: Linux"
            cat /etc/os-release 2>/dev/null | grep PRETTY_NAME || uname -a
            ;;
        Darwin*) echo "OS: macOS $(sw_vers -productVersion 2>/dev/null)" ;;
        *) echo "OS: Unknown - $(uname -s)" ;;
    esac
}

audit_memory() {
    echo "=== Memory ==="
    if free -m 2>/dev/null; then
        free -m | grep Mem
    else
        wmic os get TotalVisibleMemorySize,FreePhysicalMemory 2>/dev/null || \
            echo "Memory check failed"
    fi
}

audit_disk() {
    echo "=== Disk Space ==="
    df -h 2>/dev/null | grep -E "^/|Filesystem|C:|D:" || \
        wmic logicaldisk get deviceid,freespace,size 2>/dev/null
}

audit_gpu() {
    echo "=== GPU ==="
    if nvidia-smi -L 2>/dev/null; then
        nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv
        echo "GPU: OK"
    else
        echo "GPU: WARN - nvidia-smi not available"
    fi
}

audit_path() {
    echo "=== PATH ==="
    local broken=0 dup=0
    IFS=':' read -ra paths <<< "$PATH"
    declare -A seen
    for p in "${paths[@]}"; do
        [[ -z "$p" ]] && { broken=$((broken+1)); echo "WARN: Empty PATH entry"; continue; }
        [[ -n "${seen[$p]:-}" ]] && { dup=$((dup+1)); echo "WARN: Duplicate: $p"; continue; }
        seen[$p]=1
        # Check for partial paths (common Xilinx/Matlab issue)
        if [[ "$p" =~ ^[a-zA-Z]:\\ ]] && [[ ! -d "$p" ]]; then
            broken=$((broken+1))
            echo "FAIL: Broken path: $p"
        fi
    done
    echo "PATH entries: ${#paths[@]}, broken: $broken, duplicates: $dup"
}

audit_xilinx_vars() {
    echo "=== AMD/Xilinx Env Vars ==="
    for var in XILINX_VIVADO XILINX_VITIS XILINX_HLS; do
        if [[ -n "${!var:-}" ]]; then
            echo "OK: $var = ${!var}"
            [[ -d "${!var}" ]] && echo "  -> Directory exists" || echo "  FAIL: Directory NOT found"
        else
            echo "WARN: $var not set"
        fi
    done
    # Check for legacy vars
    for var in $(env | grep -i xilinx | cut -d= -f1); do
        [[ "$var" =~ ^XILINX_ ]] || echo "WARN: Legacy var: $var"
    done
}

main() {
    echo "========================================"
    echo "FPGA+AI Environment Audit - System Baseline"
    echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Host: $(hostname 2>/dev/null || echo unknown)"
    echo "========================================"
    echo ""
    audit_os
    echo ""
    audit_memory
    echo ""
    audit_disk
    echo ""
    audit_gpu
    echo ""
    audit_path
    echo ""
    audit_xilinx_vars
}

main "$@"
