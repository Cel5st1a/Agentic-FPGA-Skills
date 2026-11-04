import json
import os
import subprocess
import sys
from pathlib import Path

from test_s15_numeric_policy import ROOT, load


def tool():
    return load("log-analyzer/scripts/analyze_efficiency.py", "opt2_efficiency")


def test_actual_mapping_keeps_physical_counts_and_no_resource_gate():
    root = Path(os.environ.get("S15_DEVELOPMENT_EVIDENCE", Path(__file__).parents[2] / "evidence"))
    path = root / "20261026-07/p3-build/host-board/utilization_synth.rpt"
    data = tool().parse_utilization(path.read_text())
    assert data["design_state"] == "Synthesized"
    assert data["resources"]["Slice LUTs"]["used"] == 51322
    assert data["resources"]["Slice Registers"]["used"] == 45902
    assert data["resources"]["Block RAM Tile"]["used"] == 0
    assert data["resources"]["DSPs"]["used"] == 36
    assert data["acceptance_decision"] is None
    assert data["dominant_layer"] is None


def test_missing_and_zero_remain_distinct():
    data = tool().parse_utilization("| Slice LUTs* | 100 | 0 | 0 | 100 | 100.00 |")
    assert data["resources"]["Slice LUTs"]["util_percent"] == 100
    assert data["resources"]["Block RAM Tile"] is None
    assert data["acceptance_decision"] is None  # no invented 80% cutoff


def test_route_progress_not_completion_and_cumulative_times_not_added():
    data = tool().parse_route("""Phase 5.1 Global Iteration 0
Time (s): cpu = 00:05:00 ; elapsed = 00:04:00
Phase 5.2 Global Iteration 1
 Number of Nodes with overlaps = 90000
 Number of Nodes with overlaps = 45000
Time (s): cpu = 00:10:00 ; elapsed = 00:08:00
Phase 5.3 Global Iteration 2
 Number of Nodes with overlaps = 70000
""")
    assert data["last_phase"] == "Phase 5.3 Global Iteration 2"
    assert data["last_overlap_count"] == 70000
    assert data["last_reported_elapsed_s"] == 480  # not 240+480
    assert data["last_reported_cpu_s"] == 600
    assert data["physical_completion_verified"] is False
    assert data["stalled"] is None
    assert data["stop_recommended"] is False


def test_errors_are_preserved_but_not_claimed_board_failure():
    data = tool().parse_route("ERROR: [Route 35-1] unable to route\nINFO: Exiting Vivado")
    assert data["errors"] == ["ERROR: [Route 35-1] unable to route"]
    assert data["board_numerical_verified"] is False


def test_vendor_numeric_times_and_malformed_times_do_not_discard_snapshot():
    data = tool().parse_route("Time (s): cpu = 12.5 ; elapsed = 11.25")
    assert data["last_reported_cpu_s"] == 12.5
    data = tool().parse_route("Phase 5.2 Global Iteration 1\nTime (s): cpu = 12: ; elapsed = 11:00")
    assert data["last_reported_cpu_s"] is None
    assert data["last_reported_elapsed_s"] is None
    assert data["last_phase"] == "Phase 5.2 Global Iteration 1"
    data = tool().parse_route(
        "Time (s): cpu = 00:00:02 ; elapsed = 00:00:01\nTime (s): cpu = 00:00:03 ; elapsed = 00:00:"
    )
    assert data["last_reported_cpu_s"] == 2
    assert data["last_reported_elapsed_s"] == 1


def test_resource_header_controls_column_meaning():
    data = tool().parse_utilization("""| Site Type | Used | Available | Util% |
| Slice LUTs* | 10 | 200 | 5.00 |
""")
    assert data["resources"]["Slice LUTs"] == {"used": 10, "available": 200, "util_percent": 5}


def test_cli_snapshot_hashes_and_refuses_overwrite(tmp_path):
    source = tmp_path / "route.log"
    source.write_text("Phase 5.2 Global Iteration 1\n Number of Nodes with overlaps = 7\n")
    out = tmp_path / "analysis.json"
    cmd = [
        sys.executable,
        str(ROOT / "log-analyzer/scripts/analyze_efficiency.py"),
        "--log",
        str(source),
        "--out",
        str(out),
    ]
    first = subprocess.run(cmd, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    data = json.loads(out.read_text())
    assert data["sources"][0]["sha256"]
    assert data["eda_executed"] is False
    old = out.read_bytes()
    assert subprocess.run(cmd, capture_output=True).returncode != 0
    assert old == out.read_bytes()
