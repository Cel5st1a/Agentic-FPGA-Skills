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
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from line.identity import capture_reuse_identity, compare_reuse_identity  # noqa: E402

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


def core_sources(core: Path) -> dict[str, str]:
    """Snapshot exactly the flat core bundle consumed by this staging rule."""
    core = Path(core)
    if not core.is_file():
        raise FileNotFoundError(f"generated core not found: {core}")
    return {
        p.name: sha256_file(p)
        for pattern in CORE_PATTERNS
        for p in sorted(core.parent.glob(pattern))
        if p.is_file()
    }


def dependency_delta(previous: dict, current: dict) -> dict:
    """RTL-only change scope, never automatic reuse or model/hardware qualification."""
    before, after = previous.get("dependency_scopes"), current.get("dependency_scopes")
    if not before or not after:
        return {
            "staged_core_content_unchanged": None,
            "affected_stages": None,
            "reuse_identity_comparison": compare_reuse_identity(
                previous.get("reuse_identity"), current.get("reuse_identity")
            ),
        }
    unchanged = before["core"] == after["core"]
    downstream = ["board_simulation", "synth", "impl", "bitstream", "board_capture"]
    affected = (
        ["core_simulation", *downstream]
        if not unchanged
        else downstream
        if before["wrapper"] != after["wrapper"]
        else []
    )
    return {
        "staged_core_content_unchanged": unchanged,
        "affected_stages": affected,
        "scope": "RTL content only; model/config/tool identity not evaluated",
        "hardware_verified": False,
        "reuse_identity_comparison": compare_reuse_identity(
            previous.get("reuse_identity"), current.get("reuse_identity")
        ),
    }


def stage_rtl(board: Path, wrapper: dict) -> dict:
    core = Path(wrapper["core_verilog"])
    if not core.is_file():
        raise SystemExit(f"generated core not found: {core}")
    board = board.resolve()
    rtl_dir = board / "rtl"

    current_core = core_sources(core)
    recorded = wrapper.get("core_source_manifest")
    if recorded is not None and (not isinstance(recorded, dict) or recorded != current_core):
        raise ValueError(
            "core source manifest differs: missing, added or modified source; "
            "preserve the old attempt and regenerate its dependency record"
        )

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
        raise SystemExit(
            f"nothing to stage: no RTL next to {core} and no wrapper output in {board}"
        )

    unique = {}
    for source in sources:
        key = os.path.normcase(source.name)
        if key in unique and unique[key].resolve() != source.resolve():
            raise ValueError(f"colliding RTL source name: {source.name}")
        unique[key] = source
    sources = list(unique.values())
    reuse = None
    if "reuse_inputs" in wrapper:
        request = wrapper["reuse_inputs"]
        try:
            reuse = capture_reuse_identity(
                wrapper["contract"],
                arithmetic_config=request.get("arithmetic_config"),
                tool_versions=request.get("tool_versions"),
                arithmetic_files=request.get("arithmetic_files"),
                core_files={p.name: p for p in sources if p.name in current_core},
                wrapper_files={p.name: p for p in sources if p.name not in current_core},
            )
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
            reuse = {
                "schema_version": 1,
                "kind": "reuse_identity",
                "fingerprints": {},
                "missing": {"core": [str(error)], "wrapper": [str(error)]},
                "hardware_verified": False,
            }
    expected = set(unique)
    rtl_dir.mkdir(parents=True, exist_ok=True)
    removed = []
    for stale in sorted(rtl_dir.iterdir()):
        if stale.is_file() and os.path.normcase(stale.name) not in expected:
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
        "core_manifest_verification": "VERIFIED_CONTENT"
        if recorded is not None
        else "UNRECORDED_LEGACY",
        "dependency_scopes": {
            "core": current_core,
            "wrapper": {p.name: staged[p.name] for p in sources if p.name not in current_core},
        },
        "reuse_identity": reuse,
    }
