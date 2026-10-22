"""Read the routed checkpoint's real top ports before a host-I/O claim is made."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
LINE = SKILLS / "scripts" / "line"
sys.path.insert(0, str(LINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_line import Lock, LOCK_PATH, require_delegated_lock  # noqa: E402
import board_facts  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_ports(ports: dict[str, str]) -> dict:
    host = ports.get("uart_rx") == "IN" and ports.get("uart_tx") == "OUT"
    return {
        "top_ports": sorted(ports),
        "host_input_output_available": host,
        "verification_scope": "host-path-present-unverified" if host else "configure-only",
        "missing_for_host_path": [name for name, direction in (("uart_rx", "IN"), ("uart_tx", "OUT"))
                                  if ports.get(name) != direction],
        "on_board_numerical": "NOT_RUN", "programmed": False,
        "hardware_verified": False,
        "note": ("Routed top has UART pins; a bitstream alone does not prove numerical correctness."
                 if host else "Routed bitstream has no complete host input/output path."),
    }


def require_host_path(ports: dict[str, str]) -> None:
    scope = classify_ports(ports)
    if not scope["host_input_output_available"]:
        raise ValueError("routed checkpoint lacks host path: " +
                         ", ".join(scope["missing_for_host_path"]))


def parse_vivado_ports(stdout: str) -> dict[str, dict]:
    ports = {}
    for line in stdout.splitlines():
        if not line.startswith("HOST_IO_PORT|"):
            continue
        fields = line.split("|", 4)
        if len(fields) != 5 or not fields[1]:
            raise ValueError(f"malformed Vivado port record: {line}")
        _, name, direction, pin, standard = fields
        if name in ports:
            raise ValueError(f"duplicate Vivado port: {name}")
        ports[name] = {"direction": direction, "pin": pin, "iostandard": standard}
    if not ports:
        raise ValueError("Vivado reported no routed top ports")
    return ports


def require_parent_lock() -> None:
    require_delegated_lock(LOCK_PATH)


def audit_checkpoint(checkpoint: Path, wrapper_path: Path, output: Path,
                     vivado: Path, require_host: bool = True,
                     under_line_lock: bool = False) -> dict:
    checkpoint, wrapper_path, output = Path(checkpoint), Path(wrapper_path), Path(output)
    if not checkpoint.is_file():
        raise ValueError(f"routed checkpoint missing: {checkpoint}")
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
    script = output / "ports.tcl"
    script_text = f"""open_checkpoint {{{checkpoint.resolve().as_posix()}}}
foreach p [get_ports] {{
  puts "HOST_IO_PORT|[get_property NAME $p]|[get_property DIRECTION $p]|[get_property PACKAGE_PIN $p]|[get_property IOSTANDARD $p]"
}}
close_design
"""
    command = (["cmd", "/c", str(vivado)] if vivado.suffix.lower() in {".bat", ".cmd"}
               else [str(vivado)])
    command += ["-mode", "batch", "-source", str(script), "-nojournal", "-notrace"]
    if under_line_lock:
        require_parent_lock()
    with (nullcontext() if under_line_lock else Lock(False)):
        output.mkdir(parents=True, exist_ok=False)
        script.write_text(script_text, encoding="utf-8")
        result = subprocess.run(command, cwd=output, capture_output=True, text=True,
                                errors="replace", timeout=600)
    (output / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Vivado routed port read failed: exit {result.returncode}")
    details = parse_vivado_ports(result.stdout)
    directions = {name: value["direction"] for name, value in details.items()}
    scope = classify_ports(directions)
    scope.update({"checkpoint": str(checkpoint.resolve()),
                  "checkpoint_sha256": sha256_file(checkpoint),
                  "wrapper": str(wrapper_path.resolve()),
                  "wrapper_sha256": sha256_file(wrapper_path),
                  "ports": details, "vivado_command": command})
    scope["wrapper_ports_match"] = set(details) == set(wrapper.get("top_ports", []))
    # The pins are checked against the profile the *wrapper* was built for, never against a
    # pinout remembered here: a board-facing check that could be pointed at a different
    # profile than the artifact's own would let a mismatched artifact pass as verified.
    facts = board_facts.facts_for_wrapper(wrapper) if require_host else None
    if facts is not None:
        scope["board"] = facts.to_record()
    (output / "io-scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")
    if not scope["wrapper_ports_match"]:
        raise ValueError("routed top ports disagree with wrapper.json")
    if require_host:
        require_host_path(directions)
        board_facts.require_routed_uart(details, facts)
    return scope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routed-dcp", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", type=Path,
                        default=Path(r"D:\vitis\2025.2\Vivado\bin\vivado.bat"))
    parser.add_argument("--allow-configure-only", action="store_true")
    parser.add_argument("--under-line-lock", action="store_true",
                        help="only when run_line.py already holds the EDA lock")
    args = parser.parse_args()
    try:
        scope = audit_checkpoint(args.routed_dcp, args.wrapper, args.output,
                                 args.vivado, require_host=not args.allow_configure_only,
                                 under_line_lock=args.under_line_lock)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired, SystemExit) as exc:
        print(json.dumps({"error": "port_audit_failed", "message": str(exc)},
                         ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(scope, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
