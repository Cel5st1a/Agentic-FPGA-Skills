"""Scan hls4ml configurations using isolated Odatix jobs."""

import argparse
import copy
import math
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


def rank_cases(cases, count):
    return [
        case["id"]
        for case in sorted(
            (case for case in cases if case["eligible"]),
            key=lambda case: (
                case["worst_latency_cycles"],
                case["peak_fraction"],
                case["id"],
            ),
        )[:count]
    ]


def worker(args):
    sys.path.insert(0, str(args.skills / "scripts/line"))
    from fixed_mlp import UnsupportedProfile, probe, profile
    from hls_artifacts import MANIFEST, describe_hls
    from odatix_batch import file_digest, read_json, write_json
    from verification_gates import initial_gates, scan_feedback

    root = Path.cwd()
    contract = root / "p" / f"{args.case}.yaml"
    run = root / "r" / "p" / "c"
    relocated = run / "contract" / "p" / contract.name
    line = args.skills / "scripts/line"
    bootstrap = [
        line / "bootstrap_run.py",
        "--deployment",
        contract,
        "--root",
        root / "r",
        "--model-id",
        "p",
        "--run",
        "c",
        "--skills",
        args.skills,
    ]
    frontend = [
        line / "run_line.py",
        "--deployment",
        relocated,
        "--run",
        run,
        "--skills",
        args.skills,
        "--python",
        args.python,
        "--delegated-hls-lock",
        "--stages",
    ]
    fixed_profile = capture_probe = profile_reason = None
    independent = None
    stage = "bootstrap"
    try:
        result = subprocess.run([str(x) for x in [args.python, *bootstrap]], cwd=root)
        if result.returncode:
            return result.returncode
        stage = "convert"
        result = subprocess.run([str(x) for x in [args.python, *frontend, stage]], cwd=root)
        if result.returncode:
            return result.returncode
        project = run / "p2-convert/project"
        if getattr(args, "four_values", False):
            import numpy as np
            from fixed_reference import evaluate

            cfg = yaml.safe_load(relocated.read_text(encoding="utf-8"))
            conversion = read_json(run / "evidence/convert-evidence.json")
            audit = conversion["configuration_audit"]
            if audit["status"] != "PASSED":
                raise ValueError(
                    "requested configuration differs from emitted values: "
                    f"{audit.get('mismatches')}"
                )
            checks = {
                layer["name"]: layer for layer in conversion["configuration_audit"]["layer_checks"]
            }
            for name, overrides in cfg.get("hls_config", {}).get("LayerName", {}).items():
                for field, requested in overrides.items():
                    if field in {"ReuseFactor", "ExpTableSize", "InvTableSize"}:
                        emitted = checks.get(name, {}).get(field, {}).get("emitted")
                        if emitted != requested:
                            raise ValueError(
                                f"override has no matching generated value: {name}.{field}"
                            )
            original = np.asarray(
                read_json(relocated.parent / cfg["verification"]["inputs"]), dtype=np.float32
            )
            original = original.reshape(len(original), -1)
            testbench = np.loadtxt(
                project / "tb_data/tb_input_features.dat", dtype=np.float32, ndmin=2
            )
            if not np.array_equal(original, testbench):
                raise ValueError(
                    "independent reference input differs from the complete original set"
                )
            independent = evaluate(project, root / "g2", args.compiler)
        try:
            fixed_profile = profile(project)
            capture_probe = probe(project, fixed_profile[0])
        except (UnsupportedProfile, OSError) as exc:
            profile_reason = str(exc)
            fixed_profile = None
        stage = "csim,csynth"
        result = subprocess.run([str(x) for x in [args.python, *frontend, stage]], cwd=root)
        if result.returncode:
            return result.returncode
        stage = None
    finally:
        try:
            gates = scan_feedback(
                run, relocated, fixed_profile, capture_probe, profile_reason, stage
            )
            if independent is not None:
                import numpy as np

                expected = np.asarray(read_json(run / "evidence/python-output.json"), dtype=float)
                expected = expected.reshape(len(expected), -1)
                actual = np.asarray(independent["values"], dtype=float)
                if actual.shape != expected.shape or not np.isfinite(actual).all():
                    raise ValueError("independent G2 does not cover the original outputs")
                errors = np.abs(actual - expected)
                violations = int(np.count_nonzero(errors > 0.001 + 0.001 * np.abs(expected)))
                gates["gates"]["G2"] = {
                    "status": "PASS" if violations == 0 else "FAIL",
                    "diagnostic_only": False,
                    "values": int(actual.size),
                    "violations_vs_G1": violations,
                    "max_abs_error_vs_G1": float(errors.max()),
                    "method": independent["method"],
                    "reference": str(root / "g2/reference.json"),
                    "reference_sha256": file_digest(root / "g2/reference.json"),
                }
        except Exception as exc:  # noqa: BLE001 - diagnostic failure must not replace the tool verdict
            gates = {
                "schema_version": 1,
                "gates": initial_gates(),
                "feedback_error": f"{type(exc).__name__}: {exc}",
            }
        try:
            write_json(root / "gates.json", gates)
        except OSError as exc:
            print(f"gate feedback could not be saved: {exc}", file=sys.stderr)
    evidence = run / "evidence"
    synth = read_json(evidence / "csynth-evidence.json")
    simulation = read_json(evidence / "csim-evidence.json")
    conversion = read_json(evidence / "convert-evidence.json")
    report = ET.parse(run / "p3-build/csynth" / synth["artifacts"]["csynth_xml"]["file"])
    latency = int(report.findtext(".//Worst-caseLatency"))
    fractions = synth["resources_estimate"]["fraction"]
    if not fractions or not all(math.isfinite(v) and v >= 0 for v in fractions.values()):
        raise ValueError("missing or invalid HLS device utilization estimate")
    source = yaml.safe_load(contract.read_text(encoding="utf-8"))
    metrics = {
        "id": args.case,
        "eligible": (
            simulation["numerical_gate"]["passed"]
            and conversion["configuration_audit"]["status"] == "PASSED"
            and max(fractions.values()) <= 1
        ),
        "worst_latency_cycles": latency,
        "interval_max_cycles": report.findtext(".//Interval-max"),
        "peak_fraction": max(fractions.values()),
        "estimated_resources": synth["resources_estimate"]["used"],
        "csim_gate": simulation["numerical_gate"],
        "precision": source["precision"],
        "reuse_factor": source["reuse_factor"],
        "run": str(run),
        "contract": str(relocated),
    }
    if getattr(args, "four_values", False):
        metrics["eligible"] = metrics["eligible"] and all(
            gates["gates"][key]["status"] == "PASS" for key in ("G1", "G2", "G3")
        )
    write_json(run / MANIFEST, describe_hls(run))
    metrics["hls_manifest_sha256"] = file_digest(run / MANIFEST)
    write_json(root / "metrics.json", metrics)
    return 0


def prepare_cases(source, cases, payload):
    contract = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    target = payload / "p"
    shutil.copytree(source.parent, target)
    seen = set()
    for case in cases:
        case_id = case["id"]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", case_id) or case_id in seen:
            raise ValueError(f"invalid or duplicate case ID {case_id!r}")
        seen.add(case_id)
        rf = case["reuse_factor"]
        if type(rf) is not int or rf < 1:
            raise ValueError(f"invalid reuse factor: {rf!r}")
        bits = re.fullmatch(r"ap_fixed<([0-9]+),([0-9]+)>", case["precision"])
        if bits is None or not (1 <= int(bits[2]) <= int(bits[1])):
            raise ValueError(f"invalid ap_fixed precision: {case['precision']!r}")
        variation = {
            **copy.deepcopy(contract),
            "precision": case["precision"],
            "reuse_factor": rf,
            "verification": {
                **contract["verification"],
                "rule": "original_model_abs_rel",
                "atol": 0.001,
                "rtol": 0.001,
            },
        }
        for field, value in case.get("changes", {}).items():
            if (
                not re.fullmatch(
                    r"hls_config\.LayerName\.[A-Za-z_]\w*\.(ReuseFactor|ExpTableSize|InvTableSize)",
                    field,
                )
                or type(value) is not int
                or not 1 <= value <= 65536
            ):
                raise ValueError(f"unsupported candidate override {field}")
            branch = variation
            tokens = field.split(".")
            for token in tokens[:-1]:
                branch = branch.setdefault(token, {})
            branch[tokens[-1]] = value
        (target / f"{case_id}.yaml").write_text(
            yaml.safe_dump(variation, sort_keys=False), encoding="utf-8"
        )


def scan(args):
    from odatix_batch import read_json, run_batch, write_json
    from run_line import Lock
    from verification_gates import read_returned_gates

    cases = read_json(args.cases)
    if not cases or not (1 <= args.jobs <= len(cases)) or args.finalists < 1:
        raise ValueError(
            "require a non-empty candidate set, positive finalists and valid worker count"
        )
    output = args.output.resolve()
    output.mkdir(parents=True)
    payload = output / "inputs"
    payload.mkdir()
    skills = args.skills.resolve()
    prepare_cases(args.deployment.resolve(), cases, payload)
    shutil.copy2(__file__, payload / "scan_hls.py")
    request = {
        "entrypoint": "scan_hls.py",
        "candidates": [
            {
                "id": case["id"],
                "independent": True,
                "args": [
                    "--case",
                    case["id"],
                    "--python",
                    str(args.python.resolve()),
                    "--skills",
                    str(skills),
                    *(
                        ["--four-values", "--compiler", str(args.compiler.resolve())]
                        if args.four_values
                        else []
                    ),
                ],
            }
            for case in cases
        ],
        "expected_outputs": ["metrics.json"],
        "optional_outputs": ["gates.json"],
    }
    write_json(output / "request.json", request)
    with Lock(False) as lock:
        report = run_batch(
            request,
            payload,
            output / "b",
            args.odatix,
            jobs=args.jobs,
            env=lock.child_environment(),
            lock=lock,
        )
    rows = []
    for case in report["candidates"]:
        metrics = Path(case["job_directory"]) / "payload/metrics.json"
        if case["status"] == "success":
            row = {**read_json(metrics), "job_status": "success"}
        else:
            row = {"id": case["id"], "eligible": False, "job_status": case["status"]}
        rows.append({**row, "gate_feedback": read_returned_gates(case)})
    finalists = rank_cases(rows, args.finalists)
    write_json(
        output / "selection.json",
        {
            "cases": rows,
            "finalists": finalists,
            "odatix_returncode": report["odatix_returncode"],
        },
    )
    return 0 if finalists and report["odatix_returncode"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--odatix", type=Path)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--skills", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--finalists", type=int, default=2)
    parser.add_argument("--four-values", action="store_true")
    parser.add_argument("--compiler", type=Path)
    args = parser.parse_args()
    if args.four_values and not args.compiler:
        parser.error("--four-values requires --compiler")
    if args.case:
        return worker(args)
    if not all((args.cases, args.deployment, args.output, args.odatix)):
        parser.error("--cases, --deployment, --output and --odatix are required")
    return scan(args)


if __name__ == "__main__":
    sys.exit(main())
