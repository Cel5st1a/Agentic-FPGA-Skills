# Publication validation — 2026-11-21

Source: active `20261118-opt4-workflow/skills`, as identified by its ACTIVE-SKILLS record. This source includes the November 18 streaming activation and Dense high-reuse compatibility repairs. All 188 distributed source files are copied without modification; their SHA-256 values are in SOURCE-MANIFEST.json. There are 16 skills, including the new workflow-manager.

Environment: Windows PowerShell; existing project Python 3.10 virtual environment. Original model files, historical workflow records, credentials, caches, logs and generated FPGA artifacts are excluded. Odatix is a separately installed dependency, not vendored source.

## Checks

- Core regression suite: **53 passed, 1 skipped in 4.05 seconds**.
- `run_workflow.py --help` and `odatix_batch.py --help`: passed.
- Ruff default check: **30 existing findings**, comprising E402=6, E731=2, F401=10, F541=2, F811=8 and F841=2. This is not a lint-clean release; source behavior was preserved.
- Credential pattern scan: no matches for the checked GitHub-token, private-key-header and API-key patterns. This limited scan is not a guarantee.

Core command:

```powershell
python -m pytest tests/test_opt4_regressions.py tests/test_odatix_batch.py tests/test_complete_workflow.py tests/test_workflow_audit.py tests/test_hls_scan.py tests/test_gate_feedback.py tests/test_uart_entry.py -q
```

The initial complete `tests/` invocation reached a real Vivado `xelab` chunk-transport test and was stopped. A subsequent run excluding RTL test modules reached model conversion/C++ integration tests and was also stopped to bound publication work. Neither invocation is counted as a full-suite pass. Only the explicitly listed core suite above completed. No new bitstream deployment, board capture, or hardware verification was performed for this publication; the interrupted RTL attempt is not a simulation pass.

README, source manifest, third-party notice and this validation record describe the publication. Original historical experiment claims are not independently reverified here.
