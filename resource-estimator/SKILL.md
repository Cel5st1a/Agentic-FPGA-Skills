---
name: resource-estimator
description: >
  DEPRECATED — reference only. Pre-synthesis resource estimation for FPGA
  neural network accelerators from an hls4ml model graph or architecture
  parameters. The heuristic formulas have ±30% error and must not be used
  for deployment decisions. Use the routed resource gate in `vivado-manager`
  as the authority, and `reuse-factor-resource-tuning` to predict a layer's
  multiplier cost and choose a reuse factor.
status: deprecated
allowed-tools: Bash, Read, Grep
---

# Resource Estimator ⚠️ DEPRECATED

> **This skill is deprecated and has no executable helper.** The
> `scripts/estimate.py` it once referenced does not exist in this tree, and
> the `fpga-resource-validator` skill it named as its successor does not
> exist either. Both references were dangling and have been removed.
>
> **What to use instead:**
> * the **authority** on whether a design fits is the routed resource gate in
>   `vivado-manager` (`vivado-manager/scripts/run_vivado.py`), measured against the
>   contract's `constraints.resource_fraction_max`;
> * to predict what a specific layer costs and pick a reuse factor before spending a
>   synthesis run, use `reuse-factor-resource-tuning` (validated against measured RTL:
>   exact for `rf >= 2`, an upper bound at `rf = 1`);
> * to debug a timing failure with resources fine, use `timing-closure-debugger`.
>
> The formulas below are retained only as a rough early sanity check. Do not quote them as
> results and do not gate anything on them.

## Purpose
Estimate FPGA resource usage BEFORE synthesis to avoid time-wasting runs
on oversized or poorly configured designs. Use heuristic formulas and
hls4ml profiling data.

## When to Use
- Selecting FPGA part for a given model architecture
- Iterating on reuse factor / precision trade-offs
- "Will this fit on device X?" questions
- Before committing to a long synthesis run

## When NOT to Use
- After synthesis (read actual reports instead)
- During active debugging (use `timing-closure-debugger`)
- For non-neural-network designs

## Estimation Formulas

### DSP (Multiply-Accumulate Units)
```
DSP = Σ_layers [ (input_neurons × output_neurons × weight_bits²) / (DSP_slice_bits × reuse_factor) ]
```
Rule of thumb: Each MAC of <16-bit uses ~1 DSP slice.

### BRAM (Block RAM)
```
BRAM = Σ_layers [ (output_neurons × weight_bits) / (36Kb × reuse_factor) ]
         + Σ_layers [ (input_neurons × data_bits) / 36Kb ]  # activation buffers
```

### LUT (Look-Up Tables)
```
LUT ≈ 1.5 × (DSP × 100 + BRAM × 50 + FF × 0.5)
```
Rule of thumb: LUT ≈ 1-2× FF count.

### FF (Flip-Flops)
```
FF ≈ Σ_layers [ (pipeline_depth × data_width × output_neurons) / reuse_factor ]
```

### Latency
```
Latency_cycles = Σ_layers [ pipeline_depth + (input_neurons × output_neurons) / (reuse_factor)]
Latency_ns = Latency_cycles × clock_period_ns / II
```

## Execution

There is no script to run. Apply the formulas below by hand if a rough early number is
genuinely useful, and treat the result as an order-of-magnitude sanity check only:

```bash
# `scripts/estimate.py` does not exist -- this skill ships no executable helper.
# For a real number, use the routed resource gate in vivado-manager.
# To predict a layer's multiplier cost and pick a reuse factor:
python C:/Users/Administrator/Agentic_FPGA_Deployment_skills/reuse-factor-resource-tuning/scripts/tune_reuse_factor.py \
    --deployment <contract.yaml> --parameters <run>/p2-convert/project/firmware/parameters.h
```

## Output

```
=== Resource Estimation ===
Model: 4-8-4 MLP
Precision: ap_fixed<16,6>
Reuse Factor: 1
Clock: 5 ns (200 MHz)

| Resource | Estimated | Notes                    |
|----------|-----------|--------------------------|
| DSP      | 12        | ~4 per weight matrix     |
| BRAM     | 3         | Weight storage           |
| LUT      | 1842      | ~1.5× (DSP*100 + BRAM*50)|
| FF       | 920       | Pipeline registers       |
| Latency  | 12 cycles | ~60 ns at 200 MHz        |

Part Fit:
- xc7z020: ✅ Fits (DSP 220, BRAM 140)
- xc7z010: ⚠️ Tight (DSP 80, BRAM 60)
- xc7a35t: ✅ Fits
```

## Accuracy

Estimation accuracy vs actual synthesis:
- DSP: ±15%
- BRAM: ±20%
- LUT: ±30%
- FF: ±25%
- Latency: ±10%

Note: Estimation accuracy degrades for large designs (>80% utilization) due
to routing congestion not captured in the formulas.

## Gotchas
- These formulas work for simple MLP/CNN architectures; transformers need custom modeling
- Routing overhead can push LUT usage 2-5× above estimate in dense designs
- BRAM fragmentation can waste up to 30% of capacity
- DSP cascade chains have latency penalties not captured in simple formulas
- Always leave 20% margin for routing overhead when sizing parts

## Rollback
- Estimation is read-only; no files modified
