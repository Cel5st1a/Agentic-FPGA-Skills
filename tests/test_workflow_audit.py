import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/line"))


def test_effective_candidates_are_identified_by_emitted_arithmetic(tmp_path):
    from workflow_verify import effective_identity

    for name in ("a", "b"):
        firmware = tmp_path / name / "firmware"
        (firmware / "weights").mkdir(parents=True)
        for file, content in {
            "defines.h": "typedef ap_fixed<8,4> result_t;",
            "parameters.h": "reuse_factor = 2;",
            "model.cpp": "compute();",
            "weights/w2.txt": "1, 2",
        }.items():
            (firmware / file).write_text(content)
    a, b = tmp_path / "a", tmp_path / "b"
    assert effective_identity(a) == effective_identity(b)
    (b / "firmware/parameters.h").write_text("reuse_factor = 4;")
    assert effective_identity(a) != effective_identity(b)


def test_self_reported_g2_values_cannot_replace_raw_arithmetic(tmp_path):
    from fixed_reference import dependencies, parse_graph, verify_fixed
    from odatix_batch import file_digest

    project, output = tmp_path / "project", tmp_path / "g2"
    firmware = project / "firmware"
    (firmware / "weights").mkdir(parents=True)
    (firmware / "ap_types").mkdir()
    (project / "tb_data").mkdir()
    output.mkdir()
    (firmware / "defines.h").write_text("typedef ap_fixed<8,4> result_t;")
    (firmware / "parameters.h").write_text("config")
    graph = "// insert layers\nnnet::linear<input_t, result_t, config>(input, output);"
    (firmware / "model.cpp").write_text(graph)
    (project / "tb_data/tb_input_features.dat").write_text("1 2\n")
    (output / "reference.cpp").write_text("independent test source")
    (output / "values.txt").write_text("0.5 -0.5\n")
    (output / "codes.txt").write_text("8 248\n")
    record = {
        "status": "PASS",
        "exit": 0,
        "graph": parse_graph(graph),
        "values": [[0.5, -0.5]],
        "source_sha256": dependencies(project, output),
        "values_sha256": file_digest(output / "values.txt"),
        "codes_sha256": file_digest(output / "codes.txt"),
    }
    path = output / "reference.json"
    path.write_text(json.dumps(record))
    assert verify_fixed(project, output) == [[0.5, -0.5]]
    forged = copy.deepcopy(record)
    forged["values"] = [[0.0, 0.0]]
    path.write_text(json.dumps(forged))
    with pytest.raises(ValueError, match="raw"):
        verify_fixed(project, output)
    forged["values_sha256"] = file_digest(output / "values.txt")
    forged["source_sha256"] = {}
    path.write_text(json.dumps(forged))
    with pytest.raises(ValueError):
        verify_fixed(project, output)
