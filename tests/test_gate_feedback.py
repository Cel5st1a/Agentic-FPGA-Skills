import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/line"))

from fixed_mlp import Fixed
from verification_gates import board_feedback, code_discrepancy, route_hint


def test_ap_trn_casts_negative_products_towards_minus_infinity_and_wraps():
    q = Fixed(5, 2)
    assert q.code("-0.01") == -1
    assert q.mul(-1, 1) == -1
    assert q.wrap(16) == -16
    assert q.wrap(-17) == 15


def test_reports_first_output_code_without_promoting_board_status():
    d = code_discrepancy([[1, -2], [3, 4]], [[1, -1], [3, 5]])
    assert d["mismatches"] == 2
    assert d["first"] == {
        "sample": 0,
        "output": 1,
        "expected_signed_code": -2,
        "actual_signed_code": -1,
    }
    assert board_feedback()["status"] == "NOT_RUN"
    yes = {
        "shape": [1, 4],
        "violations": 0,
        "max_abs_error": 0.0,
        "first_observed_pass_at_utc": "stamp",
        "bitstream_sha256": "a",
        "raw_event_sha256": "b",
    }
    assert board_feedback(yes, expected_shape=[1, 4], expected_bitstream="a")["status"] == "PASS"
    assert (
        board_feedback(yes, expected_shape=[1, 4], expected_bitstream="wrong")["status"] == "ERROR"
    )


def test_full_board_failure_after_passing_csim_requests_targeted_rtl():
    gates = {"G3": {"status": "PASS"}, "G4": {"status": "FAIL", "values": 1000, "violations": 136}}
    assert route_hint(gates)["first_check"] == "targeted_rtl_replay"


def test_feedback_hash_failure_returns_unknown_gate_states(tmp_path):
    from odatix_batch import file_digest, write_json
    from verification_gates import initial_gates, read_returned_gates

    path = tmp_path / "payload/gates.json"
    path.parent.mkdir()
    write_json(path, {"schema_version": 1, "gates": initial_gates()})
    job = {"job_directory": str(tmp_path), "output_sha256": {"gates.json": file_digest(path)}}
    assert "feedback_error" not in read_returned_gates(job)
    write_json(path, {"schema_version": 1, "gates": {**initial_gates(), "G4": {"status": "PASS"}}})
    returned = read_returned_gates(job)
    assert returned["gates"]["G4"]["status"] == "NOT_RUN"
    assert "hash" in returned["feedback_error"]


def test_board_attempt_without_independent_record_is_an_error():
    from verification_gates import initial_gates, record_board_feedback

    report = {"gates": initial_gates()}
    record_board_feedback(report, attempted=True, error="incomplete UART")
    assert report["gates"]["G4"] == {
        "status": "ERROR",
        "reason": "incomplete UART",
        "source": "live",
    }


@pytest.mark.parametrize(
    ("values", "tensor_shape", "expected_g1", "expected_g3", "violating_output"),
    [
        ([[0.0, 0.0]], [1, 2], "PASS", "FAIL", 1),
        ([[[0.0], [0.0]]], [1, 2, 1], "PASS", "FAIL", 1),
        ([[[0.0, 0.0]]], [1, 2, 1], "ERROR", "ERROR", None),
    ],
)
def test_legacy_csim_pass_is_rechecked_with_original_tolerance(
    tmp_path, values, tensor_shape, expected_g1, expected_g3, violating_output,
):
    from odatix_batch import file_digest, write_json
    from verification_gates import scan_feedback

    contract = tmp_path / "deployment.yaml"
    contract.write_text(
        "model: {path: model.keras}\nverification: {samples: 1, inputs: inputs.json}\n"
    )
    model = tmp_path / "model.keras"
    model.write_bytes(b"model")
    inputs = tmp_path / "inputs.json"
    write_json(inputs, [[1]])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_json(
        tmp_path / "starting-state.json",
        {"model_files": {"model.keras": {"sha256": file_digest(model)}}},
    )
    ref = evidence / "python-output.json"
    write_json(ref, values)
    write_json(
        evidence / "convert-evidence.json",
        {
            "contract_sha256": file_digest(contract),
            "reference": {
                "shape": tensor_shape,
                "inputs": {"sha256": file_digest(inputs)},
                "reference": {"sha256": file_digest(ref)},
            },
        },
    )
    work = tmp_path / "p3-build/csim"
    work.mkdir(parents=True)
    output = work / "csim_results.log"
    output.write_text("0.0 0.005\n")
    write_json(
        evidence / "csim-evidence.json",
        {
            "status": "PASSED",
            "contract_sha256": file_digest(contract),
            "numerical_gate": {"passed": True, "reference_file_sha256": file_digest(ref)},
            "artifacts": {"csim_results": {"file": output.name, "sha256": file_digest(output)}},
        },
    )
    feedback = scan_feedback(tmp_path, contract, profile_reason="outside covered profile")
    gates = feedback["gates"]
    assert gates["G1"]["status"] == expected_g1
    assert gates["G2"]["status"] == "NOT_COVERED"
    assert gates["G3"]["status"] == expected_g3
    if violating_output is not None:
        assert gates["G1"]["shape"] == [1, 2]
        assert gates["G3"]["first_failing_indices"] == [
            {"sample": 0, "output": violating_output},
        ]


def test_nonfinite_board_error_cannot_report_pass():
    audit = {
        "shape": [1, 4],
        "violations": 0,
        "max_abs_error": float("nan"),
        "first_observed_pass_at_utc": "stamp",
        "bitstream_sha256": "a",
        "raw_event_sha256": "b",
    }
    response = board_feedback(audit, expected_shape=[1, 4], expected_bitstream="a")
    assert response["status"] == "ERROR"


def test_optional_gate_feedback_does_not_remove_successful_metrics(tmp_path):
    from odatix_batch import collect_results, file_digest, plan_candidates, write_json

    plan = plan_candidates([{"id": "a", "args": []}], "payload", None, "entry.py")
    row = plan[0]
    folder = tmp_path / row["token"]
    payload = folder / "payload"
    payload.mkdir(parents=True)
    metrics = payload / "metrics.json"
    write_json(metrics, {"eligible": True})
    (folder / "workflow_done").touch()
    write_json(
        folder / "candidate-result.json",
        {
            **row,
            "status": "success",
            "returncode": 0,
            "output_sha256": {"metrics.json": file_digest(metrics)},
        },
    )
    (resolved,) = collect_results(plan, tmp_path, ["metrics.json"])
    from verification_gates import read_returned_gates

    assert resolved["status"] == "success"
    assert "feedback_error" in read_returned_gates(resolved)
