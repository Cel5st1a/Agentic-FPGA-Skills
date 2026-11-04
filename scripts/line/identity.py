"""Independent hashes for the deployment, its board file and the pin facts."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml


def identity_fields(deployment, pins_profile=None):
    deployment = Path(deployment).resolve()
    contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
    relative = contract.get("board_profile")
    if not relative:
        raise ValueError("contract declares no board_profile; board identity is required")
    board = (deployment.parent / relative).resolve()
    record = {
        "contract_sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
        "board_profile": str(board),
        "board_profile_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
    }
    if pins_profile is not None:
        pins = Path(pins_profile).resolve()
        record.update(
            board_pins_profile=str(pins),
            board_pins_profile_sha256=hashlib.sha256(pins.read_bytes()).hexdigest(),
        )
    return record


CORE_GROUPS = (
    "model_files",
    "contract_arithmetic",
    "arithmetic_config",
    "core_tools",
    "arithmetic_files",
    "core_files",
)
WRAPPER_GROUPS = ("wrapper_files", "wrapper_config", "wrapper_tools")


def capture_reuse_identity(
    deployment,
    *,
    arithmetic_config=None,
    tool_versions=None,
    arithmetic_files=None,
    core_files=None,
    wrapper_files=None,
):
    """Necessary content identity only, not permission to skip work or board proof.

    Arithmetic files are an explicit producing-stage list, not an include discovery.
    Relative dependency paths are resolved against the deployment's directory.
    """
    deployment = Path(deployment).resolve()
    base = deployment.parent
    result = {
        "schema_version": 1,
        "kind": "reuse_identity",
        "fingerprints": {},
        "missing": {"core": [], "wrapper": []},
        "dependency_files": {},
        "hardware_verified": False,
        "scope": "supplied content dependencies only; not numerical validity or automatic reuse",
    }

    def remember(group, value, scope):
        try:
            data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            result["fingerprints"][group] = hashlib.sha256(data.encode()).hexdigest()
        except (TypeError, ValueError, OverflowError):
            result["fingerprints"][group] = None
            result["missing"][scope].append(group + ": invalid canonical content")

    def files(group, mapping, scope):
        entries = {}
        if not isinstance(mapping, dict) or not mapping:
            result["missing"][scope].append(group + ": no file list")
            result["fingerprints"][group] = None
            return
        for label, relative in mapping.items():
            try:
                if not isinstance(label, str) or not label:
                    raise ValueError("invalid logical file label")
                path = Path(relative)
                path = (base / path).resolve()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                entries[label] = digest
                result["dependency_files"][group + ":" + label] = {
                    "path": str(path),
                    "sha256": digest,
                }
            except (OSError, TypeError, ValueError):
                result["missing"][scope].append(group + ": unreadable " + str(label))
                entries[str(label)] = None
        if any(value is None for value in entries.values()):
            result["fingerprints"][group] = None
        else:
            remember(group, entries, scope)

    try:
        contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
        if not isinstance(contract, dict) or not isinstance(contract.get("model"), dict):
            raise ValueError("invalid contract/model mapping")
    except (OSError, ValueError, yaml.YAMLError):
        contract = {"model": {}}
        result["missing"]["core"].append("deployment: unreadable or invalid")
    model = contract["model"]
    associated = model.get("associated_files") or []
    if not isinstance(associated, list):
        result["missing"]["core"].append("model associated_files: not a list")
        associated = []
    models = {"model": model.get("path")}
    models.update({f"associated-{i}": path for i, path in enumerate(associated)})
    files("model_files", models, "core")
    profile_path = contract.get("board_profile")
    try:
        profile = yaml.safe_load((base / profile_path).read_text(encoding="utf-8-sig"))
        part = (
            profile.get("part")
            or profile.get("target_part")
            or (profile.get("fpga") or {}).get("part")
        )
        if not isinstance(part, str) or not part:
            raise ValueError("missing actual part")
    except (OSError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        part = None
        result["missing"]["core"].append("board_profile: unknown target part")
    arithmetic = {
        k: v
        for k, v in contract.items()
        if k not in {"board_profile", "verification", "host_transport", "model"}
    }
    arithmetic["model"] = {k: v for k, v in model.items() if k not in {"path", "associated_files"}}
    arithmetic["target_part"] = part
    remember("contract_arithmetic", arithmetic, "core")
    if not isinstance(arithmetic_config, dict) or not arithmetic_config:
        result["missing"]["core"].append("arithmetic_config: unknown")
    else:
        for field, expected in (
            ("part", part),
            ("clock_period_ns", contract.get("clock_period_ns")),
            ("io_type", contract.get("io_type")),
        ):
            if expected is None or arithmetic_config.get(field) != expected:
                result["missing"]["core"].append("arithmetic_config: unknown/conflicting " + field)
        if (
            not isinstance(arithmetic_config.get("hls_config"), dict)
            or not arithmetic_config["hls_config"]
        ):
            result["missing"]["core"].append("arithmetic_config.hls_config: unknown")
    remember("arithmetic_config", arithmetic_config, "core")
    versions = tool_versions if isinstance(tool_versions, dict) else {}
    for group, names, scope in (
        ("core_tools", ("python", "frontend_runtime", "hls4ml", "hls"), "core"),
        ("wrapper_tools", ("vivado",), "wrapper"),
    ):
        selected = {}
        for name in names:
            version = versions.get(name)
            if (
                not isinstance(version, str)
                or not version.strip()
                or version.strip().lower() in {"unknown", "missing", "not_recorded"}
            ):
                result["missing"][scope].append("tool version: " + name)
                version = None
            selected[name] = version
        # Preserve other explicitly supplied tool facts conservatively in core identity.
        if group == "core_tools":
            for key, value in versions.items():
                if key not in {*names, "vivado"}:
                    if (
                        not isinstance(value, str)
                        or not value.strip()
                        or value.strip().lower() in {"unknown", "missing", "not_recorded"}
                    ):
                        result["missing"][scope].append("tool version: " + str(key))
                        value = None
                    selected[key] = value
        remember(group, selected, scope)
    files("arithmetic_files", arithmetic_files, "core")
    files("core_files", core_files, "core")
    if not isinstance(wrapper_files, dict) or not wrapper_files:
        result["missing"]["wrapper"].append("wrapper_files: no supplied sources")
        wrapper_files = {}
    wrapper_dependencies = {"provided:" + str(k): v for k, v in wrapper_files.items()}
    wrapper_dependencies["board_profile"] = profile_path
    verification = contract.get("verification") or {}
    if not isinstance(verification, dict):
        result["missing"]["wrapper"].append("verification: invalid mapping")
        verification = {}
    wrapper_dependencies["public_inputs"] = verification.get("inputs")
    files("wrapper_files", wrapper_dependencies, "wrapper")
    remember(
        "wrapper_config",
        {
            "host_transport": contract.get("host_transport"),
            "verification": {k: v for k, v in verification.items() if k != "inputs"},
        },
        "wrapper",
    )
    return result


def compare_reuse_identity(previous, current):
    """Compare necessary identities; a match alone never authenticates core validity."""

    def available(snapshot, groups, scope):
        return (
            isinstance(snapshot, dict)
            and snapshot.get("schema_version") == 1
            and snapshot.get("kind") == "reuse_identity"
            and isinstance(snapshot.get("missing"), dict)
            and snapshot["missing"].get(scope) == []
            and isinstance(snapshot.get("fingerprints"), dict)
            and all(
                isinstance(snapshot["fingerprints"].get(g), str)
                and re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprints"][g])
                for g in groups
            )
        )

    core = wrapper = None
    changed = []
    if all(available(s, CORE_GROUPS, "core") for s in (previous, current)):
        changed = [
            g for g in CORE_GROUPS if previous["fingerprints"][g] != current["fingerprints"][g]
        ]
        core = not changed
        if not core:
            wrapper = False
        elif all(available(s, WRAPPER_GROUPS, "wrapper") for s in (previous, current)):
            changed = [
                g
                for g in WRAPPER_GROUPS
                if previous["fingerprints"][g] != current["fingerprints"][g]
            ]
            wrapper = not changed
    downstream = ["wrapper", "board_simulation", "synth", "impl", "bitstream", "board_capture"]
    affected = (
        ["conversion", "core_build", *downstream]
        if core is False
        else downstream
        if wrapper is False
        else []
        if wrapper is True
        else None
    )
    return {
        "core_identity_matches": core,
        "wrapper_identity_matches": wrapper,
        "changed_groups": changed,
        "affected_stages": affected,
        "hardware_verified": False,
        "scope": "necessary content identity only; verify producing evidence before reuse",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Offline dependency identity capture/comparison; no EDA"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--capture", type=Path, help="JSON arguments including deployment and dependency maps"
    )
    mode.add_argument("--compare", type=Path, nargs=2, metavar=("PREVIOUS", "CURRENT"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.capture:
        result = capture_reuse_identity(**json.loads(args.capture.read_text(encoding="utf-8")))
    else:
        result = compare_reuse_identity(
            *(json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
