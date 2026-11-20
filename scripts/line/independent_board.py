"""Independent raw UART comparison for the model declared in the qualification manifest."""

import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "board-host-io/scripts"))
from host_geometry import ChunkedGeometry, Geometry  # noqa: E402
from protocol import decode_response, encode_request  # noqa: E402

if not __debug__:
    raise RuntimeError("independent board audit requires active evidence checks")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def datetime_utc(text):
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00")))


def audit_capture(folder, workspace, original, reference, started, *, original_path, cable, target):
    summary = read(folder / "summary.json")
    assert summary["attempt_complete"] is True
    assert np.isfinite(reference).all() and reference.ndim == 2
    assert folder.resolve().is_relative_to(workspace.resolve())
    record = summary["host_geometry"]
    shape = ChunkedGeometry if "chunk_bytes" in record else Geometry
    geometry = shape(**{field.name: record[field.name] for field in fields(shape)})
    expected_rows = len(original)
    assert summary["received_samples"] == summary["requested_samples"] == expected_rows
    provenance = {Path(path): value for path, value in read(folder / "provenance.json").items()}
    assert provenance[original_path.resolve()] == digest(original_path), "input provenance differs"
    bits = [
        path
        for path, value in provenance.items()
        if path.suffix == ".bit" and value == summary["bitstream_sha256"]
    ]
    assert len(bits) == 1 and digest(bits[0]) == summary["bitstream_sha256"]
    program_summaries = [
        path
        for path in provenance
        if path.name == "summary.json" and path != folder / "summary.json"
    ]
    assert len(program_summaries) == 1
    program = program_summaries[0]
    assert digest(program) == provenance[program]
    programming = read(program)
    assert programming["programmed"] is True and programming["attempt_complete"] is True
    assert programming["cable_serial"] == cable and programming["target_name"] == target
    logs = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in program.parent.glob("stdout.txt")
    ]
    assert any(
        cable in log and target in log and "AFD_AFTER FPGA is configured" in log for log in logs
    )
    events_path = folder / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    chunks = defaultdict(list)
    for event in events:
        if event["kind"] == "response_chunk":
            chunks[event["sample"]].append(bytes.fromhex(event["rx_hex"]))
    responses = [event for event in events if event["kind"] == "validated_response"]
    requests = [event for event in events if event["kind"] == "request"]
    assert (
        [event["sample"] for event in responses]
        == [event["sample"] for event in requests]
        == list(range(expected_rows))
    )
    outputs = []
    for i, (req, resp) in enumerate(zip(requests, responses, strict=True)):
        assert req["sequence"] == resp["sequence"]
        assert bytes.fromhex(req["tx_hex"]) == encode_request(
            original[i], req["sequence"], geometry
        )
        rx = bytes.fromhex(resp["rx_hex"])
        assert rx == b"".join(chunks[i])
        _, values = decode_response(rx, req["sequence"], geometry, strict=False)
        outputs.append(values)
    data = np.asarray(outputs)
    assert data.shape == reference.shape and np.isfinite(data).all()
    error = np.abs(data - reference)
    tolerance = 0.001 + 0.001 * np.abs(reference)
    ratio = error / tolerance
    violations = int(np.count_nonzero(error > tolerance))
    completion = [e for e in events if e["kind"] == "numerical_comparison_complete"]
    assert len(completion) == 1, "missing or duplicated comparison observation"
    observed = datetime_utc(completion[0]["observed_at_utc"])
    assert observed >= datetime_utc(responses[-1]["observed_at_utc"])
    time = None
    time_kind = None
    if not violations:
        if summary["hardware_numerical_verified"] and completion[0]["passed"]:
            stamp = datetime_utc(summary["timing"]["numeric_success_observed_at"])
            assert stamp == observed
            time = stamp.isoformat()
            time_kind = "host_capture_observer_upper_bound"
        else:
            time = datetime.now(timezone.utc).isoformat()
            time_kind = "independent_audit_observer_upper_bound"
    elapsed = (datetime_utc(time) - datetime_utc(started)).total_seconds() if time else None
    if time:
        assert elapsed > 0
    return {
        "artifact_scope": str(folder),
        "shape": list(data.shape),
        "values": data.tolist(),
        "violations": violations,
        "max_abs_error": float(error.max()),
        "max_tolerance_ratio": float(ratio.max()),
        "bitstream_sha256": digest(bits[0]),
        "raw_event_sha256": digest(events_path),
        "first_observed_pass_at_utc": time,
        "first_observed_pass_time_kind": time_kind,
        "seconds_from_workload_start": elapsed,
    }
