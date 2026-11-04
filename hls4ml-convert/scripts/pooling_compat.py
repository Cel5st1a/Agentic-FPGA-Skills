"""Repair a recognized generated Vitis pooling template, preserving stock fallback."""
import hashlib
from pathlib import Path

STOCK_SHA = "502853135627a83789aeb3012b09f8546fe685195f9f70f81395e37d32c2db53"


def patch_stream_pooling(project, version, backend):
    target = (Path(project) / "firmware/nnet_utils/nnet_pooling_stream.h").resolve()
    if not target.is_relative_to(Path(project).resolve()) or target.is_symlink():
        raise ValueError("pooling target must be inside generated project")
    if version != "1.3.0" or backend.lower() != "vitis" or not target.is_file():
        return {"status": "NOT_APPLICABLE", "hardware_verified": False}
    raw = target.read_text(encoding="utf-8")
    if "struct opt4_pool_dispatch" in raw:
        return {"status": "ALREADY_APPLIED", "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    start_marker = "template <class data_T, class res_T, typename CONFIG_T>\nvoid pooling2d_cl("
    end_marker = "// *************************************************\n//                  Pooling 1D"
    if start_marker not in raw or end_marker not in raw:
        raise ValueError("unrecognized pooling template; no changes")
    start = raw.index(start_marker)
    end = raw.index(end_marker, start)
    if hashlib.sha256(raw[start:end].encode()).hexdigest() != STOCK_SHA:
        raise ValueError("unrecognized pooling function hash; no changes")
    replacement = (Path(__file__).parents[1] / "assets/pooling2d_explicit.h").read_text(encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text(raw[:start] + replacement + raw[end:], encoding="utf-8")
    return {"patch": "explicit-stream-maxpool", "status": "APPLIED",
            "before_sha256": before, "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "scope": "2x2 stride2 unpadded full-channel packets; stock fallback otherwise",
            "hardware_verified": False}
