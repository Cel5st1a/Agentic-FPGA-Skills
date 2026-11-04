"""Timestamp semantics only: all physical operations below are software stubs."""

import json
import sys

import pytest
from test_s15_numeric_policy import ROOT, load


def test_earliest_time_uses_eligible_observations_not_success_text_or_file_time():
    verify = load("board-host-io/scripts/verify_board.py", "observation_selection")
    records = [
        {"status": "HARDWARE_NUMERICAL_PASSED"},  # A string is not a verified observation.
        {
            "hardware_numerical_verified": True,
            "timing": {
                "numeric_success_observed_at": "2026-10-25T13:00:00+08:00",
                "numeric_success_time_kind": "observer_upper_bound",
            },
        },
        {
            "hardware_numerical_verified": True,
            "timing": {
                "numeric_success_observed_at": "2026-10-25T04:59:00Z",
                "numeric_success_time_kind": "observer_upper_bound",
            },
        },
        {
            "hardware_numerical_verified": True,
            "timing": {
                "numeric_success_observed_at": "2026-10-25T00:00:00",  # Unknown zone.
                "numeric_success_time_kind": "observer_upper_bound",
            },
        },
    ]
    result = verify.earliest_success_observation(records)
    assert result["source_index"] == 2
    assert result["earliest_observed_at"] == "2026-10-25T04:59:00+00:00"
    assert result["exact_first_success_at"] is None
    assert verify.earliest_success_observation(records[:1])["earliest_observed_at"] is None


@pytest.mark.parametrize("numeric_pass", [True, False])
def test_capture_records_success_bound_separately_from_finish_with_stub_io(
    tmp_path, monkeypatch, numeric_pass
):
    import serial

    sys.path.insert(0, str(ROOT / "board-host-io/tests"))
    from test_attempt_persistence import capture_fixture, verify

    deployment, board, bit, scope, programmed, output = capture_fixture(monkeypatch, tmp_path)

    class Port:
        in_waiting = 0

        def open(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(
        verify, "exchange", lambda *a, **k: (b"", b"fixture", 7, [0.0 if numeric_pass else 1.0])
    )
    result = verify.capture(
        deployment, board, bit, scope, programmed, "SOFTWARE_STUB", "NOT_A_BOARD", output
    )
    timing = result["timing"]
    assert timing["capture_started_observed_at"] <= timing["capture_finished_observed_at"]
    assert timing["sdk_ended_at"] is None and timing["owner_cleanup_ended_at"] is None
    if numeric_pass:
        assert timing["numeric_success_time_kind"] == "observer_upper_bound"
        assert timing["numeric_success_observed_at"] <= timing["capture_finished_observed_at"]
        assert timing["post_numeric_observation_elapsed_s"] >= 0
    else:
        assert timing["numeric_success_observed_at"] is None
        assert timing["post_numeric_observation_elapsed_s"] is None
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert all("observed_at_utc" in e for e in events)
    compared = [e for e in events if e["kind"] == "numerical_comparison_complete"]
    assert len(compared) == 1 and compared[0]["passed"] is numeric_pass
