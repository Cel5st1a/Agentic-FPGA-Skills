---
name: reuse-factor-resource-tuning
description: >
  Use when a build is blocked or sized out on FPGA resources and the
  deployment parameters are the remaining lever -- a resource gate BLOCKED
  with RESOURCE_LIMIT, place_design failing with [DRC UTLZ-1], or a
  CSynth/synthesis report showing LUT, register, DSP or CARRY4 over the
  contract's resource_fraction_max. Covers locating the area-dominating
  layer, hls4ml's per-layer valid ReuseFactor sets, and predicting the
  multiplier count before spending a synthesis run.
allowed-tools: Bash, Read, Grep, Glob, Write
---

# Reuse-Factor Resource Tuning

## Purpose

When a design does not fit, the reuse factor is usually the only lever left that does
not require re-earning the numerical gates — because changing it alters *scheduling*, not
arithmetic. This skill locates the layer that owns the area and picks a legal factor for
it, **before** a synthesis run is spent.

**This skill is advisory.** It predicts; it does not gate. The resource gate in
`vivado-manager`, the two numerical gates, and the RTL itself are the authority. Predictions
from this skill are checked against the RTL afterwards, never quoted as results.

**Status of the guidance here.** The measurements below are real and reproducible. The one
*rule* in this document (§ "Changing the global factor") is not enforced policy: it comes
from a single authoring mistake, and two independent attempts to reproduce that mistake —
one cold-start, one mid-task — did **not** reproduce it. It is stated as a fact about the
tooling, not as a discipline the line enforces. See "Evidence status" at the end.

## When to Use

* A resource gate reported `BLOCKED` with `RESOURCE_LIMIT:` in its reasons
* `place_design` failed with `[DRC UTLZ-1] ... over-utilized`
* Synthesis/CSynth utilisation exceeds `constraints.resource_fraction_max`
* Choosing a factor for a new contract and wanting the cost before running anything

## When NOT to Use

* Timing fails with resources fine — use `timing-closure-debugger`
* The numerical gates fail — reuse factor is not the cause; check precision and the
  reference instead
* `precision` is the thing you want to change — that alters the output LSB term and
  therefore `verification.atol`, so it is a different (and heavier) change

## Procedure

```bash
python scripts/tune_reuse_factor.py \
    --deployment <run>/contract/<pkg>/deployment.yaml \
    --parameters <run>/p2-convert/project/firmware/parameters.h \
    [--utilization <run>/p3-build/board/utilization_synth.rpt] \
    [--out report.json]
```

It reads the contract and the **generated** firmware (no TensorFlow needed), then reports
each layer's effective factor, the factors hls4ml accepts for it, and the multiplier count
now versus after each candidate — followed by the `make_contract_variant.py` command and a
new run id to act on it.

Act through a **new run directory**; never edit a run in place (`run_line.py` refuses to
overwrite without `--reuse`, and one run is one contract).

## The cost model

Multiplier instances the kernel emits:

```
multipliers = DIV_ROUNDUP(n_in * n_out, reuse_factor) - n_zeros / reuse_factor
```

The `n_zeros` term counts weight rows that are all zero after the `(block_factor, rf)`
reshape; it is 0 at this line's precisions. Validated against measured RTL:

| layer | n_in × n_out | rf | predicted | measured |
| --- | --- | --- | --- | --- |
| `mlp_64_32` `enc_dense_0` | 64 × 64 | 64 | 64 | **64** |
| `mlp_64_32` `enc_dense_1` | 64 × 32 | 64 | 32 | **32** |
| `mlp_64_32` `latent` | 32 × 4 | 64 | 2 | **2** |
| `asymmetric_cnn_fc` `encoder_conv_0` | 5 × 8 | 5 | 8 | **8** |
| `asymmetric_cnn_fc` `encoder_conv_1` | 24 × 16 | 24 | 16 | **16** |
| `asymmetric_cnn_fc` `latent` | 256 × 4 | 2 | 512 | **512** |
| `asymmetric_cnn_fc` `latent` | 256 × 4 | 256 | 4 | **4** |
| `asymmetric_cnn_fc` `latent` | 256 × 4 | 1 | 1024 | 426 |

Exact for `rf ≥ 2`; at `rf = 1` it is an **upper bound** (Vitis shares multipliers, 1024
predicted vs 426 realised). The script also cross-checks each prediction against the
`multiplier_limit` expression the generator itself wrote, and records
`prediction_agrees_with_firmware`.

## Cost versus latency

Fewer multipliers means more cycles for the same work — `cycles = rf` per layer at `II=1`.
Whether that shows up end-to-end depends on whether the layer is the bottleneck. Measured on
`asymmetric_cnn_fc`: the two convolutions take ~1,280 cycles against the Dense layer's 1
(at rf=1), so moving the Dense to rf=256 (256 cycles) grows the total by ~20% while cutting
LUT to 0.14× and DSP to 0.14×. A layer that is *not* the critical path is nearly free to
serialise.

## Valid factors are per layer, and the sets barely intersect

hls4ml accepts only factors satisfying (its own `_validate_reuse_factor`, which the script
calls rather than reimplements):

```
multiplier_limit % n_out == 0  or  rf >= n_in
rf % n_in == 0                 or  rf < n_in
(n_in * n_out) % rf == 0
```

Measured sets on one model:

| layer | n_in × n_out | valid factors |
| --- | --- | --- |
| `encoder_conv_0` | 5 × 8 | 1, 5, 10, 20, 40 |
| `encoder_conv_1` | 24 × 16 | 1, 2, 3, 4, 6, 8, 12, 24, 48, 96, 192, 384 |
| `latent` | 256 × 4 | 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024 |

The intersection of the first two is only `{1}`. A factor of 4 looks like a compromise but
is **not valid for `encoder_conv_0`** (it routes to 5), which is why a global value cannot
generally satisfy several layers at once. `convert_keras.py` refuses a silent substitution,
so an invalid value fails the conversion rather than quietly changing the design.

## Changing the global factor

To give specific layers their own legal values while leaving everything else alone, put the
overrides in `layer_reuse_factors` and **leave the global `reuse_factor` at the value the
contract intends**. The global applies to every layer without an override, so lowering it
to satisfy one layer lowers it for all of them — including layers whose cost is dominated
by their *lack* of reuse.

Measured, one model, one field changed:

| `latent` factor | source | Dense multipliers | LUT frac | DSP frac | result |
| --- | --- | --- | --- | --- | --- |
| 64 (frozen contract) | not valid for either conv | — | — | — | conversion refused |
| 1 | inherited from a global lowered for the convs | 426 | **1.380** | **1.276** | does not fit |
| 2 | smallest increase | 512 | 0.372 | **2.889** | does not fit — DSP now over |
| 256 | chosen | **4** | **0.143** | **0.143** | fits |

Note the rf=2 row: the smallest *increase* is not a fix. The count is `ceil(MACs/rf)`, so it
only falls meaningfully once `rf` approaches `MACs / n_out` — the point at which each output
gets its own multiplier. That ratio is the criterion the script uses: it recommends the
smallest legal factor reaching `n_out` multipliers (one per output). Beyond that point extra
serialisation buys nothing and only adds cycles, which is why `rf=1024` (1 multiplier) is not
an improvement on `rf=256` (4 multipliers) for a 4-output layer.

**A layer key that does not match a real layer name is ignored silently** — hls4ml warns only
on invalid *values*. So "no substitution warning" does not prove an override took effect:
assert it in the emitted config (the script checks the generated `parameters.h`).

## Checking the work

After a new run reaches CSynth, attribute the area before changing more parameters — the
per-instance reports name the dominating block directly:

```bash
# per-layer LUT/DSP/FF in the CSynth solution
ls <run>/p2-convert/project/myproject_prj/solution1/syn/report/*_csynth.rpt
# per-module multiplier instances in the generated Verilog
grep -c myproject_mul_ <run>/p2-convert/project/myproject_prj/solution1/syn/verilog/<module>.v
```

Measured example: the Dense block `config10` was 174,898 of 185,767 LUTs (94.1%), matching
426 of 450 multiplier instances (94.7%) — the convolutions were never the problem.

## Numerical effect — schedule order, not "no effect"

Reuse factor changes nothing in the arithmetic *when it preserves the accumulator order*,
and that qualification matters:

* `accum_t = ap_fixed<32,12,AP_TRN,AP_WRAP>` and every term enters as
  `acc[j] += (accum_t)product(x, w)`. Because the cast truncates and truncated addition is
  not associative, a factor can only change a result by **reordering** additions.
* `rf = 1` and `rf = n_in` enter each accumulator's terms in the same order, so they are
  bit-identical. Measured: `asymmetric_cnn_fc` at rf=1, rf=2 and rf=256 produced
  byte-identical `csim_results.log` (`sha256 6c9c6cec…`). Note that this is **one result
  measured once**, not three independent confirmations — the identical hash says so.
* Factors in between (`rf = 128`, `512` on a 256-wide layer) **interleave** terms and can
  shift the last bits (~1e-6 here — inside the gate, but no longer identical).

So: re-run `convert` and `csim` on the new run rather than reasoning that the old gate still
holds. It is cheap, and it is the only thing that makes the claim evidence.

## The enforced bound is the floor, not "4 LSB"

The rule is named `four_effective_output_lsbs`, and for `ap_fixed<32,12>` the LSB term is
`4·2⁻²⁰ = 3.81e-06` — but the operative tolerance is `max(floor, atol) + rtol·|py|` with
`floor = 0.001`, which is **262× larger**. A worst-case error of 149 LSB passes. Do not quote
"4 LSB" as the bound actually enforced, and do not assume a change is safe because it is
"only a few LSB".

## Common mistakes

| Mistake | Reality |
| --- | --- |
| Lowering the global factor so one layer becomes legal | Every layer without an override inherits it. Measured: this put a Dense layer at rf=1 and cost 426 multipliers where 4 were needed |
| Assuming the next factor up will fix the area | On a 256×4 Dense, rf=2 gives 512 multipliers — plausible-looking and still 2.89× the device's DSPs |
| Treating "no substitution warning" as proof the override applied | An unmatched layer *name* is ignored silently; assert against the generated `parameters.h` |
| Reusing the previous run's CSim verdict | The firmware changes, so the C++ changes. New run, re-earned gates |
| Changing `precision` to buy area | Precision moves the output LSB term and therefore `verification.atol`; `contract.py` refuses a contract whose `atol` contradicts the rule. Heavier change, needs its own numerical evidence |
| Relaxing `resource_fraction_max` | It is a frozen contract threshold. Change the design, not the gate |
| Quoting the predicted multiplier count as a result | It is exact for `rf ≥ 2` and an upper bound at `rf = 1`. Count the RTL |

## Evidence status

* The cost model, the valid-factor sets, the measured comparison table and the
  order-preservation argument are all reproduced from real runs; the numbers are in
  `examples/`.
* The "do not lower the global factor" guidance is **not** independently reproducible. It
  came from one authoring mistake on a real build; a cold-start agent and a mid-task agent
  both avoided it (the latter recovered the shipped answer from the workspace). It is
  therefore written here as a documented hazard, and it is deliberately **not** wired into
  `run_line.py` as a check. If it is ever observed to recur, that is the evidence needed to
  promote it.
* `scripts/tune_reuse_factor.py` is read-only, runs no EDA tool, and writes nothing outside
  its `--out` path.
