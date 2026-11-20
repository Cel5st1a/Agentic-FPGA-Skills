---
name: workflow-manager
description: Use when the user requests complete original, fixed-reference, C simulation and FPGA numerical comparisons, multiple design candidates, Odatix optimization, or an audited deployment report.
---

# Complete FPGA verification and candidate selection

Use this workflow for an explicit four-level comparison and multi-candidate optimization request. Read the supplied deployment contract and the current workflow status. The deployment contract supplies the model, original weights, full input set, arithmetic defaults and board identity.

## Required outcome

The completed result contains G1 original-model outputs, G2 independently evaluated fixed arithmetic, G3 vendor C simulation and G4 independently decoded real FPGA outputs. Each level covers every original input and output. Six pairwise comparisons, raw source identities and first differing indices appear in the report. Missing or unsupported levels leave the task incomplete.

Invoke Odatix Workflow with at least two distinct configurations. Preserve all proposed candidates, failed stages and returned errors. At least two candidates must complete all four levels and the shared physical requirements. Invoke native Odatix non-dominated sorting on measured adapter cycles and DSP. The report records the front, tie-breaking rule and selected candidate.

## Operations

Use `workflow_inspect` to read the current contract, requirements, status and actual candidate failures. Submit multiple candidates using `workflow_submit`. Each candidate contains its ID, precision, reuse factor and optional named layer overrides. Submit arithmetic hypotheses based on the supplied contract and current feedback.

The `changes` keys are complete contract paths. For a reuse override use `hls_config.LayerName.<observed_layer_name>.ReuseFactor` with an integer value. The corresponding table fields end in `ExpTableSize` and `InvTableSize`. The tool validates this syntax before starting a batch.

For an initial batch, preserve the declared arithmetic and include the baseline and one higher reuse choice. Higher reuse usually reduces concurrent multipliers. Consider lower reuse after inspecting resource headroom. Conversion may report that a requested layer reuse factor was adjusted. Use the exact observed layer name and emitted value to correct the declaration, then submit a new batch. A numerical mismatch requires reading its level and first differing sample before choosing a precision change. Resource or timing failures require reading actual estimates or routed reports. Keep the model, weights, sample count and acceptance tolerances fixed.

The executable entry point is `scripts/line/run_workflow.py`. It runs the full Odatix batch, evaluates the independent reference, obtains vendor and board results, invokes native Odatix selection, and writes a report. Use a fresh output directory. Runtime paths and confirmed board connection are supplied separately from the model task.

```powershell
& <control-python> scripts/line/run_workflow.py --deployment <deployment.yaml> --cases <candidates.json> --runtime <runtime.json> --output <new-output>
```

## Failure and completion

If a batch returns incomplete, inspect its actual errors and submit a justified revision within the shared candidate budget. Preserve the failed batch. Long RTL diagnostics are optional and require a bounded diagnostic purpose. The four numerical levels use vendor C simulation and actual board execution.

Call `workflow_finalize` for the successful attempt. It re-reads original numerical sources, programming and raw UART evidence, model and configuration hashes, physical reports, and Odatix ranking inputs. A single candidate or an incomplete fourth level fails completion. A free-text declaration of success cannot complete the workflow.

The final answer identifies the selected configuration and report, states the sample/output coverage at each level, summarizes observed failures and repairs, and gives the actual selection criteria. Scope any portability claim to the models and operators actually validated.


## Claude Code CLI entry

When workflow_inspect/workflow_submit/workflow_finalize are not registered tools,
use the available terminal and file tools. Read the contract and saved workflow.json,
write candidate JSON, invoke scripts/line/run_workflow.py with deployment, cases,
runtime and a fresh output directory, then invoke --finalize on the completed run.
Do not invent calls to tools that are not registered. Success requires the executable
audit, complete numerical evidence and real Odatix selection.

For large parallel convolution graphs, inspect actual HLS feedback and consider
streaming/resource scheduling while preserving numerical checks. Verify activation
repairs apply to the emitted stream header as well as the scalar header. Independent
reference coverage must include repacking/padding operations that conversion emits.
