"""Stage the RTL that a Vivado stage actually reads into `<board>/rtl`.

Why this exists
---------------
The board wrapper writes `board_top.v`, `board_top_tb.v`, `board.xdc` and
`wrapper.json` into the board directory, while the hls4ml core's Verilog lives in the
generated HLS project. Both the simulator and the synthesis/implementation scripts read
`<board>/rtl/*.v`, but nothing created that directory: the simulation step failed with
`no files matched glob ".../rtl/*.v"` unless a previous synthesis run happened to have
staged it. Depending on stage order for the existence of an input is a defect, not a
convention.

Staging rules
-------------
* The directory is reconciled, not wiped: a file left over from a *previous* conversion
  (different layer names, same project) must not be compiled into this design, but
  unchanged files keep their timestamps so the `STALE_CHECKPOINT` guard in
  `run_vivado.py` keeps comparing meaningful times.
* Every staged file's sha256 is returned, so the evidence records exactly which RTL the
  simulator or synthesizer read.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

CORE_PATTERNS = ("*.v", "*.sv", "*.dat", "*.mem", "*.hex")
WRAPPER_PATTERNS = ("board_top*.v", "board.xdc")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_file(left: Path, right: Path) -> bool:
    if not right.is_file() or left.stat().st_size != right.stat().st_size:
        return False
    return sha256_file(left) == sha256_file(right)


def stage_rtl(board: Path, wrapper: dict) -> dict:
    core = Path(wrapper["core_verilog"])
    if not core.is_file():
        raise SystemExit(f"generated core not found: {core}")
    board = board.resolve()
    rtl_dir = board / "rtl"
    rtl_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Path] = []
    for pattern in CORE_PATTERNS:
        sources += sorted(core.parent.glob(pattern))
    for pattern in WRAPPER_PATTERNS:
        sources += sorted(board.glob(pattern))
    for relative in wrapper.get("extra_rtl", []):
        source = board / relative
        if source.parent != board or source.suffix not in {".v", ".sv"} or not source.is_file():
            raise SystemExit(f"invalid extra RTL source: {relative}")
        sources.append(source)
    if not sources:
        raise SystemExit(f"nothing to stage: no RTL next to {core} and no wrapper output in {board}")

    expected = {source.name for source in sources}
    removed = []
    for stale in sorted(rtl_dir.iterdir()):
        if stale.is_file() and stale.name not in expected:
            stale.unlink()
            removed.append(stale.name)

    staged: dict[str, str] = {}
    copied = []
    for source in sources:
        target = rtl_dir / source.name
        if not _same_file(source, target):
            shutil.copyfile(source, target)
            copied.append(source.name)
        staged[source.name] = sha256_file(target)

    return {
        "rtl_dir": str(rtl_dir),
        "core_verilog": str(core),
        "core_verilog_sha256": sha256_file(core),
        "files": staged,
        "copied": copied,
        "removed_stale": removed,
    }
