"""D-M01: the ap_vld host path needs an adapter, and the adapter's contract is testable.

`host_stream_adapter_tb.v` proves the AXI-Stream policy. The `io_parallel` line had no
adapter and no testbench, which is exactly why a design that never worked shipped as a
"branch". These tests run the adapter's own testbench under xsim; the testbench contains a
negative control wired the way D-M01 describes, so a passing run means the adapter did
something the broken wiring did not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
ADAPTER = SKILL / "assets" / "host_avld_adapter.v"
TESTBENCH = SKILL / "tests" / "host_avld_adapter_tb.v"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_rtl_tb", SKILL / "scripts" / "run_rtl_tb.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_rtl_tb"] = module
    spec.loader.exec_module(module)
    return module


def test_avld_adapter_latches_input_holds_start_and_pulses_done_once_per_frame(tmp_path):
    result = load_runner().run_testbench(
        top="host_avld_adapter_tb", sources=[ADAPTER, TESTBENCH], workdir=tmp_path)
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "HOST_AVLD_ADAPTER_PASS"


def test_the_testbench_contains_the_broken_wiring_it_claims_to_beat():
    """A regression test with no negative control proves only that the code runs."""
    source = TESTBENCH.read_text(encoding="utf-8")
    assert ".ae_input(c_input_words), .ae_input_ap_vld(1'b1), .ap_start(c_start)" in source, (
        "the D-M01 wiring must be instantiated: raw register, ap_vld tied high, one-cycle "
        "start pulse")
    assert "c_ap_done_rises != 0" in source, (
        "the run must fail if the broken wiring unexpectedly works")


def test_the_adapter_is_parameterised_and_has_no_pinned_geometry():
    """The verified 64x4 sizes may only appear as parameter defaults, never in the body."""
    source = ADAPTER.read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("parameter integer"))
    for literal in ("2048", "127:0", "1279:0"):
        assert literal not in body, f"{literal} is a pinned geometry literal in the body"
    for parameter in ("INPUT_WORDS_BITS", "CORE_INPUT_BITS", "OUTPUT_WORDS_BITS",
                      "CORE_OUTPUT_BITS"):
        assert f"parameter integer {parameter}" in source, f"{parameter} has no default"
        assert body.count(parameter) >= 1, f"{parameter} is declared but never used"
