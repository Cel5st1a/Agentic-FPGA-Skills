"""Run the standard UART deployment commands for one bootstrapped attempt."""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path

from hls_artifacts import verify_hls
from run_line import (
    DEFAULT_SKILLS,
    load_yaml,
    resolve_interpreter,
    sha256_file,
    stage_verdict,
)


def run_stage(stage, command, run, log_dir, handlers):
    log = log_dir / f"{stage}.log"
    step = {"stage": stage, "command": command, "returncode": None}
    start = time.monotonic()
    try:
        if handlers:
            step["execution"] = "shared board reservation"
            with (
                log.open("x", encoding="utf-8") as output,
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                try:
                    step["returncode"] = handlers[stage](command[2:])
                except Exception:
                    traceback.print_exc()
                    step["returncode"] = 1
                except BaseException:
                    traceback.print_exc()
                    raise
        else:
            with log.open("xb") as output:
                result = subprocess.run(command, cwd=run, stdout=output, stderr=subprocess.STDOUT)
            step["returncode"] = result.returncode
    finally:
        step["elapsed_seconds"] = time.monotonic() - start
        with (log_dir / "steps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(step) + "\n")
    if step["returncode"]:
        print(f"{stage} failed. See {log}", flush=True)
    return step["returncode"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILLS)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--reuse-hls", action="store_true")
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--build-only", action="store_true")
    phases.add_argument("--verify-only", action="store_true")
    parser.add_argument("--cable-serial")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reserved-verify", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.reserved_verify and (args.build_only or args.dry_run):
        parser.error("internal verification requires a live capture phase")
    if not args.build_only and not args.port:
        parser.error("--port is required for UART verification")
    if args.verify_only and args.reuse_hls:
        parser.error("--reuse-hls is only relevant for building")
    deployment, run, skills = (p.resolve() for p in (args.deployment, args.run, args.skills))
    if not args.dry_run and (
        deployment.parents[2] != run or not (run / "starting-state.json").is_file()
    ):
        parser.error("use the contract inside a bootstrapped run")
    if args.reuse_hls and not (args.dry_run or args.reserved_verify):
        try:
            verify_hls(run)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(f"HLS artifact validation failed: {exc}")
        for stage in ("convert", "csim", "csynth"):
            record = run / "evidence" / f"{stage}-evidence.json"
            problems = stage_verdict(stage, run)
            if problems or not record.is_file():
                parser.error(f"HLS evidence for {stage} is incomplete: {problems}")
            evidence = json.loads(record.read_text(encoding="utf-8"))
            if evidence.get("contract_sha256") != sha256_file(deployment):
                parser.error(f"HLS evidence for {stage} does not match the deployment")
    contract = load_yaml(deployment)
    model = (deployment.parent / contract["model"]["path"]).resolve()
    python = args.python or resolve_interpreter(contract, model)
    python = python.resolve()
    host = skills / "board-host-io/scripts"
    board = run / "p3-build/host-board"
    audit = run / "evidence/host-port-audit"
    bit = board / "board_top.bit"
    scope = audit / "io-scope.json"
    programming = run / "hardware-program"
    verification = run / "hardware-validation"
    program = [
        host / "program_jtag.py",
        "--bitstream",
        bit,
        "--io-scope",
        scope,
        "--bitstream-evidence",
        run / "evidence/host-vivado-bitstream-evidence.json",
        "--output",
        programming,
    ]
    if args.cable_serial:
        program.extend(["--cable-serial", args.cable_serial])
    commands = [
        (
            "frontend",
            [
                skills / "scripts/line/run_line.py",
                "--deployment",
                deployment,
                "--run",
                run,
                "--python",
                python,
                "--skills",
                skills,
                "--stages",
                "wrapper" if args.reuse_hls else "convert,csynth,wrapper",
            ],
        ),
        (
            "host-wrapper",
            [
                host / "make_host_board.py",
                "--source-wrapper",
                run / "p3-build/board/wrapper.json",
                "--output",
                board,
            ],
        ),
        (
            "host-build",
            [
                host / "build_host.py",
                "--deployment",
                deployment,
                "--board",
                board,
                "--python",
                python,
            ],
        ),
        (
            "port-audit",
            [
                host / "audit_ports.py",
                "--routed-dcp",
                board / "routed.dcp",
                "--wrapper",
                board / "wrapper.json",
                "--output",
                audit,
            ],
        ),
        ("program", program),
        (
            "capture",
            [
                host / "verify_board.py",
                "--deployment",
                deployment,
                "--board",
                board,
                "--bitstream",
                bit,
                "--io-scope",
                scope,
                "--program-summary",
                programming / "summary.json",
                "--port",
                args.port,
                "--output",
                verification,
            ],
        ),
    ]
    log_dir = run / "logs/uart-deployment"
    if args.build_only:
        commands = commands[:4]
        log_dir = log_dir.with_name("uart-deployment-build")
    elif args.verify_only:
        commands = commands[4:]
        log_dir = log_dir.with_name("uart-deployment-verify")
    if args.reserved_verify:
        commands = [row for row in commands if row[0] in {"program", "capture"}]
        os.chdir(run)
        sys.path.insert(0, str(host))
        import program_jtag

        sys.path.insert(0, str(host))
        import verify_board
    elif not args.dry_run:
        log_dir.mkdir(parents=True, exist_ok=False)

    with ExitStack() as reservation:
        handlers = (
            {
                "program": lambda argv: program_jtag.main(argv, reservation=reservation),
                "capture": verify_board.main,
            }
            if args.reserved_verify
            else {}
        )
        for stage, arguments in commands:
            if stage == "program" and not (args.dry_run or args.reserved_verify):
                # Both hardware stages share one process, using the selected interpreter.
                worker = [
                    str(python),
                    str(Path(__file__).resolve()),
                    *sys.argv[1:],
                    "--reserved-verify",
                ]
                # Keep the child alive on interruption so XSDB cleanup can finish under its lock.
                with subprocess.Popen(worker) as process:
                    try:
                        return process.wait()
                    except KeyboardInterrupt:
                        process.wait()
                        raise
            command = list(map(str, [python, *arguments]))
            print(f"{stage} {subprocess.list2cmdline(command)}", flush=True)
            if not args.dry_run:
                result = run_stage(stage, command, run, log_dir, handlers)
                if result:
                    return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
