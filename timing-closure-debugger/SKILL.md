---
name: timing-closure-debugger
description: Use when emitted HLS scheduling, Vivado routing congestion, or setup/hold reports require evidence to choose the smallest affected implementation change.
---

# Timing and routing diagnosis

Read [ORDER.md](../ORDER.md) first. The only numerical acceptance is the complete original
model/weights running on the exact board, 256 complete outputs against original Python
`.001+.001*abs(ref)`. Full Sigmoid/Softmax stays. Timing/resource reports, CSim/RTL/cosim
and a second HLS reference are diagnostics, never extra acceptance gates. Actual vendor
failures and electrical safety still matter; do not hide errors or fabricate evidence.

## Before another expensive implementation

1. Read the actual failing stage and its log, PID plus birth and current CPU/log timestamps.
   Use `../log-analyzer/scripts/analyze_efficiency.py --log <existing vivado.log>
   --utilization <existing utilization_synth.rpt> --out <new analysis.json>` for a bounded,
   read-only snapshot. No error or no recent output does not prove a stall or convergence.
   Long runtime alone is not a reason to cancel, restart or add a time limit. During a
   frozen run an external controller observes only; this skill must not send it repair hints.
2. Separate error types. A tool-launch permission error calls for supported launch-route
   diagnosis, not precision/RF changes. A resource or routing problem calls for actual
   mapping and connectivity evidence, not automatically larger RF or wider types. A negative
   intermediate WNS is not a completed timing result. Do not invent false paths or pins.
3. Read the actual generated strategy/kernel, RF, type and table declarations. Latency
   kernels can carry a large RF without establishing Resource-style sharing. If RF changed
   but the emitted structure/mapping did not, another equivalent arithmetic build has no
   demonstrated basis. A different physical directive is a separate experiment and may
   still be justified; record its rationale, do not claim source identity ensures convergence.
4. Use synthesized utilization and available hierarchy/path reports to choose a cause to
   investigate. LUT/register-heavy, low-DSP mapping is not automatically a multiplier issue.
   Check existing RTL for wide muxes, resettable arrays, parallel ports, table replication
   or fanout, but do not attribute a dominant layer without hierarchy/path evidence. Missing
   reports are unknown; do not launch optional diagnostics merely to complete a checklist.
5. Write one hypothesis, expected observable change, changed files and invalidated stages.
   Compare generated config/schedule/RTL before paying for downstream physical work.
   Declaration or pragma acceptance alone does not prove a register boundary or a resource
   improvement. Local width/table/schedule changes need numerical checking against the
   original CPU; optional software/RTL tests are chosen to resolve a specific uncertainty,
   not a mandatory full CSim + boardsim + host-sim + cosim sequence.

## Continue from the smallest affected stage

| Change | Work to reconsider | Work to preserve when identity and valid evidence still apply |
| --- | --- | --- |
| UART shell only | changed shell and its integration/physical artifacts | original model, conversion, arithmetic core and its numerical evidence |
| arithmetic type, Strategy, RF, Softmax schedule | affected generation/HLS and downstream integration | original input/weight bytes, unaffected preparation and raw attempts |
| physical directive only | affected placement/routing/downstream | valid synthesis checkpoint with matching source, constraints and tool identity |
| report or documentation only | that report | successful deployment; no reprogramming or resampling |

Identity is necessary, not sufficient: do not auto-reuse a failed/unproven artifact, copy
another model's implementation, or silently skip stages. A wrapper fix can require a new
run record without requiring conversion from scratch. Never overwrite previous attempts.
Use the run's owned safe-stop mechanism only when authorized and genuinely needed; no
global process termination. Hardware authorization comes from the current user task.

## Completion

A tool exit, synthesis checkpoint, timing report or bitstream never proves board numerical
success. Once complete original-model board outputs pass, stop deployment; subsequent
reporting is offline and optional metrics cannot trigger another implementation. Record
first observed numerical success separately from later reporting and total elapsed time.
One run does not establish a universal or statistically significant speedup.
