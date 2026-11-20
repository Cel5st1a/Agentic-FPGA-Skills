"""Independent scalar fixed-point evaluation of a supported generated layer graph."""

import argparse
import re
import subprocess
import time
from pathlib import Path

import numpy as np
from odatix_batch import file_digest, read_json, write_json

# Per-operation contract for the emitted `nnet::<op><...>(...)` calls:
#   arity    number of template arguments written by hls4ml
#   output   index of the output tensor argument
#   alpha    index of a scalar literal operand (ELU's alpha), if any
#   fields   config typedef names of the weight operands, in call order
#   result   index of the template argument naming the output value type
OPERATIONS = {
    "repack_stream": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "zeropad1d_cl": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "dense": {"arity": 3, "output": 1, "fields": ("weight_t", "bias_t"), "result": 1},
    "conv_2d_cl": {"arity": 3, "output": 1, "fields": ("weight_t", "bias_t"), "result": 1},
    "conv_1d_cl": {"arity": 3, "output": 1, "fields": ("weight_t", "bias_t"), "result": 1},
    "normalize": {"arity": 3, "output": 1, "fields": ("scale_t", "bias_t"), "result": 1},
    "pooling2d_cl": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "pooling1d_cl": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "relu": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "linear": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "softmax": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "softsign": {"arity": 3, "output": 1, "fields": (), "result": 1},
    "elu": {"arity": 4, "output": 2, "alpha": 1, "fields": (), "result": 2},
}

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
LITERAL = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?[fFlLuU]*")


def activation_semantics(project, graph):
    """Read the lookup-table formulas of the *emitted* nnet_activation.h.

    The activation table generation/index expressions are not captured by the layer
    configs, and a deployment may patch the stock hls4ml formulas (for example the
    softsign input range).  Mirroring the emitted header keeps the independent
    reference faithful; the returned macros are injected into reference.cpp.
    """
    needed = {layer["operation"] for layer in graph} & {"elu", "softsign"}
    if not needed:
        return {}
    header = project / "firmware/nnet_utils/nnet_activation.h"
    if not header.is_file():
        return {}
    text = header.read_text(encoding="utf8")
    semantics = {}
    if "softsign" in needed:
        start = text.find("Softsign Activation")
        end = text.find("ELU Activation", start + 1)
        if start < 0 or end < 0:
            raise ValueError("cannot locate the emitted softsign template")
        section = text[start:end]
        values = {}
        match = re.search(
            r"float\s+in_val\s*=\s*2\s*\*\s*([0-9.]+)\s*\*\s*\(\s*ii\s*-\s*float\(N_TABLE\)"
            r"\s*/\s*2\.0\s*\)\s*/\s*float\(N_TABLE\)",
            section,
        )
        if not match:
            raise ValueError("unsupported emitted softsign table range expression")
        values["REFERENCE_SOFTSIGN_RANGE"] = match[1]
        if "AFD-INTERPOLATED-SOFTSIGN" in section:
            values["REFERENCE_SOFTSIGN_INTERP"] = "1"
            match = re.search(r"scaled = offset \* \(CONFIG_T::table_size\s*/\s*(\d+)\)", section)
            if not match:
                raise ValueError("unsupported interpolated softsign scale expression")
            values["REFERENCE_SOFTSIGN_INTERP_DIVISOR"] = match[1]
            match = re.search(r"\(softsign_work_t\)([0-9.]+)", section)
            if not match:
                raise ValueError("unsupported interpolated softsign offset expression")
            values["REFERENCE_SOFTSIGN_OFFSET_VALUE"] = match[1]
        else:
            match = re.search(
                r"data_round\s*=\s*data\[ii\]\s*\*\s*CONFIG_T::table_size\s*/\s*(-?\d+)",
                section,
            )
            if not match:
                raise ValueError("unsupported emitted softsign index expression")
            values["REFERENCE_SOFTSIGN_DIVISOR"] = match[1]
            match = re.search(
                r"index\s*=\s*data_round\s*\+\s*(\d+)\s*\*\s*CONFIG_T::table_size\s*/\s*(\d+)",
                section,
            )
            if not match:
                raise ValueError("unsupported emitted softsign offset expression")
            values["REFERENCE_SOFTSIGN_OFFSET"] = match[1]
            values["REFERENCE_SOFTSIGN_OFFSET_DIVISOR"] = match[2]
        semantics.update(values)
    if "elu" in needed:
        start = text.find("//       ELU Activation")
        end = text.find("//       SELU Activation", start + 1)
        section = text[start:] if end < 0 else text[start:end]
        match = re.search(
            r"float\s+in_val\s*=\s*-\s*([0-9.]+)\s*\*\s*ii\s*/\s*float\(N_TABLE\)", section
        )
        if not match:
            raise ValueError("unsupported emitted ELU table range expression")
        semantics["REFERENCE_ELU_RANGE"] = match[1]
        if "AFD-INTERPOLATED-ELU" in section:
            semantics["REFERENCE_ELU_INTERP"] = "1"
            match = re.search(r"scaled = mag \* \(CONFIG_T::table_size\s*/\s*(\d+)\)", section)
            if not match:
                raise ValueError("unsupported interpolated ELU scale expression")
            semantics["REFERENCE_ELU_DIVISOR"] = match[1]
            match = re.search(r"\(elu_work_t\)([0-9.]+)", section)
            if not match:
                raise ValueError("unsupported interpolated ELU clamp expression")
            semantics["REFERENCE_ELU_CLAMP"] = match[1]
        else:
            match = re.search(
                r"index\s*=\s*datareg\s*\*\s*CONFIG_T::table_size\s*/\s*-?\s*(\d+)", section
            )
            if not match:
                raise ValueError("unsupported emitted ELU index expression")
            semantics["REFERENCE_ELU_DIVISOR"] = match[1]
    return semantics


def parse_graph(source):
    """Parse the emitted layer calls, resolving tensor aliases in source order."""
    aliases = dict(re.findall(r"auto\s*&\s*(\w+)\s*=\s*(\w+)\s*;", source))
    graph = []
    for name, templates, arguments in re.findall(
        r"nnet::(\w+)\s*<([^;<>]+)>\s*\(([^;]+)\)\s*;", source
    ):
        if name == "load_weights_from_txt":
            continue
        spec = OPERATIONS.get(name)
        if spec is None:
            raise ValueError(f"unsupported independent reference operation {name}")
        types = [x.strip() for x in templates.split(",")]
        args = [x.strip() for x in arguments.split(",")]
        weight_positions = [i for i in range(2, len(args)) if i != spec["output"]]
        expected = 2 + len(weight_positions) + (1 if "alpha" in spec else 0)
        if len(types) != spec["arity"] or len(args) != expected:
            raise ValueError(f"unsupported generated call {name}")
        if not all(
            IDENTIFIER.fullmatch(item) or (name == "repack_stream" and i == 2 and item.isdecimal())
            for i, item in enumerate(types)
        ):
            raise ValueError("generated graph has a non-identifier template argument")
        incoming = args[0]
        output = args[spec["output"]]
        weight_names = [args[i] for i in weight_positions]
        if not IDENTIFIER.fullmatch(incoming) or not IDENTIFIER.fullmatch(output):
            raise ValueError("generated graph has a non-identifier tensor operand")
        if not all(IDENTIFIER.fullmatch(item) for item in weight_names):
            raise ValueError("generated graph has a non-identifier weight operand")
        record = {
            "operation": name,
            "types": types,
            "input": None,
            "output": output,
            "weights": weight_names,
        }
        if "alpha" in spec:
            alpha = args[spec["alpha"]]
            if not LITERAL.fullmatch(alpha):
                raise ValueError("generated graph has a non-literal activation parameter")
            record["alpha"] = alpha
        seen = set()
        while incoming in aliases:
            if incoming in seen:
                raise ValueError("cyclic tensor alias")
            seen.add(incoming)
            incoming = aliases[incoming]
        record["input"] = incoming
        graph.append(record)
    if not graph:
        raise ValueError("no supported generated computation")
    available = {graph[0]["input"]}
    for row in graph:
        if row["input"] not in available or row["output"] in available:
            raise ValueError("unsupported graph dependency")
        available.add(row["output"])
    return graph


def source_for(graph, semantics=None):
    """Emit the independent scalar C++ program for a parsed graph."""
    first, last = graph[0], graph[-1]
    definitions = "".join(
        f"#define {name} {value}\n" for name, value in sorted((semantics or {}).items())
    )
    declarations, calls = [], []
    for layer in graph:
        spec = OPERATIONS[layer["operation"]]
        config = layer["types"][spec["arity"] - 1]
        args = [layer["input"]]
        for field, weight in zip(spec["fields"], layer["weights"], strict=True):
            variable = f"weight_{layer['output']}_{field}"
            declarations.append(
                f"    auto {variable} = load<typename {config}::{field}>"
                f'(weights + "/{weight}.txt");'
            )
            args.append(variable)
        if "alpha" in spec:
            args.append(layer["alpha"])
        scalar_types = list(layer["types"])
        for index in {0, spec["result"]}:
            scalar_types[index] = f"typename scalar<{scalar_types[index]}>::type"
        if layer["operation"] != "repack_stream":
            scalar_types[-1] = f"reference_logical_config<{scalar_types[-1]}>"
        calls.append(
            f"auto {layer['output']} = reference_{layer['operation']}"
            f"<{', '.join(scalar_types)}>({', '.join(args)});"
        )
    input_type = first["types"][0]
    return f"""#include "defines.h"
#include "parameters.h"
{definitions}#include "reference_ops.hpp"
int main(int argc, char** argv) {{
    if (argc != 5) return 2;
    std::string weights(argv[1]);
    std::ifstream input(argv[2]);
    std::ofstream output(argv[3]), codes(argv[4]);
    if (!input || !output || !codes) return 3;
    output << std::setprecision(17);
    {" ".join(declarations)}
    std::string line;
    while (std::getline(input, line)) {{
        if (line.find_first_not_of(" \\r\\t") == std::string::npos) continue;
        std::istringstream row(line);
        std::vector<typename scalar<{input_type}>::type> {first["input"]};
        float value;
        while (row >> value) {first["input"]}.push_back(value);
        {" ".join(calls)}
        for (auto value : {last["output"]}) {{
            output << value.to_double() << " ";
            codes << value.range(value.width - 1, 0).to_uint64() << " ";
        }}
        output << "\\n";
        codes << "\\n";
    }}
    return 0;
}}
"""


def dependencies(project, output):
    firmware = project / "firmware"
    entrypoints = [
        p for p in firmware.glob("*.cpp") if "insert layers" in p.read_text(encoding="utf8")
    ]
    if len(entrypoints) != 1:
        raise ValueError("expected a unique generated model graph")
    paths = [
        Path(__file__).with_name("reference_ops.hpp"),
        output / "reference.cpp",
        entrypoints[0],
        firmware / "defines.h",
        firmware / "parameters.h",
        project / "tb_data/tb_input_features.dat",
        *sorted((firmware / "weights").glob("*.txt")),
        *sorted((firmware / "nnet_utils").rglob("*.h")),
        *(p for p in sorted((firmware / "ap_types").rglob("*")) if p.is_file()),
    ]
    return {str(path): file_digest(path) for path in paths}


def verify_fixed(project, output):
    record = read_json(output / "reference.json")
    if record["status"] != "PASS" or record["exit"] != 0:
        raise ValueError("independent fixed execution did not pass")
    if record["source_sha256"] != dependencies(project, output):
        raise ValueError("independent fixed source dependencies changed")
    for name in ("values", "codes"):
        if file_digest(output / f"{name}.txt") != record[f"{name}_sha256"]:
            raise ValueError("raw fixed-reference output changed")
    entrypoint = next(
        p
        for p in (project / "firmware").glob("*.cpp")
        if "insert layers" in p.read_text(encoding="utf8")
    )
    graph = parse_graph(entrypoint.read_text(encoding="utf8"))
    if graph != record["graph"]:
        raise ValueError("independent graph no longer matches the generated graph")
    definitions = (project / "firmware/defines.h").read_text(encoding="utf8")
    result_type = graph[-1]["types"][OPERATIONS[graph[-1]["operation"]]["result"]]
    declarations = re.findall(
        r"typedef\s+([^;]+)\s+" + re.escape(result_type) + r"\s*;", definitions
    )
    match = (
        re.search(r"ap_(u?fixed)<\s*(\d+)\s*,\s*(\d+)", declarations[0])
        if len(declarations) == 1
        else None
    )
    if not match or not 0 <= int(match[3]) <= int(match[2]) <= 40:
        raise ValueError("unsupported independent output representation")
    width, fractional = int(match[2]), int(match[2]) - int(match[3])
    values = np.loadtxt(output / "values.txt", ndmin=2)
    codes = [
        [int(item) for item in line.split()]
        for line in (output / "codes.txt").read_text().splitlines()
    ]
    if any(c < 0 or c >= (1 << width) for row in codes for c in row):
        raise ValueError("raw fixed code is outside its representation")
    decoded = np.asarray(codes, dtype=np.int64)
    if match[1] == "fixed":
        decoded = np.where(decoded >= (1 << (width - 1)), decoded - (1 << width), decoded)
    decoded = decoded.astype(np.float64) / (1 << fractional)
    if (
        not np.isfinite(values).all()
        or not np.array_equal(values, decoded)
        or not np.array_equal(values, record["values"])
    ):
        raise ValueError("raw fixed values, output codes and summary differ")
    return values.tolist()


def evaluate(project, output, compiler):
    project, output = project.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    firmware = project / "firmware"
    entrypoints = [
        p for p in firmware.glob("*.cpp") if "insert layers" in p.read_text(encoding="utf8")
    ]
    if len(entrypoints) != 1:
        raise ValueError("expected a unique generated model graph")
    graph = parse_graph(entrypoints[0].read_text(encoding="utf8"))
    source = output / "reference.cpp"
    source.write_text(source_for(graph, activation_semantics(project, graph)), encoding="utf8")
    binary = output / "reference.exe"
    header = Path(__file__).with_name("reference_ops.hpp")
    command = [
        str(compiler),
        "-std=c++17",
        "-O2",
        f"-I{firmware}",
        f"-I{firmware / 'ap_types'}",
        f"-I{header.parent}",
        str(source),
        "-o",
        str(binary),
    ]
    started = time.monotonic()
    build = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=300)
    (output / "compiler.stdout.txt").write_text(build.stdout, encoding="utf8")
    (output / "compiler.stderr.txt").write_text(build.stderr, encoding="utf8")
    write_json(
        output / "compiler.json",
        {"command": command, "exit": build.returncode, "wall_s": time.monotonic() - started},
    )
    if build.returncode:
        raise ValueError(f"independent fixed reference compilation failed at {output}")
    input_path = project / "tb_data/tb_input_features.dat"
    command = [
        str(binary),
        str(firmware / "weights"),
        str(input_path),
        str(output / "values.txt"),
        str(output / "codes.txt"),
    ]
    run = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=600)
    (output / "runtime.stderr.txt").write_text(run.stderr, encoding="utf8")
    values = [
        [float(v) for v in line.split()]
        for line in (output / "values.txt").read_text().splitlines()
    ]
    record = {
        "status": "PASS" if run.returncode == 0 else "ERROR",
        "graph": graph,
        "run_command": command,
        "exit": run.returncode,
        "wall_s": time.monotonic() - started,
        "source_sha256": dependencies(project, output),
        "values": values,
        "values_sha256": file_digest(output / "values.txt"),
        "codes_sha256": file_digest(output / "codes.txt"),
        "method": "independent direct scalar fixed arithmetic, no generated kernel calls",
    }
    write_json(output / "reference.json", record)
    if run.returncode:
        raise ValueError("independent fixed reference execution failed")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.project, args.output, args.compiler)
