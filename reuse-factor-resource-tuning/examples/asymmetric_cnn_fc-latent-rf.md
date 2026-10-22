# Worked example: a Dense layer that owned 94% of the design

Model: `asymmetric_cnn_fc` — a Keras CNN+FC encoder, 64 inputs to a 4-value latent vector:

```
Reshape(64,1) -> Conv1D(8, k=5)   -> relu -> MaxPool(2)
              -> Conv1D(16, k=3)  -> relu -> MaxPool(2)
              -> Flatten(256)     -> Dense(4)
```

Contract: `ap_fixed<32,12>`, `io_stream`, `Resource`, global `reuse_factor: 1`, target
`xc7a200tfbg484-2` (134,600 LUT / 269,200 FF / 740 DSP / 33,650 CARRY4), limit 0.8.

## The situation

`reuse_factor: 64` was invalid for `encoder_conv_0`, whose valid set is
`{1, 5, 10, 20, 40}`. The convolutions got per-layer overrides `{5, 24}` — but the global
factor had been lowered to `1` so that `1` would be legal everywhere. The final `Dense` layer
had **no override**, so it inherited that `1`.

The kernel then instantiates `ceil(256·4 / 1) = 1024` multipliers, i.e. the whole matrix in one
pipeline stage.

## What the run reported

| measurement | value | source |
| --- | --- | --- |
| Dense multiplier instances | **426** | generated Verilog |
| conv multiplier instances | 8 and 16 | generated Verilog |
| Dense share of the design | **94.1%** of LUTs | per-instance CSynth report |
| design LUT | 178,785 / 134,600 = **1.33×** | `utilization_synth.rpt` |
| design CARRY4 | 36,885 / 33,650 = **1.10×** | same |

`place_design` refused:
`[DRC UTLZ-1] LUT as Logic over-utilized … requires 178769 … only 134600 available`.

Note the gate did **not** catch this at synthesis at the time — it was comparing only DSPs,
registers and BRAM because the LUT row (`Slice LUTs*`) was not being parsed, and CARRY4 was
not gated at all. Both holes are now fixed in `vivado-manager`; see
`_line/runs/asymmetric_cnn_fc/20260928-cnn03/DEFECTS-20260928.md` D-N05/D-N06.

## The change

One field: `layer_reuse_factors.latent: 256`. Nothing else — not precision, not
`resource_fraction_max`, not the criterion, not `io_type`, not the convolutions' factors.

## Measured outcome

| `latent` rf | Dense multipliers | LUT frac | DSP frac | result |
| --- | --- | --- | --- | --- |
| 1 (baseline) | 426 | **1.380** | **1.276** | will not fit |
| 2 | 512 | 0.372 | **2.889** | will not fit — moves the overage from LUT to DSP |
| **256** | **4** | **0.143** | **0.143** | fits |

With `latent: 256`, the routed design reports worst resource LUT 0.178 against the 0.8 limit,
WNS +0.196 ns, WHS +0.012 ns, and both numerical gates pass at 0/1024 violations.

## Why 256 and not 2 or 1024

The count is `ceil(1024 / rf)`, one multiplier per output is the floor (4 for 4 outputs), and
that floor is reached at `rf = 256`. So:

* `rf = 2` → 512 multipliers: a small step that does not reach the floor;
* `rf = 256` → 4 multipliers: the smallest factor that reaches it;
* `rf = 1024` → 1 multiplier: past the floor, 4× the cycles for no area gain.

## What did *not* change, and what had to be re-earned

`max_abs_error` is identical across rf=1 / 2 / 256 — in fact all three runs produced a
**byte-identical** `csim_results.log` (`sha256 6c9c6cec…`). That is *one* result measured
once, not three independent confirmations, and the identical hash is the reason to say so.

So the claim "reuse factor does not change the arithmetic" was demonstrated on the C++ side
(where the factor preserves accumulator order) — and the RTL still had to re-earn its own
gate, because the hardware changed structurally from 426 multipliers in one stage to 4 over
256 cycles. Both gates were re-run on the new run directory and re-passed.

## Reproducing this analysis

```bash
python scripts/tune_reuse_factor.py \
  --deployment <run>/contract/asymmetric_cnn_fc/deployment.yaml \
  --parameters <run>/p2-convert/project/firmware/parameters.h
```

prints, for this run:

```
layer                  kind         rf  valid    MACs  mult now  recommended
latent                 dense         1   True    1024      1024  256
encoder_conv_1         conv         24   True     384        16  24
encoder_conv_0         conv          5   True      40         8  5

area-dominating layer: latent
  factor 1 -> 256: predicted multipliers 1024 -> 4
  layer_reuse_factors: {"encoder_conv_0": 5, "encoder_conv_1": 24, "latent": 256}
```

The same command on the delivered `mlp_64_32` (global rf=64) recommends **no change**, because
its `enc_dense_0` is already at one multiplier per output — a useful negative control: the tool
does not manufacture work when the design is already at the floor.
