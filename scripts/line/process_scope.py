"""Start and drain one Windows process tree without touching other sessions."""

from __future__ import annotations

import ctypes as c
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes as w
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROCESS_PROOF = "process-scope.json"
GUARD_PROOF = "process-scope-guard.json"
JOB_ENV = "AFD_ODATIX_JOB_NAME"
PROOF_ENV = "AFD_ODATIX_PROCESS_PROOF"
JOB_PREFIX = "Global\\AFD_Odatix_"

JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
STARTF_USESTDHANDLES = 0x0100
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D


class JobLimits(c.Structure):
    _fields_ = [
        ("per_process_user_time", c.c_int64),
        ("per_job_user_time", c.c_int64),
        ("flags", w.DWORD),
        ("min_working_set", c.c_size_t),
        ("max_working_set", c.c_size_t),
        ("active_process_limit", w.DWORD),
        ("affinity", c.c_size_t),
        ("priority_class", w.DWORD),
        ("scheduling_class", w.DWORD),
    ]


class ExtendedJobLimits(c.Structure):
    _fields_ = [
        ("basic", JobLimits),
        ("io_counters", c.c_uint64 * 6),
        ("process_memory", c.c_size_t),
        ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t),
        ("peak_job_memory", c.c_size_t),
    ]


class JobAccounting(c.Structure):
    _fields_ = [
        ("times", c.c_int64 * 4),
        ("page_fault_count", w.DWORD),
        ("total_processes", w.DWORD),
        ("active_processes", w.DWORD),
        ("total_terminated_processes", w.DWORD),
    ]


class StartupInfo(c.Structure):
    _fields_ = [
        ("size", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("dimensions", w.DWORD * 7),
        ("flags", w.DWORD),
        ("show_window", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_bytes", c.c_void_p),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class StartupInfoEx(c.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", c.c_void_p)]


class ProcessInformation(c.Structure):
    _fields_ = [
        ("process", w.HANDLE),
        ("thread", w.HANDLE),
        ("pid", w.DWORD),
        ("tid", w.DWORD),
    ]


def _kernel():
    api = c.WinDLL("kernel32", use_last_error=True)
    handle, boolean, dword, ptr, size = w.HANDLE, w.BOOL, w.DWORD, c.c_void_p, c.c_size_t
    declarations = {
        "CloseHandle": (boolean, handle),
        "CreateJobObjectW": (handle, ptr, w.LPCWSTR),
        "OpenJobObjectW": (handle, dword, boolean, w.LPCWSTR),
        "QueryInformationJobObject": (boolean, handle, c.c_int, ptr, dword, ptr),
        "SetInformationJobObject": (boolean, handle, c.c_int, ptr, dword),
        "TerminateJobObject": (boolean, handle, w.UINT),
        "IsProcessInJob": (boolean, handle, handle, ptr),
        "OpenProcess": (handle, dword, boolean, dword),
        "GetProcessTimes": (boolean, handle, ptr, ptr, ptr, ptr),
        "GetExitCodeProcess": (boolean, handle, ptr),
        "WaitForSingleObject": (dword, handle, dword),
        "ResumeThread": (dword, handle),
        "InitializeProcThreadAttributeList": (boolean, ptr, dword, dword, ptr),
        "UpdateProcThreadAttribute": (boolean, ptr, dword, size, ptr, size, ptr, ptr),
        "DeleteProcThreadAttributeList": (None, ptr),
        "CreateProcessW": (
            boolean,
            w.LPCWSTR,
            w.LPWSTR,
            ptr,
            ptr,
            boolean,
            dword,
            ptr,
            w.LPCWSTR,
            ptr,
            ptr,
        ),
    }
    for name, (result, *arguments) in declarations.items():
        function = getattr(api, name)
        function.restype = result
        function.argtypes = arguments
    return api


if os.name == "nt":
    KERNEL = _kernel()


def _checked(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


def _birth(handle):
    creation, exit_time, kernel, user = (w.FILETIME() for _ in range(4))
    _checked(
        KERNEL.GetProcessTimes(
            handle, c.byref(creation), c.byref(exit_time), c.byref(kernel), c.byref(user)
        )
    )
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def _open_job(name, access=JOB_OBJECT_QUERY):
    if not isinstance(name, str) or not name.startswith(JOB_PREFIX):
        raise ValueError("invalid Windows process scope identity")
    suffix = name.removeprefix(JOB_PREFIX)
    if len(suffix) != 32 or any(char not in "0123456789abcdef" for char in suffix):
        raise ValueError("invalid Windows process scope identity")
    job = KERNEL.OpenJobObjectW(access, False, name)
    if not job and c.get_last_error() != ERROR_FILE_NOT_FOUND:
        raise c.WinError(c.get_last_error())
    return job


@contextmanager
def hold_process_identity(pid, job_name=None):
    """Pin a running process identity so its PID cannot be reused during a stop."""
    handle = KERNEL.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        error = c.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            yield None
            return
        raise c.WinError(error)
    try:
        status = KERNEL.WaitForSingleObject(handle, 0)
        if status == 0:
            yield None
            return
        if status != WAIT_TIMEOUT:
            raise c.WinError(c.get_last_error())
        if job_name is not None:
            job = _open_job(job_name)
            if not job:
                yield None
                return
            try:
                member = w.BOOL()
                _checked(KERNEL.IsProcessInJob(handle, job, c.byref(member)))
                if not member.value:
                    yield None
                    return
            finally:
                KERNEL.CloseHandle(job)
        yield {"pid": pid, "created_filetime": _birth(handle)}
    finally:
        KERNEL.CloseHandle(handle)


def process_identity(pid, job_name=None):
    """Return a running process identity, optionally requiring job membership."""
    with hold_process_identity(pid, job_name) as identity:
        return identity


def _active(job):
    counts = JobAccounting()
    _checked(
        KERNEL.QueryInformationJobObject(
            job, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, c.byref(counts), c.sizeof(counts), None
        )
    )
    return counts.active_processes


def scope_drained(name, proof):
    """Require zero native members and a durable observation by a handle owner."""
    job = _open_job(name)
    if job:
        try:
            if _active(job):
                return False
        finally:
            KERNEL.CloseHandle(job)
    for path in (proof, proof.with_name(GUARD_PROOF)):
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("job_name") == name
                and record.get("state") == "closed"
                and record.get("active_processes") == 0
            ):
                return True
    return False


def write_process_proof(path, record):
    scratch = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        scratch.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def record_recovered_scope(path, name):
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("job_name") != name:
        raise ValueError("saved Windows process scope does not match this lock")
    if record.get("state") != "closed":
        record.update(
            state="closed", active_processes=0, closed_at=_timestamp(), closed_by="stale_recovery"
        )
        write_process_proof(path, record)


def _launch(job, argv, cwd, env, console):
    """Attach at creation, then let the caller record ownership before resuming."""
    import msvcrt

    required = c.c_size_t()
    KERNEL.InitializeProcThreadAttributeList(None, 2, 0, c.byref(required))
    storage = c.create_string_buffer(required.value)
    _checked(KERNEL.InitializeProcThreadAttributeList(storage, 2, 0, c.byref(required)))
    try:
        with open(os.devnull, "rb") as null, os.fdopen(os.dup(console.fileno()), "wb") as output:
            os.set_inheritable(null.fileno(), True)
            os.set_inheritable(output.fileno(), True)
            handles = (w.HANDLE * 2)(
                msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(output.fileno())
            )
            jobs = (w.HANDLE * 1)(job)
            for key, value in (
                (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
                (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
            ):
                _checked(
                    KERNEL.UpdateProcThreadAttribute(
                        storage, 0, key, value, c.sizeof(value), None, None
                    )
                )
            start = StartupInfoEx()
            start.startup.size = c.sizeof(start)
            start.startup.flags = STARTF_USESTDHANDLES
            start.startup.stdin, start.startup.stdout = handles
            start.startup.stderr = start.startup.stdout
            start.attributes = c.cast(storage, c.c_void_p)
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = c.create_unicode_buffer(
                "".join(
                    f"{key}={value}\0"
                    for key, value in sorted(env.items(), key=lambda p: p[0].upper())
                )
                + "\0"
            )
            process = ProcessInformation()
            _checked(
                KERNEL.CreateProcessW(
                    argv[0],
                    command,
                    None,
                    None,
                    True,
                    CREATE_SUSPENDED
                    | CREATE_UNICODE_ENVIRONMENT
                    | EXTENDED_STARTUPINFO_PRESENT
                    | subprocess.CREATE_NO_WINDOW,
                    environment,
                    str(cwd),
                    c.byref(start),
                    c.byref(process),
                )
            )
            return process
    finally:
        KERNEL.DeleteProcThreadAttributeList(storage)


def _wait(process):
    while True:
        result = KERNEL.WaitForSingleObject(process, 100)
        if result == 0:
            code = w.DWORD()
            _checked(KERNEL.GetExitCodeProcess(process, c.byref(code)))
            return code.value
        if result != WAIT_TIMEOUT:
            raise c.WinError(c.get_last_error())


def _drain(job):
    remaining = _active(job)
    if remaining:
        _checked(KERNEL.TerminateJobObject(job, 1))
        deadline = time.monotonic() + 10
        while _active(job):
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows process scope did not drain after termination")
            time.sleep(0.05)
    return bool(remaining)


def _watch_scope(name, proof):
    job = _checked(_open_job(name, JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE))
    try:
        record = {
            "schema_version": 1,
            "job_name": name,
            "state": "active",
            "observer": process_identity(os.getpid()),
            "opened_at": _timestamp(),
        }
        write_process_proof(proof, record)
        # Only the batch owner has the write end. Its death also delivers EOF.
        sys.stdin.buffer.read()
        terminated = _drain(job)
        record.update(
            state="closed",
            active_processes=0,
            closed_at=_timestamp(),
            terminated_descendants=terminated,
        )
        write_process_proof(proof, record)
    finally:
        KERNEL.CloseHandle(job)


def _wait_for_guard(guard, proof, name):
    deadline = time.monotonic() + 10
    while guard.poll() is None and time.monotonic() < deadline:
        if proof.is_file():
            record = json.loads(proof.read_text(encoding="utf-8"))
            if record.get("job_name") == name and record.get("state") == "active":
                return
        time.sleep(0.025)
    raise RuntimeError("Windows process-scope observer did not open the job")


def run_owned(argv, *, cwd, console, env=None, bind=None):
    """Run a dispatcher in a named kill-on-close job and persist its cleanup proof."""
    cwd = Path(cwd).resolve()
    proof = cwd / PROCESS_PROOF
    guard_proof = cwd / GUARD_PROOF
    name = JOB_PREFIX + uuid4().hex
    c.set_last_error(0)
    job = _checked(KERNEL.CreateJobObjectW(None, name))
    created = c.get_last_error() != ERROR_ALREADY_EXISTS
    process = None
    guard = None
    record = None
    try:
        if not created:
            raise FileExistsError("Windows process scope name already exists")
        limits = ExtendedJobLimits()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _checked(
            KERNEL.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, c.byref(limits), c.sizeof(limits)
            )
        )
        record = {
            "schema_version": 1,
            "job_name": name,
            "state": "starting",
            "owner": process_identity(os.getpid()),
            "workspace": str(cwd),
            "opened_at": _timestamp(),
        }
        write_process_proof(proof, record)
        guard = subprocess.Popen(
            [
                sys._base_executable,
                "-B",
                str(Path(__file__).resolve()),
                "--watch",
                name,
                str(guard_proof),
            ],
            stdin=subprocess.PIPE,
            stdout=console,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _wait_for_guard(guard, guard_proof, name)
        if bind is not None:
            bind(name, proof)
        child_environment = {
            **(os.environ if env is None else env),
            JOB_ENV: name,
            PROOF_ENV: str(proof),
        }
        process = _launch(job, list(map(str, argv)), cwd, child_environment, console)
        record.update(
            state="active",
            dispatcher={"pid": process.pid, "created_filetime": _birth(process.process)},
            active_processes=_active(job),
        )
        write_process_proof(proof, record)
        if KERNEL.ResumeThread(process.thread) == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        record["returncode"] = _wait(process.process)
        return record["returncode"]
    except BaseException as exc:
        if record is not None:
            record["interrupted"] = type(exc).__name__
        raise
    finally:
        try:
            if record is not None:
                terminated = _drain(job)
                record.update(
                    state="closed",
                    active_processes=0,
                    closed_at=_timestamp(),
                    closed_by="owner",
                    terminated_descendants=terminated,
                )
                write_process_proof(proof, record)
        finally:
            try:
                if guard is not None:
                    guard.stdin.close()
                    try:
                        code = guard.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        guard.kill()
                        guard.wait(timeout=5)
                        raise
                    if code:
                        raise RuntimeError(
                            f"Windows process-scope observer exited with code {code}"
                        )
            finally:
                if process is not None:
                    KERNEL.CloseHandle(process.thread)
                    KERNEL.CloseHandle(process.process)
                KERNEL.CloseHandle(job)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--watch":
        raise SystemExit("expected a process scope and observer proof path")
    _watch_scope(sys.argv[2], Path(sys.argv[3]))
