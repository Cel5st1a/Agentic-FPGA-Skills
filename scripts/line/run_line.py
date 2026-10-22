"""Drive the skill line for one deployment contract, stage by stage.

This is the executable form of the stage order the skills document:

    P2  hls4ml-convert          convert   -> HLS C++ project
    P3  vitis-build-agent       csim      -> numeric gate (HLS C++ vs python reference)
                                csynth    -> HLS resource estimate
                                cosim     -> optional, recorded honestly if it does not finish
        vivado-manager          wrapper   -> board-legal top + sample ROM + testbench
                                boardsim  -> synthesised RTL vs HLS C++ (per-value gate)
                                synth/impl/bitstream -> routed gates + bitstream
        board-host-io           ports     -> routed top-port audit and I/O scope
    P5  bitstream-package       package   -> refuses any build whose gates or port audit did not PASS

Everything is read from the contract: model file(s), input shape, precision, reuse
factor, strategy, io kind, clock period, board profile, sample file, resource limit.
Nothing about a particular model is written into this file, and no stage is skipped
silently: a stage that was not run stays NOT_RUN in the evidence.

Usage
-----
python run_line.py --deployment <deployment.yaml> --run <run-dir> \
    [--stages convert,csim,csynth,wrapper,boardsim,synth,impl,bitstream,ports,package] \
    [--model-id <id>] [--release-root <dir>] [--stop-after <stage>] [--python <interp>] \
    [--skills <skills-root>] [--force-lock]

Examples
--------
python run_line.py --deployment <run>/contract/<pkg>/deployment.yaml --run <run> --stop-after csim
python run_line.py --deployment <run>/contract/<pkg>/deployment.yaml --run <run> --stages wrapper,boardsim
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

try:
    from .identity import identity_fields
except ImportError:  # direct script entry point
    from identity import identity_fields

DEFAULT_SKILLS = Path(__file__).resolve().parents[2]
# The conversion environment depends on the frontend, and that is a property of the
# model format, not of a particular model:
#   * Keras / qKeras frontends: the hls4ml conversion env (TF 2.13.1, hls4ml 1.3.0).
#   * ONNX frontend: the workspace env, which additionally has onnxruntime + qonnx, and
#     qonnx is what can execute a channels-last custom-domain graph to build the Python
#     reference. Without it the conversion succeeds but the reference cannot be written.
DEFAULT_PYTHON_KERAS = Path(r"D:\python_envs\hls4ml_conv\Scripts\python.exe")
DEFAULT_PYTHON_ONNX = Path(r"D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe")
#: A Keras 3 environment. Needed because the archive's *container kind* does not determine
#: its Keras major version: `keras_archive` covers both, and a Keras 3 `.keras` container
#: cannot be deserialised by Keras 2.13 at all. This env carries TF 2.21 / Keras 3.12 /
#: hls4ml 1.3.0, so it can run the Keras 3 path and the later stages alike.
DEFAULT_PYTHON_KERAS3 = Path(r"D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe")
FRONTEND_PYTHON = {
    "keras_json_weights": DEFAULT_PYTHON_KERAS,
    "qkeras_json_weights": DEFAULT_PYTHON_KERAS,
    "keras_h5": DEFAULT_PYTHON_KERAS,
    "keras_archive": DEFAULT_PYTHON_KERAS,
    "onnx": DEFAULT_PYTHON_ONNX,
}
DEFAULT_PYTHON = DEFAULT_PYTHON_KERAS
#: Frontends whose model file is a `.keras` zip container that may state its own Keras
#: version. Only for these is the archive's metadata consulted.
ARCHIVE_FRONTENDS = {"keras_archive", "keras_h5"}
DEFAULT_RELEASE_ROOT = Path(r"D:\ML_models")
STAGE_ORDER = (
    "convert",
    "csim",
    "csynth",
    "wrapper",
    "boardsim",
    "synth",
    "impl",
    "bitstream",
    "ports",
    "package",
)
# Explicit diagnostics remain selectable, outside the recommended/default flow.
SUPPORTED_STAGE_ORDER = STAGE_ORDER[:3] + ("cosim",) + STAGE_ORDER[3:]
EDA_STAGES = {"csim", "csynth", "cosim", "boardsim", "synth", "impl", "bitstream", "ports"}
LOCK_PATH = Path(os.environ.get("TEMP", ".")) / "fpga_skill_line_eda.lock"


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def detect_keras_major(model_path: Path | None) -> int | None:
    """The Keras major version a `.keras` container declares, or None if it says nothing.

    A Keras 3 container stores `metadata.json` with `keras_version`; older ones either omit
    the file or record a 2.x version. Failure to read anything is not an error -- it means
    "unknown", and the caller keeps the frontend's default so that an unrecognised file can
    never be moved onto a different interpreter than the one it was verified with.
    """
    if model_path is None:
        return None
    path = Path(model_path)
    if not path.is_file() or not zipfile.is_zipfile(path):
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            if "metadata.json" not in archive.namelist():
                return None
            metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    version = str(metadata.get("keras_version", "")).strip()
    if not version or not version[0].isdigit():
        return None
    return int(version.split(".")[0])


def resolve_interpreter(contract: dict, model_path: Path | None) -> Path:
    """The interpreter the conversion stages must run under, for this contract.

    Chosen from the frontend, then refined by the model file's own declared Keras version.
    The refinement is deliberately one-directional: only a container that positively says
    "Keras 3" is routed to the Keras 3 environment. Everything else -- a legacy `.h5`, a
    JSON graph, an unreadable or missing file -- keeps the frontend mapping unchanged, so
    no contract that is already verified can be silently re-pointed at another interpreter.
    """
    frontend = (contract.get("model") or {}).get("frontend")
    if frontend not in FRONTEND_PYTHON:
        raise SystemExit(
            f"no default interpreter for frontend {frontend!r}; pass --python. "
            f"Known frontends: {sorted(FRONTEND_PYTHON)}"
        )
    default = FRONTEND_PYTHON[frontend]
    if frontend in ARCHIVE_FRONTENDS and detect_keras_major(model_path) == 3:
        return DEFAULT_PYTHON_KERAS3
    return default


def board_pin_profile(contract: dict, contract_path: Path, skills: Path) -> Path:
    """The profile the wrapper reads pins from: the skills' copy for that board id.

    The contract's own board profile declares the part and the clock pins; the skills'
    board profile (board-profiles/<ID>/board_profile.yaml) additionally carries the
    board-facing pins with provenance. They are matched by id / part, never guessed.
    """
    value = contract.get("board_profile")
    if not value:
        raise SystemExit("contract has no board_profile")
    contract_profile_path = (contract_path.parent / value).resolve()
    contract_profile = load_yaml(contract_profile_path)
    ident = str(contract_profile.get("id") or contract_profile.get("board_id") or "").lower()
    part = str(contract_profile.get("part") or (contract_profile.get("fpga") or {}).get("part") or "")

    candidates = []
    for profile in sorted((skills / "board-profiles").rglob("board_profile.yaml")):
        data = load_yaml(profile)
        profile_id = str(data.get("board_id") or data.get("id") or "").lower()
        profile_part = str(data.get("part") or (data.get("fpga") or {}).get("part") or "")
        if (ident and profile_id and ident == profile_id) or (part and part == profile_part):
            candidates.append(profile)
        elif (ident and profile_id and ident in profile_id) or (profile_id and ident and profile_id in ident):
            candidates.append(profile)
    if not candidates:
        raise SystemExit(
            f"no board profile under {skills / 'board-profiles'} matches id={ident!r} part={part!r} "
            f"(contract profile: {contract_profile_path})"
        )
    if len(candidates) > 1:
        raise SystemExit(f"board profile lookup is ambiguous: {[str(p) for p in candidates]}")
    return candidates[0]


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid is still running.

    Without this the lock outlives a killed run and blocks every later run until someone
    deletes the file by hand - which happened after an interrupted synthesis.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            # OpenProcess also succeeds for a pid that has exited but is not yet reaped, so
            # the exit code decides: anything but STILL_ACTIVE means the holder is gone.
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Lock:
    """One EDA job at a time on this machine. Not a distributed lock, by design."""

    def __init__(self, force: bool) -> None:
        self.force = force
        self.acquired = False
        self.token = ""

    def __enter__(self) -> "Lock":
        if LOCK_PATH.exists():
            raw = LOCK_PATH.read_text(encoding="utf-8", errors="replace")
            holder = {}
            try:
                holder = json.loads(raw)
            except ValueError:
                pass
            pid = holder.get("pid")
            if pid and not _pid_alive(int(pid)):
                print(f"[run_line] clearing a stale lock (pid {pid} is gone): {raw.strip()}", flush=True)
                LOCK_PATH.unlink()
            elif not self.force:
                raise SystemExit(
                    f"another line run holds the EDA lock {LOCK_PATH}:\n{raw}\n"
                    "wait for it, or pass --force-lock if you know it is stale."
                )
            else:
                print(f"[run_line] overriding a live lock: {raw.strip()}", flush=True)
        self.token = secrets.token_hex(32)
        LOCK_PATH.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "delegation_sha256": hashlib.sha256(self.token.encode("ascii")).hexdigest(),
                    "started": datetime.now().isoformat(timespec="seconds"),
                    "argv": sys.argv,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.acquired = True
        return self

    def child_environment(self) -> dict[str, str]:
        """Delegate this acquisition explicitly; OS ancestry is not an identity."""
        if not self.acquired:
            raise ValueError("EDA lock delegation requires an acquired lock")
        return {**os.environ, "AFD_LINE_LOCK_PID": str(os.getpid()),
                "AFD_LINE_LOCK_TOKEN": self.token}

    def __exit__(self, *_exc) -> None:
        if self.acquired and LOCK_PATH.exists():
            try:
                holder = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
                if (holder.get("pid") == os.getpid() and
                    holder.get("delegation_sha256") == hashlib.sha256(self.token.encode("ascii")).hexdigest()):
                    LOCK_PATH.unlink()
            except (OSError, ValueError):
                pass
        self.acquired = False
        self.token = ""


def require_delegated_lock(path: Path) -> None:
    """Validate a live, specific acquisition, not an unreliable parent PID.

    A local cooperation protocol, not a sandbox against lock-file writers.
    The raw credential is passed only in the child's environment, never logged.
    """
    holder = None
    owner = os.environ.get("AFD_LINE_LOCK_PID")
    token = os.environ.get("AFD_LINE_LOCK_TOKEN", "")

    def refuse(reason):
        pid = holder.get("pid") if isinstance(holder, dict) else None
        raise ValueError(f"EDA lock delegation refused: {reason}; lock={path}; "
                         f"holder_pid={pid!r}; delegated_pid={owner!r}; "
                         f"child_pid={os.getpid()}; child_ppid={os.getppid()}")

    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        refuse("missing or unreadable lock record")
    if not isinstance(holder, dict):
        refuse("invalid lock record")
    pid = holder.get("pid")
    if type(pid) is not int or pid <= 0 or owner != str(pid):
        refuse("holder PID does not match explicit delegation")
    digest = holder.get("delegation_sha256")
    if (not token or not isinstance(digest, str) or len(digest) != 64 or
        any(c not in "0123456789abcdef" for c in digest) or
        not secrets.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), digest)):
        refuse("missing, mismatched or expired acquisition credential")
    if not _pid_alive(pid):
        refuse("lock holder is not alive")


def run(cmd: list[str], label: str, *, env=None) -> int:
    print(f"\n[run_line] {label}\n[run_line] $ {' '.join(str(c) for c in cmd)}", flush=True)
    started = time.monotonic()
    completed = subprocess.run([str(c) for c in cmd], env=env)
    print(f"[run_line] {label} exit={completed.returncode} in {time.monotonic() - started:.1f}s", flush=True)
    return completed.returncode


def stage_verdict(stage: str, run_dir: Path) -> list[str]:
    """Problems with a stage, judged from its own evidence, not from its exit code.

    A tool exit code is not a numerical verdict, and a stage can exit 0 while recording
    FAILED (the board simulation did exactly that). Conversely `cosim` may legitimately
    be NOT_RUN: the line records it honestly instead of claiming RTL evidence it does
    not have.
    """
    problems: list[str] = []
    evidence = run_dir / "evidence"
    board = run_dir / "p3-build" / "board"
    checks = {
        "csim": evidence / "csim-evidence.json",
        "csynth": evidence / "csynth-evidence.json",
        "boardsim": evidence / "board-sim-evidence.json",
        "synth": evidence / "vivado-synth-evidence.json",
        "impl": evidence / "vivado-impl-evidence.json",
        "bitstream": evidence / "vivado-bitstream-evidence.json",
    }
    if stage in checks:
        path = checks[stage]
        if not path.is_file():
            return [f"{path.name} was not written"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = payload.get("status")
        if status != "PASSED":
            problems.append(f"{path.name} status={status} reasons={payload.get('reasons')}")
        for key in ("numerical_gate", "board_gate"):
            gate = payload.get(key)
            if isinstance(gate, dict) and gate.get("passed") is not True:
                problems.append(f"{path.name}: {key}.passed is {gate.get('passed')!r}")
        # A numeric stage that recorded no comparison at all is not a passing stage: the
        # csim gate is the whole point of the stage.
        if stage == "csim" and not isinstance(payload.get("numerical_gate"), dict):
            problems.append(f"{path.name}: no numerical_gate, so nothing was compared")
        if stage == "boardsim" and not isinstance(payload.get("board_gate"), dict):
            problems.append(f"{path.name}: no board_gate, so nothing was compared")
        # A design that misses timing is not a passing stage even though the tool exited 0.
        if stage in {"impl", "bitstream"}:
            timing = payload.get("timing") or {}
            if not timing:
                problems.append(f"{path.name}: no timing data, so timing could not be checked")
            elif timing.get("met") is not True:
                problems.append(f"{path.name}: timing not met (WNS={timing.get('wns_ns')})")
    elif stage == "cosim":
        path = evidence / "cosim-evidence.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "FAILED":
                problems.append(f"cosim-evidence.json status=FAILED reasons={payload.get('reasons')}")
        else:
            problems.append("cosim-evidence.json was not written")
    elif stage == "convert":
        path = evidence / "convert-evidence.json"
        if not path.is_file():
            problems.append("convert-evidence.json was not written")
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "FAILED":
                problems.append(f"convert-evidence.json status=FAILED reasons={payload.get('reasons')}")
            elif not payload.get("hls4ml_config_path"):
                problems.append("convert-evidence.json records no generated hls4ml config")
            elif not (run_dir / "p2-convert" / "project" / "build_prj.tcl").is_file():
                problems.append("the converted project has no build_prj.tcl")
    elif stage == "wrapper":
        if not (board / "wrapper.json").is_file():
            problems.append("wrapper.json was not written")
    elif stage == "ports":
        path = evidence / "io-port-audit" / "io-scope.json"
        if not path.is_file():
            problems.append("routed io-scope.json was not written")
        else:
            scope = json.loads(path.read_text(encoding="utf-8"))
            checkpoint, wrapper = board / "routed.dcp", board / "wrapper.json"
            if not checkpoint.is_file() or scope.get("checkpoint_sha256") != sha256_file(checkpoint):
                problems.append("io-scope checkpoint hash differs from routed.dcp")
            if not wrapper.is_file() or scope.get("wrapper_sha256") != sha256_file(wrapper):
                problems.append("io-scope wrapper hash differs from wrapper.json")
            if scope.get("wrapper_ports_match") is not True:
                problems.append("routed ports disagree with wrapper.json")
    elif stage == "package":
        if not (evidence / "convert-evidence.json").is_file():
            problems.append("package step ran but the run has no evidence")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--stages", default=",".join(STAGE_ORDER))
    parser.add_argument("--stop-after", choices=SUPPORTED_STAGE_ORDER, default=None)
    parser.add_argument("--model-id", default=None, help="defaults to the contract's model path stem")
    parser.add_argument("--release-root", type=Path, default=DEFAULT_RELEASE_ROOT)
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILLS)
    parser.add_argument("--python", type=Path, default=None,
                        help="interpreter for the skills' scripts; defaults per model frontend "
                             f"({', '.join(f'{k} -> {v.name}' for k, v in FRONTEND_PYTHON.items())})")
    parser.add_argument("--timeout-csynth", type=int, default=7200)
    parser.add_argument("--timeout-cosim", type=int, default=300,
                        help="explicit diagnostic cosim budget; boardsim is the RTL numerical gate")
    parser.add_argument("--force-lock", action="store_true")
    parser.add_argument("--reuse", action="store_true",
                        help="allow regenerating artifacts that already exist in the run dir")
    parser.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    args = parser.parse_args()

    contract_path = args.deployment.resolve()
    run_dir = args.run.resolve()
    skills = args.skills.resolve()
    contract = load_yaml(contract_path)
    if not args.dry_run:
        # --dry-run prints commands and touches nothing, not even an empty run tree.
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "evidence").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in SUPPORTED_STAGE_ORDER]
    if unknown:
        raise SystemExit(f"unknown stages: {unknown}; known: {list(SUPPORTED_STAGE_ORDER)}")
    stages = [s for s in SUPPORTED_STAGE_ORDER if s in stages]
    if args.stop_after:
        stages = [s for s in stages if SUPPORTED_STAGE_ORDER.index(s) <= SUPPORTED_STAGE_ORDER.index(args.stop_after)]
    if not stages:
        raise SystemExit("no stages selected")

    project = run_dir / "p2-convert" / "project"
    evidence = run_dir / "evidence"
    board = run_dir / "p3-build" / "board"
    # The registry id is the model directory the run lives under: runs/<model>/<run-id>.
    model_id = args.model_id or run_dir.parent.name
    verilog_dir = project / "myproject_prj" / "solution1" / "syn" / "verilog"
    frontend = contract["model"].get("frontend")
    if args.python is None:
        # Resolved from the frontend AND the model file's own declared Keras version: a
        # Keras 3 `.keras` archive must not be handed to the Keras 2.13 interpreter, which
        # cannot deserialise it (defect D-N01). `--python` still overrides everything.
        model_path = None
        if contract["model"].get("path"):
            model_path = (contract_path.parent / contract["model"]["path"]).resolve()
        args.python = resolve_interpreter(contract, model_path)
        if not Path(args.python).is_file():
            raise SystemExit(f"the {frontend} conversion environment is missing: {args.python}")
        keras_major = detect_keras_major(model_path)
        print(json.dumps({
            "interpreter_resolved": str(args.python),
            "frontend": frontend,
            "model_keras_major": keras_major,
            "reason": ("archive declares Keras 3; the legacy env cannot deserialise it"
                       if keras_major == 3 else
                       "frontend default (no Keras 3 declaration in the model file)"),
        }, indent=2), flush=True)

    def converter() -> Path:
        if frontend in {"keras_json_weights", "qkeras_json_weights", "keras_h5", "keras_archive"}:
            return skills / "hls4ml-convert" / "scripts" / "convert_keras.py"
        if frontend == "onnx":
            return skills / "hls4ml-convert" / "scripts" / "convert_onnx.py"
        if frontend in {"pytorch", "torch"}:
            # There is no PyTorch converter in this tree, and `hls4ml-convert/SKILL.md` used
            # to advertise one that does not exist (`scripts/convert_pytorch.py`). Failing
            # here -- before a run directory is spent -- is the honest behaviour; returning a
            # missing path would surface much later as an unexplained tool error.
            raise SystemExit(
                "no PyTorch converter exists in this tree: hls4ml-convert/scripts/ holds only "
                "convert_keras.py and convert_onnx.py. Export the model and use the ONNX "
                "frontend, or add the converter deliberately."
            )
        raise SystemExit(f"no converter for frontend {frontend!r}")

    def top_name() -> str:
        if not verilog_dir.is_dir():
            raise SystemExit(f"generated verilog not found: {verilog_dir}")
        files = sorted(verilog_dir.glob("*.v"))
        if not files:
            raise SystemExit(f"no .v files in {verilog_dir}")
        for candidate in files:
            if candidate.stem == "myproject":
                return candidate.stem
        if len(files) != 1:
            raise SystemExit(f"cannot pick the top module among {[f.name for f in files]}")
        return files[0].stem

    def wrapper_command() -> list[str]:
        command = [args.python, skills / "vivado-manager" / "scripts" / "make_board_wrapper.py",
                   "--deployment", contract_path, "--verilog-dir", verilog_dir, "--top", top_name(),
                   "--output", board, "--board-profile", pins_profile, "--force"]
        command += ["--reference-json", evidence / "python-output.json"]
        return command
    pins_profile = board_pin_profile(contract, contract_path, skills)
    inputs_json = (contract_path.parent / contract["verification"]["inputs"]).resolve()
    reference_json = evidence / "python-output.json"
    csim_log = run_dir / "p3-build" / "csim" / "csim_results.log"

    def build_plan_entry(stage: str) -> list[str]:
        if stage == "convert":
            return [args.python, converter(), "--deployment", contract_path, "--output", project,
                    "--inputs", inputs_json, "--reference-out", reference_json,
                    "--evidence", evidence / "convert-evidence.json"]
        if stage in {"csim", "csynth", "cosim"}:
            command = [
                args.python, skills / "vitis-build-agent" / "scripts" / "run_stage.py",
                "--stage", stage, "--project", project, "--work", run_dir / "p3-build" / stage,
                "--deployment", contract_path, "--evidence", evidence / f"{stage}-evidence.json",
            ]
            if stage in {"csim", "cosim"}:
                command += ["--reference", reference_json]
            return command + ["--timeout", args.timeout_cosim if stage == "cosim" else args.timeout_csynth]
        if stage == "wrapper":
            # --force regenerates only the wrapper's own outputs in place. The driver's
            # stale-artifact guard (see below) is what decides whether re-running at all
            # is allowed, so this does not silently replace RTL behind a bitstream.
            # The top module and the stream sizes are discovered from the Verilog that
            # csynth generated, so they are resolved when the stage runs.
            return wrapper_command()
        if stage == "boardsim":
            return [args.python, skills / "vivado-manager" / "scripts" / "run_board_sim.py",
                    "--board", board, "--deployment", contract_path,
                    "--reference", csim_log, "--work", board,
                    "--evidence", evidence / "board-sim-evidence.json"]
        if stage in {"synth", "impl", "bitstream"}:
            return [args.python, skills / "vivado-manager" / "scripts" / "run_vivado.py",
                    "--stage", stage, "--board", board, "--deployment", contract_path,
                    "--work", board, "--evidence", evidence / f"vivado-{stage}-evidence.json"]
        if stage == "ports":
            return [args.python, skills / "board-host-io" / "scripts" / "audit_ports.py",
                    "--routed-dcp", board / "routed.dcp", "--wrapper", board / "wrapper.json",
                    "--output", evidence / "io-port-audit", "--allow-configure-only",
                    "--under-line-lock"]
        if stage == "package":
            # --force replaces an existing <release-root>/<model-id>. The driver only gets
            # here when every stage's evidence passed, and the packager re-verifies the
            # gates, the artifacts and the bitstream before writing anything, so replacing
            # a previous delivery is the intended behaviour.
            return [args.python, skills / "bitstream-package" / "scripts" / "package_bitstream.py",
                    "--run", run_dir, "--model-id", model_id, "--release-root", args.release_root,
                    "--force"]
        raise SystemExit(f"no command for stage {stage!r}")

    # Each entry is a thunk: the wrapper stage needs the Verilog that csynth generates, so
    # commands are resolved when the stage runs, not when the plan is assembled.
    plan: list[tuple[str, object]] = [(stage, (lambda s=stage: build_plan_entry(s))) for stage in stages]

    print(json.dumps({"run": str(run_dir), "contract": str(contract_path), "model_id": model_id,
                      "stages": stages, "board_pins_profile": str(pins_profile),
                      "python": str(args.python)}, indent=2, ensure_ascii=False), flush=True)
    if args.dry_run:
        for stage, builder in plan:
            try:
                command = builder()
            except SystemExit as exc:
                command = [f"<unavailable at plan time: {exc}>"]
            print(f"\n[dry-run] {stage}: {' '.join(str(c) for c in command)}")
        return 0

    # A stage must not silently overwrite generated artifacts: a bitstream that no longer
    # matches its RTL is worse than no bitstream. Iterations get a new run directory.
    artefacts = {
        "convert": project / "hls4ml_config.yml",
        "csim": run_dir / "p3-build" / "csim" / "csim_results.log",
        "csynth": evidence / "csynth-evidence.json",
        "cosim": evidence / "cosim-evidence.json",
        "wrapper": board / "wrapper.json",
        "boardsim": evidence / "board-sim-evidence.json",
        "synth": board / "synth.dcp",
        "impl": board / "routed.dcp",
        "bitstream": board / "board_top.bit",
        "ports": evidence / "io-port-audit" / "io-scope.json",
    }
    existing = [stage for stage in stages if artefacts.get(stage) and artefacts[stage].exists()]
    if existing and not args.reuse:
        raise SystemExit(
            f"refusing to overwrite existing artefacts for stages {existing} in {run_dir}. "
            "Use a new run directory (bootstrap_run.py materialises an identical starting "
            "state), or pass --reuse if you intend to regenerate in place."
        )

    identity = identity_fields(contract_path, pins_profile)
    for frozen_path in (run_dir / "starting-state.json", run_dir / "run-plan.json"):
        if frozen_path.is_file():
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            for key in ("contract_sha256", "board_profile_sha256", "board_pins_profile_sha256"):
                if key in frozen and frozen[key] != identity[key]:
                    raise SystemExit(f"{key} changed since {frozen_path}; use a new run")
    plan_record = {**identity, "skills_root": str(skills), "stages": stages,
                   "python": str(args.python)}
    (run_dir / "run-plan.json").write_text(
        json.dumps(plan_record, indent=2, ensure_ascii=False), encoding="utf-8")

    needs_eda = any(stage in EDA_STAGES for stage in stages)
    results: list[dict] = []
    context = Lock(args.force_lock) if needs_eda else None
    if context is None:
        return _execute(plan, results, run_dir, identity=identity)
    with context:
        return _execute(plan, results, run_dir, context, identity=identity)


def _execute(plan, results: list[dict], run_dir: Path, lock: Lock | None = None,
             identity: dict | None = None) -> int:
    for stage, builder in plan:
        command = builder()
        code = (run(command, stage, env=lock.child_environment())
                if stage == "ports" and lock is not None else run(command, stage))
        if identity:
            evidence_names = {"convert": "convert-evidence.json", "csim": "csim-evidence.json",
                "csynth": "csynth-evidence.json", "cosim": "cosim-evidence.json",
                "boardsim": "board-sim-evidence.json", "synth": "vivado-synth-evidence.json",
                "impl": "vivado-impl-evidence.json", "bitstream": "vivado-bitstream-evidence.json"}
            name = evidence_names.get(stage)
            path = run_dir / "evidence" / name if name else None
            if path and path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                for key in ("contract_sha256", "board_profile_sha256", "board_pins_profile_sha256"):
                    if key in payload and payload[key] != identity[key]:
                        raise ValueError(f"{path}: {key} differs from this run's identity")
                payload.update(identity)
                path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        problems = stage_verdict(stage, run_dir)
        results.append({"stage": stage, "exit_code": code, "problems": problems})
        if code != 0 or problems:
            print(json.dumps({"stopped_at": stage, "results": results}, indent=2), flush=True)
            return code or 1
    print(json.dumps({"results": results}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
