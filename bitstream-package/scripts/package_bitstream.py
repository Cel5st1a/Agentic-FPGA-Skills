"""bitstream-package skill: assemble a deployment package from a verified run.

Contract-driven and generic: the model id is an argument, every other parameter
(model files, board, part, precision, clock, resource limit) comes from the deployment
contract inside the run directory. Nothing is inferred.

Hard rules enforced here
------------------------
A bitstream plus a zero tool exit code is not evidence of correctness, so this script
does not trust the run's evidence files either. Before packaging it:

  1. requires every stage evidence file to exist, parse, and report `status == PASSED`
     (only `cosim` may be `NOT_RUN`, and never `FAILED`);
  2. requires every hard gate block to be a pass (numerical gates, routed resource gate,
     timing);
  3. RECOMPUTES both numerical gates from the raw logs with the line's own frozen
     implementation (`line.evidence.compare`) and refuses if the recomputed verdict,
     violations or margins differ from what the evidence recorded;
  4. re-hashes every artifact the evidence names and refuses on any mismatch;
  5. requires the bitstream file to live inside the run and to match the recorded
     sha256, and to be newer than the RTL and checkpoint it was built from;
  6. requires the wrapper record to be self-consistent (its ROM hash must match the ROM
     actually on disk).
  7. requires the routed checkpoint's port audit to match the wrapper and the bitstream
     evidence; ROM tops are explicitly shipped as configure-only.

Usage
-----
python package_bitstream.py --run <run-dir> --model-id <id> --release-root D:\\ML_models
python package_bitstream.py --run <run-dir> --model-id <id> --release-root <dir> --check-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SKILL_ROOT = Path(__file__).resolve().parents[2]
for candidate in (SKILL_ROOT / "scripts", Path(__file__).resolve().parents[1] / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from line.contract import criterion_from_deployment  # noqa: E402
from line.evidence import compare  # noqa: E402
from line.identity import identity_fields  # noqa: E402
sys.path.insert(0, str(SKILL_ROOT / "board-host-io" / "scripts"))
import board_facts  # noqa: E402

REQUIRED_EVIDENCE = (
    "convert-evidence.json",
    "csim-evidence.json",
    "csynth-evidence.json",
    "vivado-synth-evidence.json",
    "vivado-impl-evidence.json",
    "vivado-bitstream-evidence.json",
    "board-sim-evidence.json",
)
# Stages that must report PASSED. cosim is handled separately: it may honestly be NOT_RUN.
PASS_REQUIRED = tuple(name for name in REQUIRED_EVIDENCE if name != "convert-evidence.json")
HARD_GATES = (
    ("csim-evidence.json", ("numerical_gate", "passed")),
    ("board-sim-evidence.json", ("board_gate", "passed")),
    ("vivado-impl-evidence.json", ("resource_gate", "passed")),
    ("vivado-impl-evidence.json", ("timing", "met")),
    ("vivado-bitstream-evidence.json", ("resource_gate", "passed")),
)
CRITERION_FIELDS = ("rule", "atol", "rtol", "floor", "samples")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_io_scope(run_dir: Path, evidence: dict, refusals: list[str]) -> dict | None:
    """Bind a routed Vivado port audit to this run's wrapper and bitstream evidence."""
    board = run_dir / "p3-build" / "board"
    scope_path = run_dir / "evidence" / "io-port-audit" / "io-scope.json"
    scope = load_json(scope_path, refusals)
    if scope is None:
        refusals.append(f"missing routed io-scope.json: {scope_path}")
        return None
    checkpoint, wrapper_path = board / "routed.dcp", board / "wrapper.json"
    expected = (("checkpoint", checkpoint), ("wrapper", wrapper_path))
    for key, path in expected:
        if Path(scope.get(key, "")).resolve() != path.resolve():
            refusals.append(f"io-scope {key} is not this run's {path.name}")
        if not path.is_file() or scope.get(f"{key}_sha256") != sha256_file(path):
            refusals.append(f"io-scope {key} hash does not match {path.name}")
    recorded_dcp = ((evidence.get("vivado-bitstream-evidence.json") or {}).get("artifacts")
                    or {}).get("routed.dcp", {}).get("sha256")
    if scope.get("checkpoint_sha256") != recorded_dcp:
        refusals.append("io-scope checkpoint differs from bitstream build evidence")
    wrapper = load_json(wrapper_path, refusals) or {}
    if not scope.get("wrapper_ports_match") or set(scope.get("top_ports") or []) != set(wrapper.get("top_ports") or []):
        refusals.append("io-scope top ports disagree with the wrapper")
    details = scope.get("ports") or {}
    if set(details) != set(scope.get("top_ports") or []):
        refusals.append("io-scope has no complete routed port details")
    host = (details.get("uart_rx", {}).get("direction") == "IN" and
            details.get("uart_tx", {}).get("direction") == "OUT")
    if scope.get("host_input_output_available") is not host:
        refusals.append("io-scope host path declaration disagrees with routed directions")
    if scope.get("verification_scope") != ("host-path-present-unverified" if host else "configure-only"):
        refusals.append("io-scope verification scope disagrees with routed ports")
    if scope.get("hardware_verified") is not False or scope.get("on_board_numerical") != "NOT_RUN":
        refusals.append("io-scope claims hardware verification before a live board run")
    return scope


def load_json(path: Path, refusals: list[str]) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        refusals.append(f"evidence file is not valid JSON: {path.name} ({exc})")
        return None


def read_results(path: Path) -> list[list[float]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append([float(value) for value in line.split()])
    return rows


def dig(node: dict, keys: tuple[str, ...]):
    current = node
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def find_contract(run_dir: Path) -> Path:
    candidates = sorted((run_dir / "contract").rglob("deployment.yaml"))
    if len(candidates) != 1:
        raise SystemExit(
            f"expected exactly one deployment.yaml under {run_dir / 'contract'}, found {len(candidates)}"
        )
    return candidates[0]


def resolve(contract_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (contract_dir / path).resolve()


def pins_profile(skills: Path, contract_profile: dict) -> Path | None:
    """The skills' board profile for the same board: carries the board-facing pins."""
    ident = str(contract_profile.get("id") or contract_profile.get("board_id") or "").lower()
    part = str(contract_profile.get("part") or (contract_profile.get("fpga") or {}).get("part") or "")
    matches = []
    for profile in sorted((skills / "board-profiles").rglob("board_profile.yaml")):
        data = yaml.safe_load(profile.read_text(encoding="utf-8-sig")) or {}
        profile_id = str(data.get("board_id") or data.get("id") or "").lower()
        profile_part = str(data.get("part") or (data.get("fpga") or {}).get("part") or "")
        if (ident and profile_id and ident == profile_id) or (part and part == profile_part):
            matches.append(profile)
    return matches[0] if len(matches) == 1 else None


def same_numbers(left: dict, right: dict, keys) -> tuple[bool, dict]:
    """Field-by-field comparison of a recomputed gate against the recorded one.

    A field the producer recorded as `None` was not recorded at all (for instance the
    file digest of a log the stage referenced by a relative path), so it is reported as
    not-recorded rather than as a difference; the artifacts' own sha256 records are what
    bind those files.
    """
    differences = {}
    not_recorded = {}
    for key in keys:
        a, b = left.get(key), right.get(key)
        if b is None and key.endswith("_sha256"):
            not_recorded[key] = "recorded as null"
            continue
        if isinstance(a, float) or isinstance(b, float):
            if a is None or b is None or abs(float(a) - float(b)) > 1e-12:
                differences[key] = {"recorded": b, "recomputed": a}
        elif a != b:
            differences[key] = {"recorded": b, "recomputed": a}
    if not_recorded:
        differences["_not_recorded"] = not_recorded
    return not {k: v for k, v in differences.items() if k != "_not_recorded"}, differences


def check_artifacts(evidence: dict, refusals: list[str], label: str, bases: tuple[Path, ...]) -> dict:
    """Every artifact the evidence names must still hash to what was recorded.

    Artifact records use `path` when the producer wrote an absolute one, and otherwise
    `source_path`/`file` relative to the stage's own working directory - which is the
    hls4ml project for the Vitis stages and the board directory for the Vivado stages, so
    both bases are tried. A record that carries a sha256 but no resolvable file is a
    refusal, never a silent skip.
    """
    verified = {}
    for name, record in (evidence.get("artifacts") or {}).items():
        if not isinstance(record, dict):
            continue
        candidate = None
        if record.get("path"):
            candidate = Path(record["path"])
        else:
            relative = record.get("source_path") or record.get("file")
            if relative:
                for base in bases:
                    attempt = base / relative
                    if attempt.is_file():
                        candidate = attempt
                        break
                else:
                    candidate = bases[0] / relative
        if candidate is None:
            if record.get("sha256"):
                refusals.append(f"{label}: artifact {name} records a hash but no file to check")
            continue
        if not candidate.is_file():
            refusals.append(f"{label}: artifact {name} is missing: {candidate}")
            continue
        actual = sha256_file(candidate)
        recorded = record.get("sha256")
        verified[name] = {"path": str(candidate), "sha256": actual, "recorded": recorded}
        if not recorded:
            refusals.append(f"{label}: artifact {name} has no recorded sha256")
        elif actual != recorded:
            refusals.append(f"{label}: artifact {name} changed since the stage ran ({actual} != {recorded})")
    return verified


def verify_board_identity(contract_path: Path, wrapper: dict, records) -> list[str]:
    """New recorded fingerprints must still describe the files being packaged.

    Legacy evidence without the added board hashes keeps its existing gate checks.
    """
    facts = board_facts.facts_for_wrapper(wrapper)
    current = identity_fields(contract_path, facts.profile_path)
    errors = []
    for source, record in records:
        for key in ("contract_sha256", "board_profile_sha256", "board_pins_profile_sha256"):
            if key in record and record[key] != current[key]:
                errors.append(f"{source}: {key} changed since build; use a new run")
    return errors


def verify_run(run_dir: Path, contract_path: Path, contract: dict) -> tuple[list[str], dict]:
    refusals: list[str] = []
    evidence_dir = run_dir / "evidence"
    board = run_dir / "p3-build" / "board"
    evidence = {name: load_json(evidence_dir / name, refusals) for name in REQUIRED_EVIDENCE}
    cosim = load_json(evidence_dir / "cosim-evidence.json", refusals)

    report: dict = {"stages": {}}
    for name in REQUIRED_EVIDENCE:
        node = evidence.get(name)
        if node is None:
            if not any(name in refusal for refusal in refusals):
                refusals.append(f"missing evidence file: evidence/{name}")
            report["stages"][name.removesuffix("-evidence.json")] = "MISSING"
            continue
        if name == "convert-evidence.json":
            # convert-evidence.json deliberately carries no pass/fail: the conversion is
            # not a numerical verdict. It is recorded as such instead of being dressed up
            # as PASSED.
            report["stages"]["convert"] = ("RECORDED" if node.get("hls4ml_config_path")
                                           else "INCOMPLETE")
            continue
        status = node.get("status")
        report["stages"][name.removesuffix("-evidence.json")] = status
        if name in PASS_REQUIRED and status != "PASSED":
            refusals.append(f"{name}: status={status!r} (reasons: {node.get('reasons')})")
    if cosim is None:
        report["stages"]["cosim"] = "MISSING"
    else:
        report["stages"]["cosim"] = cosim.get("status")
        if cosim.get("status") not in {"NOT_RUN", "PASSED"}:
            refusals.append(f"cosim-evidence.json: status={cosim.get('status')!r} (reasons: {cosim.get('reasons')})")
        # A cosim whose only per-value log is the C-wrapper phase compares C++ against C++.
        # It must never be packaged as RTL evidence, whatever its status says.
        if cosim.get("status") == "PASSED" and \
                (cosim.get("rtl_gate") or {}).get("phase") == "c_wrapper":
            refusals.append(
                "cosim-evidence.json claims PASSED from the C-wrapper phase, which is not RTL "
                "evidence; the stage must be recorded as NOT_RUN"
            )

    for name, keys in HARD_GATES:
        node = evidence.get(name)
        if node is None:
            continue
        value = dig(node, keys)
        if value is not True:
            refusals.append(f"{name}: {'.'.join(keys)} is {value!r}, expected True")

    # --- artifacts unchanged since the stage ran -------------------------------------
    report["artifacts"] = {}
    project = run_dir / "p2-convert" / "project"
    for name in REQUIRED_EVIDENCE:
        node = evidence.get(name)
        if node:
            report["artifacts"][name] = check_artifacts(node, refusals, name, (board, project))

    # --- recompute the two numerical gates from the raw logs -------------------------
    criterion = criterion_from_deployment(contract)
    reference_json = evidence_dir / "python-output.json"
    convert = evidence.get("convert-evidence.json") or {}
    recorded_reference = convert.get("reference", {}).get("reference", {})
    if not reference_json.is_file():
        refusals.append(f"python reference is missing: {reference_json}")
    else:
        recorded_sha = recorded_reference.get("sha256")
        actual_sha = sha256_file(reference_json)
        if recorded_sha and recorded_sha != actual_sha:
            refusals.append(
                f"python reference changed since conversion ({actual_sha} != {recorded_sha})"
            )
    csim_log = run_dir / "p3-build" / "csim" / "csim_results.log"
    board_evidence = evidence.get("board-sim-evidence.json") or {}
    board_source = (board_evidence.get("artifacts", {}).get("board_results") or {}).get("source_path")
    board_log = (board / board_source) if board_source else None
    if not csim_log.is_file():
        refusals.append(f"csim results log is missing: {csim_log}")
    if board_log is None or not board_log.is_file():
        refusals.append(f"board simulation log is missing under {board} (source_path={board_source!r})")

    # The RTL-level gate is a behavioural simulation of the RTL that synthesis consumed, so
    # the package has to prove that those are the same files. Both stages stage the RTL with
    # `stage_rtl` and record every file's sha256, which makes this checkable instead of
    # asserted: if the simulated RTL and the synthesised RTL diverge, the numbers describe a
    # different design than the bitstream.
    simulated_rtl = (board_evidence.get("staged_rtl") or {}).get("files") or {}
    synthesised_rtl = ((evidence.get("vivado-synth-evidence.json") or {}).get("staged_rtl")
                       or {}).get("files") or {}
    if not simulated_rtl or not synthesised_rtl:
        refusals.append(
            "the board simulation and the synthesis stage do not both record the staged RTL "
            "hashes, so it cannot be shown that the value-checked RTL is the synthesised RTL"
        )
    elif simulated_rtl != synthesised_rtl:
        differing = sorted(
            name for name in set(simulated_rtl) | set(synthesised_rtl)
            if simulated_rtl.get(name) != synthesised_rtl.get(name)
        )
        refusals.append(
            "the board simulation did not run the RTL that synthesis consumed; differing "
            f"files: {differing}"
        )

    recomputed: dict = {}
    if reference_json.is_file() and csim_log.is_file():
        reference = json.loads(reference_json.read_text(encoding="utf-8"))
        cpp_gate = compare(
            "package_recheck_hls_cpp", reference, read_results(csim_log), criterion,
            reference_source=str(reference_json), actual_source=str(csim_log),
        )
        recomputed["hls_cpp_vs_python"] = cpp_gate
        recorded = (evidence.get("csim-evidence.json") or {}).get("numerical_gate") or {}
        # The digests are part of the comparison on purpose: a single value changed by less
        # than the tolerance would leave passed/max_abs_error/margin identical while the
        # evidence's own recorded digests no longer match the log being packaged.
        same, differences = same_numbers(
            cpp_gate, recorded,
            ("passed", "max_abs_error", "max_error_over_tolerance",
             "reference_sha256", "actual_sha256", "reference_file_sha256", "actual_file_sha256"),
        )
        if not same:
            refusals.append(f"csim gate does not reproduce from the raw logs: {differences}")
        if (recomputed["hls_cpp_vs_python"].get("values") or {}).get("violations") != \
                (recorded.get("values") or {}).get("violations"):
            refusals.append("csim gate violation count does not reproduce from the raw logs")
        if not cpp_gate["passed"]:
            refusals.append(
                f"recomputed HLS C++ vs python gate FAILED: "
                f"{(cpp_gate.get('values') or {}).get('violations')}/"
                f"{(cpp_gate.get('values') or {}).get('total')} values out of tolerance"
            )
        recorded_criterion = recorded.get("criterion") or {}
        drift = {k: {"recorded": recorded_criterion.get(k), "recomputed": criterion.to_dict().get(k)}
                 for k in CRITERION_FIELDS if recorded_criterion.get(k) != criterion.to_dict().get(k)}
        if drift:
            refusals.append(f"the contract's criterion changed since the csim stage: {drift}")

    if board_log is not None and board_log.is_file() and csim_log.is_file():
        wrapper = load_json(board / "wrapper.json", refusals)
        # The board log carries the output stream's own element grid, which is not always the
        # contract's precision; the wrapper records it (falling back to the ROM grid, which is
        # what the ap_vld path has always used).
        grid = ((wrapper or {}).get("output_grid")
                or ((wrapper or {}).get("rom_image") or {}).get("grid") or {})
        if not grid:
            refusals.append("wrapper.json has no rom_image grid; the board log cannot be scaled")
        else:
            scale = float(2 ** (grid["total_bits"] - grid["integer_bits"]))
            raw = [[int(round(float(value))) for value in line.split()]
                   for line in board_log.read_text(encoding="utf-8", errors="replace").splitlines()
                   if line.strip()]
            rtl_gate = compare(
                "package_recheck_rtl", read_results(csim_log),
                [[value / scale for value in row] for row in raw], criterion,
                reference_source=str(csim_log), actual_source=str(board_log),
            )
            rtl_gate["bit_exact_on_the_grid"] = [
                [int(round(value * scale)) for value in row] for row in read_results(csim_log)
            ] == raw
            rtl_gate["grid"] = {"total_bits": grid["total_bits"], "integer_bits": grid["integer_bits"],
                                "scale": scale}
            recomputed["rtl_vs_hls_cpp"] = rtl_gate
            recorded = board_evidence.get("board_gate") or {}
            same, differences = same_numbers(
                rtl_gate, recorded,
                ("passed", "max_abs_error", "max_error_over_tolerance",
                 "reference_sha256", "actual_sha256", "reference_file_sha256", "actual_file_sha256"),
            )
            if not same:
                refusals.append(f"board gate does not reproduce from the raw logs: {differences}")
            if (rtl_gate.get("values") or {}).get("violations") != (recorded.get("values") or {}).get("violations"):
                refusals.append("board gate violation count does not reproduce from the raw logs")
            if not rtl_gate["passed"]:
                refusals.append(
                    f"recomputed board gate FAILED: "
                    f"{(rtl_gate.get('values') or {}).get('violations')}/"
                    f"{(rtl_gate.get('values') or {}).get('total')} values out of tolerance"
                )
    report["recomputed_gates"] = recomputed

    # --- the wrapper record must describe the files that are actually there ----------
    wrapper_path = board / "wrapper.json"
    wrapper = load_json(wrapper_path, refusals)
    if wrapper is None:
        refusals.append(f"missing wrapper record: {wrapper_path}")
    else:
        rom = wrapper.get("rom_image") or {}
        rom_path = Path(rom.get("path", ""))
        if not rom_path.is_file():
            refusals.append(f"wrapper.json names a ROM image that does not exist: {rom.get('path')}")
        elif rom.get("sha256") != sha256_file(rom_path):
            refusals.append(
                "wrapper.json is stale: rom_image.sha256 does not match the ROM file it names "
                f"({sha256_file(rom_path)} != {rom.get('sha256')})"
            )
        core = Path(wrapper.get("core_verilog", ""))
        if not core.is_file():
            refusals.append(f"wrapper.json names a core Verilog that does not exist: {core}")
        elif wrapper.get("core_verilog_sha256") != sha256_file(core):
            refusals.append("wrapper.json is stale: core_verilog_sha256 does not match the core file")
    if wrapper is not None:
        try:
            facts = board_facts.facts_for_wrapper(wrapper)
            report["board_facts"] = facts.to_record()
            identity_records = [(name, node) for name, node in evidence.items() if node]
            identity_records.append(("wrapper.json", wrapper))
            for name in ("starting-state.json", "run-plan.json"):
                if (run_dir / name).is_file():
                    node = load_json(run_dir / name, refusals)
                    if node:
                        identity_records.append((name, node))
            refusals.extend(verify_board_identity(contract_path, wrapper, identity_records))
        except ValueError as exc:
            refusals.append(str(exc))
    report["wrapper"] = wrapper

    # --- the bitstream itself -------------------------------------------------------
    bit_evidence = (evidence.get("vivado-bitstream-evidence.json") or {}).get("artifacts", {}).get("bitstream") or {}
    bit_path = Path(bit_evidence["path"]).resolve() if bit_evidence.get("path") else board / "board_top.bit"
    report["bitstream"] = {"path": str(bit_path), "recorded_sha256": bit_evidence.get("sha256")}
    if not bit_evidence.get("path"):
        refusals.append("bitstream evidence does not name the bitstream file")
    elif run_dir.resolve() not in bit_path.parents:
        refusals.append(f"bitstream evidence points outside the run: {bit_path}")
    if not bit_evidence.get("sha256"):
        refusals.append("bitstream evidence carries no sha256; the file cannot be bound to the build")
    if not bit_path.is_file():
        refusals.append(f"bitstream file not found: {bit_path}")
    else:
        actual = sha256_file(bit_path)
        report["bitstream"]["sha256"] = actual
        report["bitstream"]["bytes"] = bit_path.stat().st_size
        if bit_evidence.get("sha256") and actual != bit_evidence["sha256"]:
            refusals.append(f"bitstream sha256 mismatch: file {actual} != evidence {bit_evidence['sha256']}")
        # a bitstream older than its own inputs would package the previous design
        bit_mtime = bit_path.stat().st_mtime
        for name in ("board_top.v", "board_top_tb.v", "board.xdc", "input_words.mem"):
            source = board / name
            if source.is_file() and source.stat().st_mtime > bit_mtime:
                refusals.append(f"stale bitstream: {name} is newer than {bit_path.name}")
        routed = board / "routed.dcp"
        if routed.is_file() and routed.stat().st_mtime > bit_mtime:
            refusals.append(f"stale bitstream: {routed.name} is newer than {bit_path.name}")
    scope = verify_io_scope(run_dir, evidence, refusals)
    report["io_scope"] = scope
    if scope is not None and scope.get("host_input_output_available"):
        refusals.append("this packager handles the ROM variant only; host-I/O bitstreams need a host package gate")
    return refusals, report


def copy_if_exists(source: Path | None, target: Path) -> bool:
    if source is None or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def drc_summary(path: Path) -> dict | None:
    """Severity counts from a Vivado DRC report, when the report carries them.

    The routed DRC report often has only a total check count; the error/warning split lives
    in the implementation log. Saying "191" without saying what it counts would read as a
    bare number, so the key names carry the meaning and the raw report is packaged too.
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    summary: dict = {}
    for label, key in (("Errors", "errors"), ("Critical Warnings", "critical_warnings"),
                       ("Warnings", "warnings"), ("Checks", "checks_found")):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(label):
                numbers = [int(token) for token in stripped.replace("|", " ").split() if token.isdigit()]
                if numbers:
                    summary.setdefault(key, numbers[-1])
                break
    if not summary:
        return None
    summary.setdefault("severities", "see reports/drc.rpt; the implementation log records "
                                     "the error/critical-warning split")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True, help="verified run directory")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true", help="evaluate the gates, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing <release-root>/<model-id> directory")
    args = parser.parse_args()

    run_dir: Path = args.run.resolve()
    evidence_dir = run_dir / "evidence"
    board = run_dir / "p3-build" / "board"
    convert_dir = run_dir / "p2-convert" / "project"
    contract_path = find_contract(run_dir)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8-sig"))

    refusals, report = verify_run(run_dir, contract_path, contract)
    if refusals:
        print(json.dumps({"model": args.model_id, "packaged": False, "refusals": refusals},
                         indent=2, ensure_ascii=False))
        return 2
    facts = board_facts.facts_for_wrapper(report.get("wrapper") or {})
    if args.check_only:
        print(json.dumps({"model": args.model_id, "packaged": False, "check": "OK", "report": report},
                         indent=2, ensure_ascii=False, default=str))
        return 0

    destination = args.release_root.resolve() / args.model_id
    if destination.exists():
        if not args.force:
            raise SystemExit(
                f"a package already exists at {destination}. Re-packaging replaces it, so it "
                "has to be asked for explicitly: pass --force (the previous package is "
                "checked by --check-only first, not merged)."
            )
        shutil.rmtree(destination)
    for sub in ("reports", "constraints", "evidence", "logs"):
        (destination / sub).mkdir(parents=True)

    bit_path = Path(report["bitstream"]["path"])
    shutil.copy2(bit_path, destination / f"{args.model_id}.bit")

    reports = {
        board / "timing_routed.rpt": "reports/timing_impl.rpt",
        board / "utilization_routed.rpt": "reports/utilization_impl.rpt",
        board / "utilization_synth.rpt": "reports/utilization_synth.rpt",
        board / "drc_routed.rpt": "reports/drc.rpt",
        board / "clocks.rpt": "reports/clocks.rpt",
        board / "timing_synth.rpt": "reports/timing_synth.rpt",
        run_dir / "p3-build" / "csynth" / "csynth-summary.json": "reports/csynth-summary.json",
        run_dir / "p3-build" / "csynth" / "myproject_csynth.xml": "reports/myproject_csynth.xml",
    }
    for source, relative in reports.items():
        copy_if_exists(source, destination / relative)
    copy_if_exists(board / "board.xdc", destination / "constraints" / "board.xdc")
    copy_if_exists(board / "wrapper.json", destination / "board-wrapper.json")
    copy_if_exists(run_dir / "evidence" / "io-port-audit" / "io-scope.json",
                   destination / "io-scope.json")
    copy_if_exists(convert_dir / "hls4ml_config.yml", destination / "hls4ml_config.yaml")
    copy_if_exists(contract_path, destination / "deployment.yaml")
    profile_value = contract.get("board_profile")
    contract_profile_path = resolve(contract_path.parent, profile_value) if profile_value else None
    contract_profile = {}
    if copy_if_exists(contract_profile_path, destination / "board_profile.yaml"):
        contract_profile = yaml.safe_load(contract_profile_path.read_text(encoding="utf-8-sig")) or {}
    pins_path = facts.profile_path
    copy_if_exists(pins_path, destination / "board_pins_profile.yaml")
    copy_if_exists(run_dir / "RUN.md", destination / "RUN.md")
    copy_if_exists(run_dir / "starting-state.json", destination / "starting-state.json")
    copy_if_exists(run_dir / "p3-build" / "csim" / "csim_results.log", destination / "logs" / "csim_results.log")
    board_source = (report["artifacts"].get("board-sim-evidence.json", {})
                    .get("board_results", {}).get("path"))
    copy_if_exists(Path(board_source) if board_source else None, destination / "logs" / "board_results.log")
    for name in list(REQUIRED_EVIDENCE) + ["cosim-evidence.json"]:
        copy_if_exists(evidence_dir / name, destination / "evidence" / name)

    model_files: dict[str, dict] = {}
    references = [contract.get("model", {}).get("path")] + list(
        contract.get("model", {}).get("associated_files") or []
    )
    for relative in filter(None, references):
        candidate = resolve(contract_path.parent, relative)
        if candidate.is_file():
            model_files[relative] = {
                "sha256": sha256_file(candidate),
                "bytes": candidate.stat().st_size,
                "shipped_in_package": False,
                "why": "the model file lives in the frozen contract, which is packaged as "
                       "deployment.yaml; copying it here would duplicate that input",
            }

    bitstream_evidence = json.loads((evidence_dir / "vivado-bitstream-evidence.json").read_text(encoding="utf-8"))
    impl_evidence = json.loads((evidence_dir / "vivado-impl-evidence.json").read_text(encoding="utf-8"))
    csim_evidence = json.loads((evidence_dir / "csim-evidence.json").read_text(encoding="utf-8"))
    board_evidence = json.loads((evidence_dir / "board-sim-evidence.json").read_text(encoding="utf-8"))
    cosim_evidence = json.loads((evidence_dir / "cosim-evidence.json").read_text(encoding="utf-8")) \
        if (evidence_dir / "cosim-evidence.json").is_file() else {}
    csim_gate = csim_evidence["numerical_gate"]
    board_gate = board_evidence["board_gate"]
    cosim_status = cosim_evidence.get("status", "NOT_RUN") if cosim_evidence \
        else "NOT_RUN (stage was not invoked in this run)"
    drc = drc_summary(board / "drc_routed.rpt")
    wrapper = report.get("wrapper") or {}
    deployment = {
        key: contract.get(key)
        for key in ("precision", "reuse_factor", "strategy", "io_type", "top_dataflow",
                    "clock_period_ns", "backend", "layer_reuse_factors", "constraints")
    }

    # Machine-written verification card. Generated from the evidence (never typed by hand)
    # so a number in the package cannot drift from the number that was measured.
    verification_lines = [
        f"# Verification card - {args.model_id}",
        "",
        "Generated by `bitstream-package/scripts/package_bitstream.py` from the run's own",
        "evidence files. Every number below is read from, or recomputed against, those files.",
        "",
        "| item | value |",
        "| --- | --- |",
        f"| run | `{run_dir.name}` |",
        f"| contract | `{contract_path.name}` (sha256 `{sha256_file(contract_path)}`) |",
        f"| deployment | {deployment.get('precision')}, rf={deployment.get('reuse_factor')}, "
        f"{deployment.get('io_type')}, {deployment.get('strategy')}, "
        f"clock {deployment.get('clock_period_ns')} ns |",
        f"| bitstream | `{args.model_id}.bit`, {report['bitstream']['bytes']:,} bytes |",
        f"| bitstream sha256 | `{report['bitstream']['sha256']}` |",
        "",
        "## Gates (recomputed by the packager from the raw logs)",
        "",
        f"- criterion: `{csim_gate['criterion']['rule']}` - atol {csim_gate['criterion']['atol']} "
        f"(from {csim_gate['criterion'].get('atol_source') or csim_gate['criterion'].get('source')}), "
        f"rtol {csim_gate['criterion']['rtol']}, {csim_gate['criterion']['samples']} samples, per value",
        f"- HLS C++ vs python reference: {csim_gate['passed']}, "
        f"{csim_gate['values']['violations']}/{csim_gate['values']['total']} violations, "
        f"max error {csim_gate['max_abs_error']}, margin {csim_gate['max_error_over_tolerance']}",
        f"- RTL vs HLS C++ (board simulation of the RTL submitted to synthesis): {board_gate['passed']}, "
        f"{board_gate['values']['violations']}/{board_gate['values']['total']} violations, "
        f"max error {board_gate['max_abs_error']}, margin {board_gate['max_error_over_tolerance']}",
        f"- routed resource gate: {impl_evidence['resource_gate']['passed']}, worst "
        f"{impl_evidence['resource_gate']['worst']} = {impl_evidence['resource_gate']['worst_fraction']} "
        f"(limit {impl_evidence['resource_gate']['limit']})",
        f"- timing: WNS {impl_evidence['timing']['wns_ns']} ns, WHS {impl_evidence['timing']['whs_ns']} ns, "
        f"met={impl_evidence['timing']['met']}",
        f"- DRC: {drc if drc else 'see reports/drc.rpt'}",
        "",
        "## What was NOT done",
        "",
        "- **not programmed**: no JTAG download, no on-board inference. `hardware_verified = false`.",
        f"- **hls4ml traced RTL cosim**: `{cosim_status}`"
        + (f" - {cosim_evidence.get('reasons')}" if cosim_evidence.get("reasons") else ""),
        "  The RTL-level per-value evidence above is an xsim behavioural simulation of the RTL",
        "  that was staged for synthesis - the same files, by sha256, that the synthesis stage",
        "  consumed (this package refuses to assemble if those hashes differ). It is not the",
        "  hls4ml traced cosim. It is also **not** a post-synthesis netlist simulation: the",
        "  testbench observes the result stream hierarchically, and a synthesised netlist",
        "  flattens those signals (Vivado's funcsim netlist keeps only the kept checksum",
        "  register), so netlist-level per-value evidence is not obtainable from this board",
        "  top. What the gate therefore covers is the RTL behaviour of the exact design that",
        "  was synthesised; post-synthesis/optimisation/place-and-route behaviour is not",
        "  covered by it.",
        "- The bitstream is an artifact, not a correctness proof: the gates above are the proof.",
        "",
        "## Board-vs-contract timing note",
        "",
        f"`constraints/board.xdc` constrains the {facts.reference_mhz:g} MHz {facts.clock_kind} reference input",
        f"(`create_clock ... {1000 / facts.reference_mhz:g}`); the core runs at",
        f"{deployment.get('clock_period_ns')} ns after the MMCM. The two numbers describe different clocks.",
        "",
        "## How the two sides of the board gate were fed",
        "",
        f"The wrapper's sample ROM was written with `{(wrapper or {}).get('rom_image', {}).get('quantisation', 'unknown')}`"
        " quantisation"
        + (" (it matches the cast the generated C++ testbench applies, so both sides receive the"
           " same input values)."
           if (wrapper or {}).get("rom_image", {}).get("quantisation")
           else " - for packages built before this rule was recorded, the ROM used"
                " round-half-to-even while the C++ cast truncates, so a few LSB of the board"
                " gate's error are an input convention rather than RTL arithmetic."),
        "",
    ]
    # What the *current* board profile says about the clock IOSTANDARD override: the run's
    # frozen wrapper record was written when the design was built, which may predate an
    # operator sign-off, so both are reported.
    current_clock = {}
    if pins_path and Path(pins_path).is_file():
        current_profile = yaml.safe_load(Path(pins_path).read_text(encoding="utf-8-sig")) or {}
        current_clock = ((current_profile.get("board_pins") or {}).get("clock") or {})
    if current_clock.get("iostandard"):
        if current_clock.get("operator_confirmation") and current_clock.get("requires_operator_confirmation") is False:
            verification_lines += [
                "## Clock IOSTANDARD override",
                "",
                f"The clock pair uses `{current_clock['iostandard']}` rather than the manual's",
                "standard, because the bank's VCCO rejects it and Vivado would leave both clock",
                f"ports at IOSTANDARD DEFAULT (D-019). The board profile records the operator's",
                f"confirmation: {str(current_clock.get('operator_confirmation', '')).strip()}",
                "",
            ]
        elif current_clock.get("requires_operator_confirmation") is True:
            verification_lines += [
                "## Open item for the operator",
                "",
                "The clock pair's IOSTANDARD override still carries",
                "`requires_operator_confirmation: true` in `board_pins_profile.yaml`: the pins are",
                "documented (manual + validated vendor IO report) but the manual's own XDC says a",
                "different standard, so the override needs a human sign-off (DEFECTS D-019).",
                "",
            ]
    (destination / "VERIFICATION.md").write_text("\n".join(verification_lines), encoding="utf-8")

    readme_lines = [
        f"# {args.model_id} - {facts.board_id} bitstream package",
        "",
        "| item | value |",
        "| --- | --- |",
        f"| model | `{args.model_id}` |",
        f"| board / part | {contract_profile.get('id') or contract_profile.get('board_id') or 'see board_profile.yaml'} "
        f"/ `{bitstream_evidence.get('part')}` |",
        f"| top | `{bitstream_evidence.get('top')}` |",
        f"| deployment | precision `{deployment.get('precision')}`, rf={deployment.get('reuse_factor')}, "
        f"{deployment.get('io_type')}, {deployment.get('strategy')}, "
        f"clock {deployment.get('clock_period_ns')} ns |",
        f"| resource gate | limit {impl_evidence['resource_gate']['limit']}, worst "
        f"{impl_evidence['resource_gate']['worst']} = {impl_evidence['resource_gate']['worst_fraction']} |",
        f"| timing | WNS {impl_evidence['timing']['wns_ns']} ns, "
        f"WHS {impl_evidence['timing']['whs_ns']} ns, met={impl_evidence['timing']['met']} |",
        "",
        "## Numerical evidence (frozen criterion, recomputed at packaging time)",
        "",
        f"`{csim_gate['criterion']['rule']}`: atol {csim_gate['criterion']['atol']} "
        f"(source `{csim_gate['criterion'].get('atol_source') or csim_gate['criterion'].get('source')}`), "
        f"rtol {csim_gate['criterion']['rtol']}, {csim_gate['criterion']['samples']} samples, per-value.",
        "",
        "| gate | verdict | violations | max_abs_error | margin (max err / tol) |",
        "| --- | --- | --- | --- | --- |",
        f"| HLS C++ vs python reference | {csim_gate['passed']} | "
        f"{csim_gate['values']['violations']} / {csim_gate['values']['total']} | "
        f"{csim_gate['max_abs_error']} | {csim_gate['max_error_over_tolerance']} |",
        f"| RTL vs HLS C++ (board sim of the RTL sent to synthesis) | {board_gate['passed']} | "
        f"{board_gate['values']['violations']} / {board_gate['values']['total']} | "
        f"{board_gate['max_abs_error']} | {board_gate['max_error_over_tolerance']} |",
        "",
        "## What was NOT done",
        "",
        "- **not programmed**: no JTAG download, no on-board inference. `hardware_verified = false`.",
        f"- **hls4ml traced RTL cosim**: `{cosim_status}`.",
        "- The bitstream is an artifact, not a correctness proof: the gates above are the proof.",
        "",
        "## Content",
        "",
        "```",
        f"{destination.name}/",
        f"  {args.model_id}.bit          bitstream (sha256 {report['bitstream']['sha256']})",
        "  MANIFEST.json                build metadata, gates, checksums",
        "  CHECKSUMS.sha256             sha256 of every file except MANIFEST.json itself",
        "  VERIFICATION.md              machine-generated verification card",
        "  deployment.yaml              frozen deployment contract",
        "  board_profile.yaml           the contract's board profile (part, clock source)",
        "  board_pins_profile.yaml      the skills' profile: board-facing pins + provenance",
        "  board-wrapper.json           board wrapper description and pin provenance",
        "  hls4ml_config.yaml           conversion config actually applied",
        "  constraints/board.xdc        pin / clock constraints actually used",
        "  reports/                     timing, utilization, DRC, clocks, csynth",
        "  evidence/                    the gate evidence JSONs this package was built from",
        "  logs/                        csim and board-simulation raw logs",
        "```",
        "",
        "Reproduce: `RUN.md` (starting state, stage order, commands) and `starting-state.json`.",
        "",
    ]
    (destination / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")

    checksums = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "CHECKSUMS.sha256":
            checksums[path.relative_to(destination).as_posix()] = sha256_file(path)

    manifest = {
        "schema_version": 2,
        "package_version": "line-2026-09-27",
        "build_date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "model": {"id": args.model_id, "format": contract.get("model", {}).get("format"), "files": model_files},
        "target": {
            "board": facts.board_id,
            "part": bitstream_evidence.get("part"),
            "top": bitstream_evidence.get("top"),
        },
        "deployment": deployment,
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "resources": {"used": impl_evidence.get("resources"),
                      "resource_gate": impl_evidence.get("resource_gate")},
        "timing": impl_evidence.get("timing"),
        "drc": drc,
        "validation": {
            "hls_csim": csim_evidence.get("status"),
            "hls_csynth": json.loads((evidence_dir / "csynth-evidence.json").read_text(encoding="utf-8")).get("status"),
            "rtl_cosim": cosim_status,
            "board_sim": board_evidence.get("status"),
            "board_inference": "NOT_RUN",
            "verification_scope": (report.get("io_scope") or {}).get("verification_scope"),
            "hardware_verified": False,
            "programmed": False,
            "notes": ("a bitstream is not hardware verification: nothing was programmed and no on-board "
                      "inference was run. The packager recomputed both numerical gates from the raw logs "
                      "before assembling this package."),
        },
        "gates": {
            "criterion": csim_gate.get("criterion"),
            "hls_cpp_vs_python": {k: csim_gate.get(k) for k in
                                  ("passed", "max_abs_error", "max_error_over_tolerance",
                                   "reference_source", "actual_source")},
            "rtl_vs_hls_cpp": {k: board_gate.get(k) for k in
                               ("passed", "max_abs_error", "max_error_over_tolerance",
                                "reference_source", "actual_source", "bit_exact_on_the_grid")},
            "recomputed_by_packager": {
                "hls_cpp_vs_python": report["recomputed_gates"].get("hls_cpp_vs_python", {}).get("passed"),
                "rtl_vs_hls_cpp": report["recomputed_gates"].get("rtl_vs_hls_cpp", {}).get("passed"),
            },
            "evidence_statuses": report["stages"],
        },
        "checksums": checksums,
    }
    (destination / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (destination / "CHECKSUMS.sha256").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="utf-8"
    )

    print(json.dumps({
        "model": args.model_id,
        "packaged": True,
        "destination": str(destination),
        "bitstream_sha256": report["bitstream"]["sha256"],
        "files_checksummed": len(checksums),
        "stages": report["stages"],
        "recomputed_gates": {
            key: {"passed": value.get("passed"),
                  "violations": value.get("values", {}).get("violations"),
                  "max_error_over_tolerance": value.get("max_error_over_tolerance")}
            for key, value in report["recomputed_gates"].items()
        },
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
