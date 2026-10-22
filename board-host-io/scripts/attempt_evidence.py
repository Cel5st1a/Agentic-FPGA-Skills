"""Fail-closed attempt records and process-lifetime JTAG cable exclusion."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile

LOCK_ROOT = Path(tempfile.gettempdir())


def write_json_atomic(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def attempt_output(output, summary):
    output = Path(output)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError(f"attempt output already exists: {output}; preserve its evidence "
                         "and choose a new output path for the next attempt") from None
    summary["attempt_complete"] = False
    write_json_atomic(output / "summary.json", summary)
    try:
        yield
    except BaseException as exc:
        summary.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        for flag in ("programmed", "hardware_numerical_verified"):
            if flag in summary:
                summary[flag] = False
        raise
    else:
        summary["attempt_complete"] = True
    finally:
        write_json_atomic(output / "summary.json", summary)


class CableLock:
    """Non-queuing, OS-held byte lock; a killed process releases it automatically.

    Lock files remain in place: unlinking them would permit two holders on different
    file objects. The EDA lock is independent and is never read or changed here.
    """
    def __init__(self, cable_serial):
        if not re.fullmatch(r"[0-9A-Za-z]+", cable_serial):
            raise ValueError("JTAG cable serial contains unsupported characters")
        self.path = LOCK_ROOT / f"fpga_skill_jtag_{cable_serial.upper()}.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ValueError(f"JTAG cable is busy ({self.path}): {exc}") from exc
        return self

    def __exit__(self, *_exc):
        self.handle.close()
        self.handle = None
