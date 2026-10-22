# Publication validation — 2026-10-22

Environment: Windows, PowerShell, existing Python virtual environment at
`D:/Agentic_FPGA_Deployment/.venv/Scripts/python.exe`. These checks exercised the
publication copy, not the source tree. No EDA tools or FPGA hardware were operated.

| Check | Result |
| --- | --- |
| Top-level skill count | 15 |
| YAML frontmatter parsing | All 15 parsed successfully with PyYAML |
| SHA-256 comparison with source | All 120 copied files match `SOURCE-MANIFEST.json` |
| Credential pattern scan | No matches for GitHub token, private-key header or quoted long API-secret assignment patterns; this is a limited pattern scan, not a guarantee |
| CLI `--help` | `scripts/line/run_line.py` and `board-host-io/scripts/verify_board.py` succeeded |
| Selected pytest suite | 61 passed in 2.63 seconds |
| Ruff default check | Failed: 45 existing findings; source scripts were preserved |

Pytest command (from repository root):

```powershell
python -m pytest tests/test_bringup_line.py tests/test_rom_package_board.py board-host-io/tests/test_host_geometry.py board-host-io/tests/test_protocol_geometry.py reuse-factor-resource-tuning/scripts/test_tune_reuse_factor.py -q
```

This selected suite checks contract/bootstrap behavior, packaging, host geometry,
protocol encoding and reuse-factor analysis. The complete test tree was not run;
some tests depend on historical local artifacts or AMD xsim.

Ruff command: `ruff check . --output-format json`. Findings by rule:

| Rule | Count |
| --- | ---: |
| F401 | 20 |
| E402 | 9 |
| F811 | 8 |
| F841 | 4 |
| E731 | 2 |
| F541 | 2 |

There were no code changes in this publication copy. README, license, third-party
notices, ignore rules, this validation record and the source hash manifest were
added for distribution. Historical archives and local workflow records were
excluded as described in README. No new hardware-verification claim is made.
