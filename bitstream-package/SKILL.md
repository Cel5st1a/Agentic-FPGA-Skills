---
name: bitstream-package
description: >
  Package bitstream, XSA, reports, constraints, and version info into an
  archive for deployment or release. Generates manifest with build metadata.
  Use after successful implementation when ready to deploy or archive.
allowed-tools: Bash, Read, Write, Grep, Glob
---

# Bitstream Package

## Purpose
Create deployment-ready FPGA bitstream packages with all metadata for
reproducibility and deployment tracking.

## When to Use
- After `write_bitstream` succeeds in Vivado
- Before deploying to production FPGA
- Archiving a release candidate
- User says "package bitstream", "create release", "archive build"

## When NOT to Use
- Before synthesis/implementation passes
- For intermediate checkpoints (use Vivado .dcp files)
- HLS-only projects without bitstream

## Prerequisites
- Vivado 2025.2 project with completed implementation
- Bitstream file (.bit) generated
- Optional: debug probes file (.ltx)

## Execution

```bash
# The packager is Python, not shell. It takes a RUN directory, not loose artefacts: it
# re-verifies every stage's evidence, both numerical gates and the routed port audit before it
# writes anything, and refuses if any of them is not a pass.
python scripts/package_bitstream.py \
    --run <run-dir> \
    --model-id <model-id> \
    --release-root <release-root> \
    [--force]
```

## Package Structure

This is the layout `scripts/package_bitstream.py` writes for new runs. The older
`D:\ML_models\asymmetric_cnn_fc` delivery predates the automatic routed port gate:

```
<release-root>/<model-id>/
├── <model-id>.bit              # the bitstream
├── deployment.yaml             # the frozen contract, byte-identical to the run's copy
├── board_profile.yaml          # board resource profile used
├── board_pins_profile.yaml     # board-facing pins + their provenance
├── board-wrapper.json          # the generated top's interface and ROM geometry
├── io-scope.json               # routed checkpoint port audit; configure-only for ROM tops
├── constraints/board.xdc       # pin constraints used
├── hls4ml_config.yaml          # model-to-HLS config as applied
├── reports/
│   ├── timing_impl.rpt         # routed timing
│   ├── timing_synth.rpt        # post-synthesis timing
│   ├── utilization_impl.rpt    # routed resources
│   ├── utilization_synth.rpt   # post-synthesis resources
│   ├── drc.rpt                 # routed DRC
│   ├── clocks.rpt              # clock summary
│   └── myproject_csynth.xml    # the CSynth report
├── evidence/                   # one JSON per stage, the only source of the verdicts
│   ├── convert-evidence.json   csim-evidence.json   csynth-evidence.json
│   ├── board-sim-evidence.json vivado-synth-evidence.json
│   ├── vivado-impl-evidence.json  vivado-bitstream-evidence.json
├── logs/
│   ├── csim_results.log        # raw CSim output (gate 1's actual side)
│   └── board_results.log       # raw xsim output (gate 2's actual side)
├── MANIFEST.json               # build metadata + gate numbers + per-file checksums
├── CHECKSUMS.sha256            # every file except MANIFEST.json and itself
├── VERIFICATION.md             # generated from the evidence
├── README.md
├── RUN.md
└── starting-state.json
```

**There are no `*_validation_report.md` files.** Earlier versions of this SKILL.md listed
`resource_validation_report.md`, `rtl_verification_report.md` and
`board_validation_report.md` "from" `fpga-resource-validator`, `rtl-cosim-verifier` and
`board-inference-validator` — none of those three skills exists in this tree, and the
packager writes no such files. The verdicts live in `evidence/*.json` and are summarised in
`MANIFEST.json` and `VERIFICATION.md`.

### MANIFEST.json (Extended)

```json
{
  "version": "1.0.0",
  "build_date": "2026-05-13T00:30:00+08:00",
  "target_part": "xc7a200tfbg484-2",
  "board": "AX7A200B",
  "vivado_version": "2025.2",
  "clock_frequency_mhz": 200,
  "model": "mlp_4_8_4",
  "checksums": {
    "top.bit": "sha256:abc123...",
    "top.hwh": "sha256:def456..."
  },
  "resource_usage": {
    "dsp": 48, "bram": 12, "lut": 8234, "ff": 12456
  },
  "timing": {
    "wns_ns": 0.15, "tns_ns": 0.0, "clock_period_ns": 5.0
  },
  "validation": {
    "golden_reference": "sha256:...",
    "hls_functional": "PASS",
    "rtl_cosim": "PASS",
    "resource_check": "PASS",
    "board_inference": "PENDING"
  }
}
```

## MANIFEST.json

```json
{
  "version": "1.0.0",
  "build_date": "2026-05-13T00:30:00+08:00",
  "target_part": "xczu3eg-sbva484-1-e",
  "vivado_version": "2025.2",
  "clock_frequency_mhz": 200,
  "model": "mlp_4_8_4",
  "checksums": {
    "top.bit": "sha256:abc123...",
    "top.hwh": "sha256:def456..."
  },
  "resource_usage": {
    "dsp": 48,
    "bram": 12,
    "lut": 8234,
    "ff": 12456
  },
  "timing": {
    "wns_ns": 0.15,
    "tns_ns": 0.0,
    "clock_period_ns": 5.0
  }
}
```

## Gotchas
- Always include .hwh file for PYNQ or Zynq platforms
- Verify bitstream with `get_property` before packaging
- Checksums prevent deployment of corrupted files
- XSA file needed for Vitis platform projects (not just .bit)
