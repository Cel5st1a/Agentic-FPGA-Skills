"""Integer reference for the qualified hls4ml Vitis Dense/ReLU MLP profile."""

import math
import re
import struct
from dataclasses import dataclass

from odatix_batch import file_digest

# Only qualify the observed template and generated call graph. Changing a kernel,
# call, weight loader, or output formatter requires a new coverage check.
SOURCE_SHA256 = {
    "myproject_test.cpp": "4e2c161db63874b892f9faf0a56297d8649ffbac6e4ccb9f4abf1e7aed8f361c",
    "firmware/nnet_utils/nnet_dense.h": (
        "96a8568b0b32eda76b90ae0f7b4f4194cf948802b2c55f48135d13504928f6c2"
    ),
    "firmware/myproject.cpp": "8c63372c8e5405e34a883d761cfe2d4b66e0204e272f05c100bf5fb8d5b06966",
    "firmware/nnet_utils/nnet_dense_resource.h": (
        "247e6bf2ab59bef2ed100df348168308e71b0f2e464a09a3a09ce9d62fdfdb5e"
    ),
    "firmware/nnet_utils/nnet_mult.h": (
        "28bc325053f8458fc65e12c54a6b98aa4d9bfe8f8984836a6ddbb149bdd9a269"
    ),
    "firmware/nnet_utils/nnet_activation.h": (
        "2581cd0352d5df49fcd11902375c00e20da9562f7dac2a2137377de19bfe9e29"
    ),
    "firmware/nnet_utils/nnet_helpers.h": (
        "be3b44aa21ba78d32f47d74ef42be38e082dab1913bbe6214dc83afd74511c61"
    ),
}
PARAMETERS = {
    "a39415107ccd8807277f77fccdbb37bd3c2423c25b1a19d936a8455a969cfdc3": 64,
    "3c61e98c9ec7c080ad19b7f6b3d4a37b2280bfca0c7d7928518ef755492f0115": 128,
}
ROLES = {"input_t", "model_default_t", "layer2_t", "layer3_t", "layer4_t", "layer5_t", "result_t"}
PROBE_FILE = "csim_fixed_codes.log"


class UnsupportedProfile(ValueError):
    """The generated profile is outside the validated reference coverage."""


@dataclass(frozen=True)
class Fixed:
    width: int
    integer: int

    @property
    def fractional(self):
        return self.width - self.integer

    def wrap(self, value):
        half = 1 << (self.width - 1)
        return (value + half) % (half * 2) - half

    def code(self, value):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("fixed-point input is not finite")
        n, d = f.as_integer_ratio()
        return self.wrap((n << self.fractional) // d)

    def mul(self, a, b):
        return self.wrap((a * b) >> self.fractional)


def profile(project):
    for path, digest in SOURCE_SHA256.items():
        source = project / path
        if not source.is_file() or file_digest(source) != digest:
            raise UnsupportedProfile(f"unqualified generated source {path}")
    firmware = project / "firmware"
    parameters = file_digest(firmware / "parameters.h")
    if parameters not in PARAMETERS:
        raise UnsupportedProfile("unqualified Dense parameters")
    definitions = (firmware / "defines.h").read_text(encoding="utf-8")
    types = {
        name: (int(w), int(i))
        for w, i, name in re.findall(r"typedef\s+ap_fixed<(\d+),\s*(\d+)>\s+(\w+);", definitions)
        if name in ROLES
    }
    if types.keys() != ROLES or len(set(types.values())) != 1:
        raise UnsupportedProfile(
            "expected the same default signed ap_fixed type at all Dense boundaries"
        )
    width, integer = types["input_t"]
    if not 1 <= integer <= width <= 32:
        raise UnsupportedProfile("fixed width must be between 1 and 32 bits")
    return Fixed(width, integer), PARAMETERS[parameters]


def probe(project, fixed):
    """Add a raw-code sidecar to the CSim testbench, retaining the normal result log."""
    path = project / "myproject_test.cpp"
    text = path.read_bytes()
    before = file_digest(path)
    if before != SOURCE_SHA256["myproject_test.cpp"]:
        raise UnsupportedProfile("CSim probe requires the qualified testbench")
    output = b"nnet::print_result<result_t, 4>(layer6_out, fout);"
    if text.count(output) != 2:
        raise UnsupportedProfile("expected both testbench output sites")
    write = (
        f"\n            for (int k = 0; k < 4; ++k) "
        f'fixed_log << layer6_out[k].range({fixed.width - 1}, 0).to_uint64() << " ";'
        '\n            fixed_log << "\\n";'
    ).encode("ascii")
    text = text.replace(
        b"std::ofstream fout(RESULTS_LOG);",
        b"std::ofstream fout(RESULTS_LOG);\n"
        b'    std::ofstream fixed_log("tb_data/csim_fixed_codes.log");',
    ).replace(output, output + write)
    path.write_bytes(text)
    return {"original_sha256": before, "instrumented_sha256": file_digest(path)}


def evaluate(project, fixed, original_inputs=None):
    firmware = project / "firmware/weights"
    input_file = project / "tb_data/tb_input_features.dat"

    def as_float32(value):
        return struct.unpack("f", struct.pack("f", float(value)))[0]

    values = [
        [as_float32(v) for v in line.split()]
        for line in input_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not values or any(len(row) != 64 for row in values):
        raise ValueError("unexpected MLP testbench input dimensions")
    if original_inputs is not None and values != [
        [as_float32(v) for v in row] for row in original_inputs
    ]:
        raise ValueError("testbench floats differ from original inputs")
    rows = [[fixed.code(v) for v in row] for row in values]
    provenance = {"input_sha256": file_digest(input_file), "weights_sha256": {}}
    for layer, nin, nout, relu in ((2, 64, 64, True), (4, 64, 32, True), (6, 32, 4, False)):

        def load(name):
            path = firmware / name
            provenance["weights_sha256"][name] = file_digest(path)
            return [fixed.code(v) for v in path.read_text(encoding="utf-8").split(",") if v.strip()]

        weight, bias = load(f"w{layer}.txt"), load(f"b{layer}.txt")
        if len(weight) != nin * nout or len(bias) != nout:
            raise ValueError(f"unexpected size of Dense layer {layer}")
        # The qualified resource kernels store output-major weights. Each product
        # is narrowed to the accumulator grid. Wrap addition is modulo 2**W.
        blocks = [weight[k * nin : (k + 1) * nin] for k in range(nout)]
        rows = [
            [
                fixed.wrap(
                    b + sum((a * w) >> fixed.fractional for a, w in zip(row, ws, strict=True))
                )
                for b, ws in zip(bias, blocks, strict=True)
            ]
            for row in rows
        ]
        if relu:
            rows = [[max(0, v) for v in row] for row in rows]
    return rows, provenance
