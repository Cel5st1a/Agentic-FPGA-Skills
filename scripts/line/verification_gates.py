"""Report four numerical levels without changing candidate or board acceptance."""

import math
from pathlib import Path

from odatix_batch import file_digest, read_json, write_json


def initial_gates():
    return {f"G{i}": {"status": "NOT_RUN"} for i in range(1, 5)}


def code_discrepancy(expected, actual):
    if (
        len(expected) != len(actual)
        or not expected
        or any(len(e) != len(a) for e, a in zip(expected, actual, strict=True))
    ):
        raise ValueError("fixed code coverage differs")
    n, first = 0, None
    for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
        for j, (p, o) in enumerate(zip(e, a, strict=True)):
            if p != o:
                n += 1
                if first is None:
                    first = {
                        "sample": i,
                        "output": j,
                        "expected_signed_code": p,
                        "actual_signed_code": o,
                    }
    return {
        "status": "FAIL" if n else "PASS",
        "compared": sum(map(len, expected)),
        "mismatches": n,
        "first": first,
        "boundary": "model_output",
    }


def route_hint(gates):
    if gates["G4"]["status"] == "FAIL" and gates["G3"]["status"] == "PASS":
        return {
            "first_check": "targeted_rtl_replay",
            "check": [
                "effective HLS backend",
                "generated IP",
                "ROM initialization",
                "UART packaging",
            ],
        }
    if gates["G3"].get("fixed_codes", {}).get("status") == "FAIL":
        return {"first_check": "fixed_reference_and_hls_arithmetic"}
    if gates["G2"]["status"] == "FAIL":
        return {"first_check": "quantization_and_precision"}
    if gates["G3"]["status"] == "FAIL":
        return {"first_check": "csim_vs_original"}
    return None


def board_feedback(
    audit=None,
    *,
    expected_shape=None,
    expected_bitstream=None,
    attempted=False,
    error=None,
    source="live",
):
    if audit is None:
        return (
            {
                "status": "ERROR",
                "reason": error or "independent board evidence missing",
                "source": source,
            }
            if attempted
            else {"status": "NOT_RUN"}
        )
    if (
        audit.get("shape") != expected_shape
        or audit.get("bitstream_sha256") != expected_bitstream
        or not expected_bitstream
        or not audit.get("raw_event_sha256")
    ):
        return {
            "status": "ERROR",
            "reason": "board evidence shape or bitstream identity differs",
            "source": source,
        }
    violations = audit.get("violations")
    values = math.prod(expected_shape)
    if type(violations) is not int or not 0 <= violations <= values:
        return {"status": "ERROR", "reason": "invalid violation count", "source": source}
    max_abs = audit.get("max_abs_error")
    if type(max_abs) not in {int, float} or not math.isfinite(max_abs) or max_abs < 0:
        return {"status": "ERROR", "reason": "invalid board error metric", "source": source}
    if violations == 0 and not audit.get("first_observed_pass_at_utc"):
        return {
            "status": "ERROR",
            "reason": "validated board pass is unconfirmed",
            "source": source,
        }
    return {
        "status": "FAIL" if violations else "PASS",
        "source": source,
        "values": values,
        "violations": violations,
        "shape": expected_shape,
        "max_abs_error": audit.get("max_abs_error"),
        "bitstream_sha256": expected_bitstream,
        "raw_event_sha256": audit["raw_event_sha256"],
    }


def check_board_program(capture, bitstream):
    """Bind the actual programming record and readback to one bitstream path and digest."""
    bitstream = bitstream.resolve()
    bit_hash = file_digest(bitstream)
    provenance = read_json(capture / "provenance.json")
    programs = [
        (Path(path), digest)
        for path, digest in provenance.items()
        if Path(path).name == "summary.json"
        and Path(path).resolve() != (capture / "summary.json").resolve()
    ]
    if len(programs) != 1:
        raise ValueError("expected one programming summary in UART provenance")
    path, digest = programs[0]
    if digest != file_digest(path):
        raise ValueError("programming summary hash differs from UART provenance")
    program = read_json(path)
    if (
        program.get("programmed") is not True
        or program.get("bitstream_sha256") != bit_hash
        or Path(program["bitstream"]).resolve() != bitstream
        or read_json(capture / "summary.json")["bitstream_sha256"] != bit_hash
    ):
        raise ValueError("programming and UART readback do not bind the same bitstream")
    return bit_hash


def record_board_feedback(report, audit=None, **identity):
    """Update one candidate report with the outcome of its independent board audit."""
    gates = report["gates"]
    gates["G4"] = board_feedback(audit, **identity)
    report["diagnostic"] = route_hint(gates)


def scan_feedback(run, contract, profile=None, probe=None, profile_reason=None, aborted=None):
    import numpy as np
    import yaml
    from fixed_mlp import PROBE_FILE, evaluate

    gates = initial_gates()
    evidence = run / "evidence"
    project = run / "p2-convert/project"
    reference_path = evidence / "python-output.json"
    reference = None
    try:
        cfg = yaml.safe_load(contract.read_text(encoding="utf-8-sig"))
        spec = cfg["verification"]
        input_path = contract.parent / spec["inputs"]
        model = contract.parent / cfg["model"]["path"]
        conv = read_json(evidence / "convert-evidence.json")
        if conv.get("gate_quantity", {}).get("resolved") == "pre_softmax_logits":
            raise ValueError("the conversion reference contains transformed pre-softmax logits")
        starting = read_json(run / "starting-state.json")
        models = starting["model_files"]
        if (
            not models
            or file_digest(model) != models[cfg["model"]["path"]]["sha256"]
            or any(file_digest(contract.parent / k) != v["sha256"] for k, v in models.items())
        ):
            raise ValueError("original model or associated weights differ from the run manifest")
        ref_meta = conv["reference"]
        if (
            file_digest(contract) != conv["contract_sha256"]
            or file_digest(input_path) != ref_meta["inputs"]["sha256"]
            or file_digest(reference_path) != ref_meta["reference"]["sha256"]
        ):
            raise ValueError("original reference inputs or output failed provenance checks")
        tensor = np.asarray(read_json(reference_path))
        if (
            tensor.ndim < 2
            or tensor.shape[0] != spec["samples"]
            or list(tensor.shape) != ref_meta["shape"]
            or not tensor.size
            or tensor.dtype.kind not in "biuf"
            or not np.isfinite(tensor).all()
        ):
            raise ValueError("original reference is incomplete")
        samples = tensor.shape[0]
        rows = tensor.reshape(samples, -1)
        width = rows.shape[1]
        reference = rows.tolist()
        gates["G1"] = {
            "status": "PASS",
            "shape": [samples, width],
            "output_tensor_shape": list(tensor.shape),
            "samples": samples,
            "inputs_sha256": ref_meta["inputs"]["sha256"],
            "model_files": models,
            "reference_sha256": ref_meta["reference"]["sha256"],
        }
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        gates["G1"] = {"status": "ERROR", "reason": str(exc), "stage_error": aborted}
    expected = None
    if profile and reference is not None:
        try:
            fixed, rf = profile
            expected, provenance = evaluate(project, fixed, read_json(input_path))
            if len(expected) != samples or any(len(r) != width for r in expected):
                raise ValueError("fixed reference does not cover the original input set")
            scale = 1 << fixed.fractional
            deviations = [
                abs((q / scale) - ref)
                for fixed_row, ref_row in zip(expected, reference, strict=True)
                for q, ref in zip(fixed_row, ref_row, strict=True)
            ]
            refs = [x for row in reference for x in row]
            violations = sum(
                e > 0.001 + 0.001 * abs(v) for e, v in zip(deviations, refs, strict=True)
            )
            artifact = evidence / "g2-fixed-reference.json"
            write_json(
                artifact, {"signed_output_codes": expected, "fractional_bits": fixed.fractional}
            )
            gates["G2"] = {
                "status": "FAIL" if violations else "PASS",
                "diagnostic_only": True,
                "coverage": "qualified Dense/ReLU MLP model output",
                "format": [fixed.width, fixed.integer],
                "reuse_factor": rf,
                "values": len(deviations),
                "violations_vs_G1": violations,
                "max_abs_error_vs_G1": max(deviations),
                "source_inputs": provenance,
                "codes_sha256": file_digest(artifact),
            }
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            expected = None
            gates["G2"] = {"status": "ERROR", "diagnostic_only": True, "reason": str(exc)}
    elif profile_reason:
        gates["G2"] = {"status": "NOT_COVERED", "diagnostic_only": True, "reason": profile_reason}
    elif reference is not None:
        gates["G2"] = {"status": "NOT_RUN", "reason": "no fixed reference requested"}
    try:
        path = evidence / "csim-evidence.json"
        if path.is_file():
            sim = read_json(path)
            numerical = sim.get("numerical_gate")
            if numerical and reference is not None:
                if numerical["reference_file_sha256"] != file_digest(reference_path) or sim[
                    "contract_sha256"
                ] != file_digest(contract):
                    raise ValueError("CSim used a different deployment or original reference")
                artifact = sim["artifacts"]["csim_results"]
                result = run / "p3-build/csim" / artifact["file"]
                if result.name != artifact["file"] or file_digest(result) != artifact["sha256"]:
                    raise ValueError("CSim output differs from its recorded hash")
                actual = [
                    [float(v) for v in row.split()]
                    for row in result.read_text(encoding="utf-8").splitlines()
                    if row.strip()
                ]
                if len(actual) != samples or any(
                    len(row) != width or not all(math.isfinite(v) for v in row) for row in actual
                ):
                    raise ValueError("CSim output does not cover the original shape")
                errors, first, count = [], [], 0
                for i, (ref, obs) in enumerate(zip(reference, actual, strict=True)):
                    for j, (expected_value, value) in enumerate(zip(ref, obs, strict=True)):
                        error = abs(value - expected_value)
                        errors.append(error)
                        if error > 0.001 + 0.001 * abs(expected_value):
                            count += 1
                            if len(first) < 20:
                                first.append({"sample": i, "output": j})
                gates["G3"] = {
                    "status": "PASS" if count == 0 and sim["status"] == "PASSED" else "FAIL",
                    "values": len(errors),
                    "violations_vs_G1": count,
                    "first_failing_indices": first,
                    "max_abs_error_vs_G1": max(errors),
                    "csim_evidence_sha256": file_digest(path),
                    "csim_output_sha256": artifact["sha256"],
                    "original_tolerance": [0.001, 0.001],
                }
            else:
                gates["G3"] = {
                    "status": "ERROR",
                    "reason": "no bound CSim numerical comparison",
                    "tool_status": sim.get("status"),
                }
        elif aborted and reference is not None:
            gates["G3"] = {"status": "ERROR", "reason": aborted}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        gates["G3"] = {"status": "ERROR", "reason": str(exc)}
    if expected is not None and probe is not None and gates["G3"]["status"] in {"PASS", "FAIL"}:
        try:
            source = Path(sim["artifacts"]["csim_results"]["source_path"])
            raw = (project / source.parent / PROBE_FILE).resolve()
            if not raw.is_relative_to(project.resolve()):
                raise ValueError("fixed-code trace is outside the CSim project")
            scale = 1 << fixed.width
            unsigned = [[int(c) for c in row.split()] for row in raw.read_text().splitlines()]
            if any(c < 0 or c >= scale for row in unsigned for c in row):
                raise ValueError("invalid raw fixed-code bit range")
            out = code_discrepancy(expected, [[fixed.wrap(c) for c in row] for row in unsigned])
            gates["G3"]["fixed_codes"] = {**out, "code_file_sha256": file_digest(raw), **probe}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            gates["G3"]["fixed_codes"] = {"status": "ERROR", "reason": str(exc)}
    else:
        gates["G3"]["fixed_codes"] = {"status": "NOT_RUN" if not profile_reason else "NOT_COVERED"}
    return {"schema_version": 1, "gates": gates, "diagnostic": route_hint(gates)}


def read_returned_gates(job):
    path = Path(job["job_directory"]) / "payload/gates.json"
    expected = job.get("output_sha256", {}).get("gates.json")
    try:
        if not expected or file_digest(path) != expected:
            raise ValueError("gate feedback is missing or does not match the batch hash")
        report = read_json(path)
        gates = report["gates"]
        allowed = {"PASS", "FAIL", "ERROR", "NOT_RUN", "NOT_COVERED"}
        if (
            report.get("schema_version") != 1
            or not isinstance(gates, dict)
            or gates.keys() != initial_gates().keys()
            or any(
                not isinstance(g, dict) or g.get("status") not in allowed for g in gates.values()
            )
            or not isinstance(gates["G3"].get("fixed_codes", {}), dict)
        ):
            raise ValueError("invalid gate feedback schema")
        return report
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"schema_version": 1, "gates": initial_gates(), "feedback_error": str(exc)}
