#!/bin/bash
# Log Analyzer - Parse FPGA+AI toolchain errors
# Usage: bash analyze_log.sh <logfile>  OR  cat log.txt | bash analyze_log.sh -

set -euo pipefail

INPUT="${1:--}"
TEMP=$(mktemp)
trap "rm -f $TEMP" EXIT

# Read input
if [[ "$INPUT" == "-" ]]; then
    cat > "$TEMP"
else
    cat "$INPUT" > "$TEMP"
fi

TOTAL_LINES=$(wc -l < "$TEMP")

echo "=== Log Analysis Report ==="
echo "Source: ${INPUT} ($TOTAL_LINES lines)"
echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Count by severity
ERRORS=$(grep -ciE '^\s*(ERROR|Error):|\[ERROR\]|FATAL' "$TEMP" 2>/dev/null || echo 0)
CRITICAL=$(grep -ci 'CRITICAL WARNING' "$TEMP" 2>/dev/null || echo 0)
WARNINGS=$(grep -ciE '^\s*(WARNING|WARN):|\[WARNING\]' "$TEMP" 2>/dev/null || echo 0)

echo "=== Summary ==="
echo "ERROR: $ERRORS"
echo "CRITICAL WARNING: $CRITICAL"
echo "WARNING: $WARNINGS"
echo ""

# Extract key patterns
echo "=== Key Findings ==="

# Vitis HLS errors
if grep -q 'ERROR: \[HLS' "$TEMP" 2>/dev/null; then
    echo "--- Vitis HLS Errors ---"
    grep 'ERROR: \[HLS' "$TEMP" | head -10
    echo ""
fi

# Vivado errors
if grep -qE 'ERROR: \[[A-Z][a-z]+ [0-9]+-' "$TEMP" 2>/dev/null; then
    echo "--- Vivado Errors ---"
    grep -E 'ERROR: \[[A-Z][a-z]+ [0-9]+-' "$TEMP" | head -10
    echo ""
fi

# Timing failure
if grep -qE 'Timing|negative slack|WNS.*-' "$TEMP" 2>/dev/null; then
    echo "--- Timing ---"
    grep -E 'WNS|TNS|slack|Timing' "$TEMP" | grep -iv 'report\|check' | head -5
    echo ""
fi

# License issues
if grep -qiE 'license|licence|FLEXlm|LM_' "$TEMP" 2>/dev/null; then
    echo "--- License ---"
    grep -iE 'license|licence' "$TEMP" | grep -iE 'error|fail|not found|expired' | head -5
    echo ""
fi

# Memory issues
if grep -qiE 'out of memory|OOM|memory' "$TEMP" 2>/dev/null; then
    echo "--- Memory ---"
    grep -iE 'out of memory|OOM' "$TEMP" | head -5
    echo ""
fi

# hls4ml specific
if grep -qiE 'hls4ml|Unsupported operation|pyparsing|tensorflow.keras' "$TEMP" 2>/dev/null; then
    echo "--- hls4ml ---"
    grep -iE 'Unsupported operation|no module named|pyparsing|tensorflow.keras|import error' "$TEMP" | head -5
    echo ""
fi

# Segfault/Crash
if grep -qiE 'segmentation fault|SIGSEGV|SIGABRT|crash|dump' "$TEMP" 2>/dev/null; then
    echo "--- Crash / Segfault ---"
    grep -iE 'segmentation fault|SIGSEGV|crash' "$TEMP" | head -5
    echo ""
fi

echo "=== Recommendations ==="
if (( ERRORS > 0 )); then
    echo "Priority: Fix $ERRORS errors before proceeding."
    grep -E 'ERROR|FATAL' "$TEMP" | head -3 | while read -r line; do
        echo "  -> $line"
    done
fi

if (( CRITICAL > 0 )); then
    echo "Note: $CRITICAL CRITICAL WARNINGs — these are often hard errors in Vivado."
fi

exit 0
