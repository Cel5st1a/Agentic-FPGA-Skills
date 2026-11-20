"""Bind a ranked scan candidate to its reusable HLS outputs."""

from odatix_batch import file_digest, read_json, snapshot_digest

MANIFEST = "evidence/hls-artifact-manifest.json"


def describe_hls(run):
    files = (
        "evidence/convert-evidence.json",
        "evidence/python-output.json",
        "evidence/csim-evidence.json",
        "evidence/csynth-evidence.json",
        "p3-build/csim/csim_results.log",
    )
    report = read_json(run / "evidence/csynth-evidence.json")["artifacts"]["csynth_xml"]
    filename = report["file"]
    xml = run / "p3-build/csynth" / filename
    if xml.name != filename:
        raise ValueError("unexpected HLS report path")
    if file_digest(xml) != report["sha256"]:
        raise ValueError("HLS report differs from synthesis evidence")
    rtl = run / "p2-convert/project/myproject_prj/solution1/syn/verilog"
    if not any(rtl.glob("*.v")):
        raise ValueError("HLS output is missing Verilog")
    return {
        "files": {
            name: file_digest(run / name)
            for name in (*files, xml.relative_to(run).as_posix())
        },
        "rtl_tree_sha256": snapshot_digest(rtl),
    }


def verify_hls(run):
    if read_json(run / MANIFEST) != describe_hls(run):
        raise ValueError("reusable HLS artifacts differ from the scan manifest")
