---
name: reuse-factor-resource-tuning
description: Use when actual generated Dense or Conv reuse factors or full Softmax table-read scheduling need read-only analysis before selecting an isolated candidate.
---

# Reuse factor and full Softmax scheduling advice

Read [ORDER.md](../ORDER.md) first. Current acceptance is complete original-model board
outputs for 256 inputs against original Python `.001+.001*abs(ref)`. Resource percentages,
preselected RF/precision, HLS/cosim and reports are diagnostics, not extra requirements.

Run `scripts/tune_reuse_factor.py --deployment <yaml> --parameters <actual parameters.h>
--out <new report.json>`. It runs no EDA, changes no source, and refuses an existing output.
It reads emitted RF, reports differences from requested RF, and lists legal candidate
arithmetic estimates. `ceil(n_in*n_out/RF)` is an estimate, not a universal physical count;
sharing, zero weights and backend mapping can change resources. RF is not measured latency.
No factor is selected automatically; actual latency and resources remain null when unmeasured.
One multiplier per output is a possible tradeoff, not a lower bound on multiplier count.

For complete Softmax the report includes observed exp/inverse table sizes and choices:
keep scheduling unchanged (`softmax_exp_read_factor: null`), factor 1, or supported finite
parallel factors. The converter checks the actual template before an optional pragma patch.
Keep complete exponent, sum and normalization. Reduced table replication can cost II/latency;
never promise universal speedup or hardcode a historical model's RF/table/precision.
A Softmax-only model is analyzable without any Dense/Conv RF lever.

Change one knob per attributable trial, preserve every attempt, and validate the actual
generated value against original CPU. A host C++ or RTL result is development evidence,
not board acceptance. A shell-only change keeps a valid arithmetic core; regenerate only
affected downstream artifacts. Once real numerical board success is established, do not
rerun for optional RF/resource/cosim/report measurements. This opt2 copy does not authorize hardware access or replacement of a frozen experiment.
Experiment authorization and current state come from the user and campaign ledger.

## opt2: diagnose the emitted implementation before spending an implementation run

Conv1D and Conv2D are classified by their actual config structs and associated `_mult`
block, not by layer comments. A Conv2D fallback requires height × width × channels;
missing height is unknown. The generated block dimensions take precedence.

Read `sharing_diagnostic.kernel` before interpreting RF candidates. `DenseLatency`
plus RF64 is not proof of Resource-kernel multiplier sharing. For a known Latency kernel,
`predicted_multipliers_now` is null; candidates are hypothetical arithmetic costs only.
Compare requested strategy with the emitted kernel and schedule. Correct a documented
configuration mismatch before another physical run; do not increase RF blindly. Unknown
kernels remain unknown. Explicitly changing Strategy may alter scheduling and invalidates
that arithmetic implementation and downstream evidence, not the original model/weights.

For LUT/register-heavy designs, use the existing synthesized utilization report with
`../log-analyzer/scripts/analyze_efficiency.py` and inspect existing hierarchical reports
or generated RTL. A small DSP count does not identify the bottleneck, and no global count
proves the cost belongs to Softmax or the UART adapter. Keep every attempt and measured
tradeoff. One justified candidate at a time, with no fixed attempt or wall limit.

## Explicit per-layer requests (all model families)

Read `requested_factor_resolution`: LayerName RF and case-insensitive
`layer_reuse_factors` must be honored before global RF. Ambiguous/conflicting declarations
are unknown diagnostics, not permission to fix a valid override by rebuilding the model.
If LayerType contains RF overrides but the emitted header does not identify that frontend
layer type, requested RF and match stay null. Do not infer QDense vs Dense from a dense
multiply config. Actual emitted RF remains recorded regardless of request ambiguity.
