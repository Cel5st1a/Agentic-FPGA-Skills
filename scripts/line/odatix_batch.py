"""Run a same-input EDA candidate batch through Odatix Workflow."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_digest(path):
    with path.open("rb") as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_digest(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"payload directory missing: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"invalid payload entry: {path}")
        if path.is_file():
            files.append([path.relative_to(root).as_posix(), file_digest(path)])
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()


def candidate_key(payload_digest, tool_identity, entrypoint, args, discriminator):
    identity = [payload_digest, tool_identity, entrypoint, args, discriminator]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def plan_candidates(cases, payload_digest, tool_identity, entrypoint):
    seen_ids = set()
    canonical = {}
    plan = []
    for index, case in enumerate(cases, start=1):
        case_id, args = case["id"], case["args"]
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise ValueError(f"invalid or duplicate candidate ID: {case_id!r}")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"candidate arguments must be strings: {case_id}")
        seen_ids.add(case_id)
        discriminator = None
        if (
            not isinstance(tool_identity, str)
            or tool_identity.strip().casefold() in {"", "unknown", "unverified", "unavailable"}
            or case.get("independent")
        ):
            discriminator = index
        key = candidate_key(payload_digest, tool_identity, entrypoint, args, discriminator)
        token, primary_id = canonical.setdefault(key, (f"job{index:04d}", case_id))
        plan.append(
            {
                "id": case_id,
                "args": args,
                "token": token,
                "canonical_id": primary_id,
                "key": key,
                "payload_digest": payload_digest,
                "discriminator": discriminator,
            }
        )
    if not plan:
        raise ValueError("at least one candidate is required")
    return plan


def output_hashes(payload, names):
    hashes = {}
    for name in names:
        path = payload_path(payload, name)
        if not (payload / name).is_symlink() and path.is_file() and path.stat().st_size:
            hashes[name] = file_digest(path)
    return hashes


def collect_results(plan, work, expected=()):
    measured = {}
    for row in plan:
        token = row["token"]
        if token in measured:
            continue
        folder = work / token
        result = {"status": "incomplete", "job_directory": str(folder)}
        try:
            worker = read_json(folder / "candidate-result.json")
            if any(worker[key] != row[key] for key in ("token", "key", "payload_digest")):
                raise ValueError("worker evidence does not match the candidate identity")
            status = worker["status"]
            if status not in {"success", "failed"}:
                raise ValueError(f"unexpected worker status: {status}")
            result.update(
                {
                    key: worker.get(key)
                    for key in (
                        "returncode",
                        "missing_outputs",
                        "seconds",
                        "started_unix",
                        "finished_unix",
                        "command",
                        "output_sha256",
                    )
                }
            )
            if status == "success":
                if worker["returncode"] != 0 or not (folder / "workflow_done").is_file():
                    raise ValueError("worker and Odatix completion do not agree")
                hashes = output_hashes(folder / "payload", expected)
                if len(hashes) != len(expected) or hashes != {
                    k: worker.get("output_sha256", {}).get(k) for k in expected
                }:
                    raise ValueError("worker outputs are missing or no longer match")
            result["status"] = status
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["evidence_error"] = str(exc)
        measured[token] = result
    return [
        {**row, **measured[row["token"]], "reused_within_batch": row["id"] != row["canonical_id"]}
        for row in plan
    ]


def payload_path(root, name):
    path = Path(name)
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root):
        raise ValueError(f"expected a path inside the candidate payload: {name}")
    return resolved


def run_candidate(token):
    root = Path.cwd()
    payload = root / "payload"
    case = read_json(root / "cases" / f"{token}.json")
    before = snapshot_digest(payload)
    if before != case["payload_digest"]:
        raise ValueError("staged candidate payload differs from the frozen inputs")
    computed_key = candidate_key(
        before, case["tool_identity"], case["entrypoint"], case["args"], case["discriminator"]
    )
    if computed_key != case["key"]:
        raise ValueError("effective arguments do not match the candidate identity")
    output_names = [*case["expected"], *case.get("optional_outputs", [])]
    expected = [payload_path(payload, name) for name in output_names]
    if any(path.exists() for path in expected):
        raise ValueError("staged candidate already contains an expected output")
    command = [sys.executable, str(payload_path(payload, case["entrypoint"])), *case["args"]]
    start = time.monotonic()
    started_unix = time.time()
    with (root / "candidate-console.log").open("wb") as console:
        process = subprocess.run(command, cwd=payload, stdout=console, stderr=subprocess.STDOUT)
    finished_unix = time.time()
    hashes = output_hashes(payload, output_names)
    missing = [name for name in case["expected"] if name not in hashes]
    status = "success" if process.returncode == 0 and not missing else "failed"
    write_json(
        root / "candidate-result.json",
        {
            "status": status,
            "returncode": process.returncode,
            "missing_outputs": missing,
            "seconds": round(time.monotonic() - start, 3),
            "started_unix": started_unix,
            "finished_unix": finished_unix,
            "payload_digest": before,
            "token": token,
            "key": computed_key,
            "output_sha256": hashes,
            "command": command,
        },
    )
    return 0 if status == "success" else 1


def dispatch(job_count, jobs=1):
    from odatix.run import Run
    from odatix.workspace import Workspace

    run = Run(Workspace.open(), "workflow", nb_jobs=jobs)
    scope_name = os.environ.get("AFD_ODATIX_JOB_NAME") if os.name == "nt" else None
    session, session_path, session_record = None, None, None
    if scope_name:
        from process_scope import hold_process_identity, process_identity, write_process_proof

        if process_identity(os.getpid(), scope_name) is None:
            raise ValueError("dispatcher is outside its Windows process scope")
        session_name = f"afd-{scope_name.rsplit('_', 1)[-1]}"
        session_path = Path.cwd() / "odatix-session.json"
        session_record = {
            "schema_version": 1,
            "job_name": scope_name,
            "session_name": session_name,
            "workspace": str(Path.cwd()),
            "state": "starting",
        }
        write_process_proof(session_path, session_record)
    with ExitStack() as held:
        try:
            if scope_name:
                run.start(detach=True, session=session_name)
                candidate = run.session
                if candidate is None or candidate.pid is None:
                    raise RuntimeError("Odatix did not record the daemon process")
                identity = held.enter_context(hold_process_identity(candidate.pid, scope_name))
                if (
                    identity is None
                    or candidate.name != session_name
                    or Path(candidate.workspace_root).resolve() != Path.cwd().resolve()
                ):
                    raise RuntimeError("Odatix did not start the expected owned session")
                session = candidate
                session_record.update(state="active", session_id=session.id, daemon=identity)
                write_process_proof(session_path, session_record)
            else:
                run.start(detach=True)
                session = run.session
            jobs = run.wait()
            if len(jobs) != job_count:
                raise RuntimeError(f"Odatix reported {len(jobs)} jobs, expected {job_count}")
            print(json.dumps(jobs, default=str), flush=True)
        finally:
            if session is not None:
                stopped = session.stop()
                if session_record is not None:
                    session_record.update(state="closed" if stopped else "stop_unconfirmed")
                    write_process_proof(session_path, session_record)
                if not stopped:
                    raise RuntimeError("Could not confirm the batch Odatix session stopped")
    return 0


def run_batch(request, payload, output, odatix, *, jobs=1, env=None, lock=None):
    if type(jobs) is not int or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    start = time.monotonic()
    payload, output, odatix = (Path(item).resolve() for item in (payload, output, odatix))
    if output.is_relative_to(payload):
        raise ValueError("batch output must be outside the input payload")
    entrypoint = request["entrypoint"]
    if not payload_path(payload, entrypoint).is_file():
        raise ValueError(f"entry point is missing: {entrypoint}")
    expected = request["expected_outputs"]
    optional = request.get("optional_outputs", [])
    if not expected or any(payload_path(payload, name).exists() for name in (*expected, *optional)):
        raise ValueError("expected outputs must be absent from the frozen input payload")
    digest = snapshot_digest(payload)
    plan = plan_candidates(request["candidates"], digest, request.get("tool_identity"), entrypoint)
    output.mkdir(parents=True)
    write_json(output / "plan.json", plan)
    source = output / "sources"
    copied = source / "payload"
    shutil.copytree(payload, copied)
    if snapshot_digest(copied) != digest:
        raise ValueError("input payload changed while staging the batch")
    cases = source / "cases"
    cases.mkdir()
    shutil.copy2(__file__, source / "odatix_batch.py")

    userconfig = output / "odatix_userconfig"
    workflow = userconfig / "workflows" / "candidates"
    workflow.mkdir(parents=True)
    seen = set()
    for row in plan:
        token = row["token"]
        if token in seen:
            continue
        seen.add(token)
        (workflow / f"{token}.txt").write_text(token + "\n", encoding="utf-8")
        write_json(
            cases / f"{token}.json",
            {
                "entrypoint": entrypoint,
                "args": row["args"],
                "expected": expected,
                "optional_outputs": optional,
                "payload_digest": digest,
                "key": row["key"],
                "tool_identity": request.get("tool_identity"),
                "discriminator": row["discriminator"],
            },
        )

    write_json(output / "odatix.yml", {"workspace": "odatix"})
    write_json(
        userconfig / "workflow_settings.yml",
        {
            "overwrite": False,
            "ask_continue": False,
            "exit_when_done": True,
            "log_size_limit": 300,
            "nb_jobs": jobs,
            "force_single_thread": False,
            "workflows": ["candidates/*"],
        },
    )
    write_json(
        workflow / "_settings.yml",
        {
            "sources": {"path": "sources"},
            "use_parameters": False,
            "tasks": [
                {
                    "name": "main",
                    "commands": [
                        '"${python}" odatix_batch.py --worker "${candidates}"',
                    ],
                }
            ],
        },
    )
    write_json(workflow / "_metrics.yml", {"metrics": request.get("metrics", {})})
    odatix_python = odatix.with_name("python.exe" if os.name == "nt" else "python")
    command = [
        str(odatix_python),
        str(Path(__file__).resolve()),
        "--dispatch",
        str(len(seen)),
        "--jobs",
        str(jobs),
    ]
    with (output / "odatix-console.log").open("wb") as console:
        if os.name == "nt":
            from process_scope import run_owned

            returncode = run_owned(
                command,
                cwd=output,
                env=env,
                console=console,
                bind=lock.bind_process_scope if lock is not None else None,
            )
        else:
            returncode = subprocess.run(
                command,
                cwd=output,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=console,
                stderr=subprocess.STDOUT,
            ).returncode
    rows = collect_results(plan, output / "work" / "workflows" / "candidates", expected)
    report = {
        "status": "success"
        if returncode == 0 and all(row["status"] == "success" for row in rows)
        else "failed",
        "odatix_returncode": returncode,
        "payload_digest": digest,
        "requests": len(rows),
        "distinct_jobs": len(seen),
        "max_concurrent_jobs": jobs,
        "wall_seconds": round(time.monotonic() - start, 3),
        "candidates": rows,
    }
    if os.name == "nt":
        report["process_scope"] = "process-scope.json"
        report["odatix_session"] = "odatix-session.json"
    write_json(output / "batch-result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker")
    parser.add_argument("--dispatch", type=int)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--odatix", type=Path)
    args = parser.parse_args()
    if args.worker:
        return run_candidate(args.worker)
    if args.dispatch is not None:
        return dispatch(args.dispatch, args.jobs)
    if not all((args.request, args.payload, args.output, args.odatix)):
        parser.error("--request, --payload, --output and --odatix are required")
    result = run_batch(
        read_json(args.request), args.payload, args.output, args.odatix, jobs=args.jobs
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
