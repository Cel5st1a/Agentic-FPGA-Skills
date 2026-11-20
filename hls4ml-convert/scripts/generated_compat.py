"""Verified compatibility repair restricted to a newly generated hls4ml project."""

# ruff: noqa: E501
# Embedded C++ templates retain their emitted text for compatibility auditing.
import hashlib
import re

SOFTMAX_TEMPLATE_SHA256 = "2581cd0352d5df49fcd11902375c00e20da9562f7dac2a2137377de19bfe9e29"


def configure_softmax_exp_reads(project, version, factor):
    """Change only the stable Softmax table-read scheduling in a generated project.

    Opt-in, version/hash pinned, idempotent. No precision, table data or arithmetic
    changes. Resource savings and board behavior require separate measurements.
    """
    if isinstance(factor, bool) or not isinstance(factor, int) or factor <= 0:
        raise ValueError("softmax_exp_read_factor must be a positive integer")
    target = (project / "firmware/nnet_utils/nnet_activation.h").resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError("Softmax scheduling target must be inside the generated project")
    raw = target.read_bytes()
    pattern = (
        rb"(for \(unsigned i = 0; i < CONFIG_T::n_slice; i\+\+\) \{\r?\n"
        rb"        #pragma HLS unroll)(?: factor=(\d+))?"
        rb"(\r?\n        unsigned x = softmax_idx_from_real_val<typename CONFIG_T::inp_norm_t, "
        rb"CONFIG_T::exp_table_size>\(d_xi_xmax\[i\]\);)"
    )
    matches = list(re.finditer(pattern, raw))
    canonical = re.sub(pattern, rb"\1\3", raw)
    original_sha = hashlib.sha256(canonical).hexdigest()
    if version != "1.3.0" or len(matches) != 1 or original_sha != SOFTMAX_TEMPLATE_SHA256:
        raise ValueError("unrecognized stable Softmax template/version; no files changed")
    match = matches[0]
    prior = int(match.group(2)) if match.group(2) else None
    patched = re.sub(pattern, lambda m: m.group(1) + f" factor={factor}".encode() + m.group(3), raw)
    record = {
        "patch": "softmax-exp-read-scheduling",
        "factor": factor,
        "previous_factor": prior,
        "status": "ALREADY_APPLIED" if patched == raw else "APPLIED",
        "before_sha256": hashlib.sha256(raw).hexdigest(),
        "after_sha256": hashlib.sha256(patched).hexdigest(),
        "original_sha256": original_sha,
        "hardware_verified": False,
        "scope": "generated project only; exp table read pragma only",
    }
    if patched != raw:
        target.write_bytes(patched)
    return record


def sequential_dense_top(project, version, backend, io_type, name):
    """Remove the exact generated top pipeline pragma for an opt-in Dense design."""
    if version != "1.3.0" or backend.lower() != "vitis" or io_type != "io_parallel":
        raise ValueError("sequential top is supported for hls4ml 1.3.0 Vitis io_parallel only")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("unrecognized generated HLS top name")
    raw_path = project / "firmware" / f"{name}.cpp"
    path = raw_path.resolve()
    if raw_path.is_symlink() or not path.is_relative_to(project.resolve()) or not path.is_file():
        raise ValueError("sequential top target must be a generated project source")
    raw = path.read_bytes()
    opening = list(re.finditer(rb"(?m)^void[ \t]+" + name.encode() + rb"[ \t]*\(", raw))
    if len(opening) != 1:
        raise ValueError("unrecognized generated HLS top function; no files changed")
    start = opening[0].start()
    closing = re.search(rb"(?m)^}[ \t]*\r?$", raw[start:])
    if closing is None:
        raise ValueError("unrecognized generated HLS top function body; no files changed")
    end = start + closing.end()
    top = raw[start:end]
    if re.search(rb"(?mi)^[ \t]*#pragma[ \t]+HLS[ \t]+DATAFLOW\b", top):
        raise ValueError("sequential top expects a pipeline source; no files changed")
    directive = rb"(?m)^[ \t]*#pragma HLS PIPELINE[ \t]*\r?\n"
    if len(list(re.finditer(directive, top))) != 1:
        raise ValueError("unrecognized generated top PIPELINE pragma; no files changed")
    patched = raw[:start] + re.sub(directive, b"", top, count=1) + raw[end:]
    path.write_bytes(patched)
    return {
        "patch": "sequential-dense-top",
        "status": "APPLIED",
        "path": str(path),
        "before_sha256": hashlib.sha256(raw).hexdigest(),
        "after_sha256": hashlib.sha256(patched).hexdigest(),
        "scope": "single generated top PIPELINE pragma",
        "hardware_verified": False,
    }


BEFORE = "28bc325053f8458fc65e12c54a6b98aa4d9bfe8f8984836a6ddbb149bdd9a269"
AFTER = "79697a887ac1d5b5303211c7c22a3772f2b2efd432dd8b3ac13f337d329401bf"
OLD = (
    "    using r_T = ap_fixed<2 * (decltype(w_T::weight)::width + x_T::width), "
    "(decltype(w_T::weight)::width + x_T::width)>;"
)
NEW = """    // Shift amount is signed: |shift| <= 2**(Ww-1).  Keep that many integer
    // bits for a left shift and that many fractional bits for a right shift so
    // the product is exact; the original 2*(Ww+Wx) / (Ww+Wx) split truncated
    // every product below 2**-(Ww+Wx-Ix).
    static const int expo_width = decltype(w_T::weight)::width;
    static const int max_shift = 1 << (expo_width - 1);
    using r_T = ap_fixed<x_T::width + 2 * max_shift + 1, x_T::iwidth + max_shift + 1>;"""


def patch_po2_product(project, version):
    target = (project / "firmware/nnet_utils/nnet_mult.h").resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError("po2 compatibility target must be inside the generated project")
    raw = target.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    record = {
        "patch": "po2-product-width",
        "path": str(target),
        "hls4ml_version": version,
        "before_sha256": digest,
        "scope": "generated project only",
    }
    if version != "1.3.0" or digest not in {BEFORE, AFTER}:
        raise ValueError(
            f"unrecognized hls4ml po2 template/version: {version}, {digest}; no files changed"
        )
    if digest == AFTER:
        return dict(record, status="ALREADY_APPLIED", after_sha256=digest)
    patched = raw.replace(OLD.encode(), NEW.encode())
    after = hashlib.sha256(patched).hexdigest()
    if after != AFTER:
        raise ValueError("unrecognized po2 patch result; no files changed")
    target.write_bytes(patched)
    return dict(record, status="APPLIED", after_sha256=after)


def pin_output_grid(hls_model, precision):
    """Pin only resolved HLS graph outputs, after frontend/skip/fusion resolution."""
    outputs = hls_model.get_output_variables()
    if len(outputs) != 1:
        raise ValueError("board output-grid pin requires exactly one graph output")
    variable = outputs[0]
    before = str(variable.type.precision)
    from hls4ml.backends.fpga.fpga_types import APTypeConverter

    variable.type.precision = APTypeConverter().convert(
        hls_model.config.backend.convert_precision_string(precision)
    )
    producer = next(
        node.name for node in hls_model.get_layers() if hls_model.outputs[0] in node.outputs
    )
    config = hls_model.config.config["HLSConfig"]
    block = config.setdefault("LayerName", {}).setdefault(producer, {})
    previous = block.get("Precision", {})
    if not isinstance(previous, dict):
        previous = {"default": previous}
    block["Precision"] = dict(previous, result=precision)
    return {
        "output": hls_model.outputs[0],
        "before": before,
        "producer": producer,
        "after": str(variable.type.precision),
        "scope": "graph output only",
    }


# The stock hls4ml 1.3.0 softsign lookup table maps table index i to the logit
# x = 16*(i - N/2)/N, i.e. it only represents x in [-8, +8]; outside that window the
# index is clamped and the activation saturates at softsign(+-8) = +-0.888.  The
# table type and size are deployment-configurable, but the +-8 input window is
# hard-coded in the template.  A deployment whose logits exceed +-8 therefore needs
# this verified, range-parameterised repair: the sampled function is unchanged
# (softsign(x) = x/(|x|+1)), only the index map is widened to [-R, +R].  The repair
# is scoped to the softsign block: other activations share the same literal.


def patch_activation_table_range(project, version, activation, table_range):
    """Widen a generated lookup-table activation's input range; version/hash pinned.

    Only the index map of the named activation is rewritten.  The table values are
    still produced by the stock init function at the stock sample points, and the
    table size/type still come from the deployment config (TableSize / Table_t).
    Idempotent: a second application reports ALREADY_APPLIED.
    """
    if activation != "softsign":
        raise ValueError(f"unsupported activation table range patch: {activation!r}")
    if isinstance(table_range, bool) or not isinstance(table_range, int) or table_range < 8:
        raise ValueError("activation_table_range must be an integer >= 8")
    target = (project / "firmware/nnet_utils/nnet_activation.h").resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError("activation table target must be inside the generated project")
    if version != "1.3.0":
        raise ValueError(f"unrecognized hls4ml version for activation table patch: {version}")
    raw = target.read_text(encoding="utf-8")
    start_marker = "inline float softsign_fcn_float"
    end_marker = "// *************************************************\n//       ELU Activation"
    if raw.count(start_marker) != 1 or raw.count(end_marker) != 1:
        raise ValueError("unrecognized softsign/ELU block markers; no files changed")
    start = raw.index(start_marker)
    end = raw.index(end_marker, start)
    block = raw[start:end]
    span = 2 * table_range
    old_init = "float in_val = 2 * 8.0 * (ii - float(N_TABLE) / 2.0) / float(N_TABLE);"
    new_init = f"float in_val = 2 * {table_range}.0 * (ii - float(N_TABLE) / 2.0) / float(N_TABLE);"
    old_comment = (
        "        // First, convert from table index to X-value (signed 8-bit, range -8 to +8)"
    )
    new_comment = f"        // First, convert from table index to X-value (range -{table_range} to +{table_range})"
    old_index = (
        "        data_round = data[ii] * CONFIG_T::table_size / 16;\n"
        "        index = data_round + 8 * CONFIG_T::table_size / 16;"
    )
    new_index = (
        f"        data_round = data[ii] * CONFIG_T::table_size / {span};\n"
        f"        index = data_round + {table_range} * CONFIG_T::table_size / {span};"
    )
    before = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    record = {
        "patch": "activation-table-range",
        "activation": activation,
        "table_range": table_range,
        "path": str(target),
        "hls4ml_version": version,
        "before_sha256": before,
        "scope": "generated softsign lookup-table index map only; function and sampled values unchanged",
        "hardware_verified": False,
    }
    if new_index in block and new_init in block:
        return dict(record, status="ALREADY_APPLIED", after_sha256=before)
    for name, needle in (("init", old_init), ("comment", old_comment), ("index", old_index)):
        if block.count(needle) != 1:
            raise ValueError(
                f"unrecognized generated {activation} {name} pattern "
                f"(count={block.count(needle)}); no files changed"
            )
    patched_block = (
        block.replace(old_init, new_init)
        .replace(old_comment, new_comment)
        .replace(old_index, new_index)
    )
    patched = raw[:start] + patched_block + raw[end:]
    after = hashlib.sha256(patched.encode("utf-8")).hexdigest()
    target.write_text(patched, encoding="utf-8")
    return dict(record, status="APPLIED", after_sha256=after)


def patch_stream_activations(project, version, activations):
    """Keep stream ELU/softsign scalar arithmetic identical to repaired array kernels."""
    if version != "1.3.0" or not set(activations) <= {"elu", "softsign"}:
        raise ValueError("unsupported stream activation repair")
    target = project / "firmware/nnet_utils/nnet_activation_stream.h"
    raw = target.read_text(encoding="utf8")
    patched = raw
    for name in sorted(set(activations)):
        if f"AFD-STREAM-SCALAR-{name.upper()}" in patched:
            continue
        signature = (
            "template <class data_T, class param_T, class res_T, typename CONFIG_T>\n"
            "void elu(hls::stream<data_T> &data, param_T alpha, hls::stream<res_T> &res) {"
            if name == "elu"
            else "template <class data_T, class res_T, typename CONFIG_T> "
            "void softsign(hls::stream<data_T> &data, hls::stream<res_T> &res) {"
        )
        if patched.count(signature) != 1:
            raise ValueError(f"unrecognized stream {name} signature")
        start = patched.index(signature)
        opened = patched.index("{", start)
        depth, end = 1, opened + 1
        while depth:
            depth += (patched[end] == "{") - (patched[end] == "}")
            end += 1
        call = (
            "elu<typename data_T::value_type, param_T, typename res_T::value_type, "
            "afd_stream_scalar_config<CONFIG_T>>(source, alpha, result);"
            if name == "elu"
            else "softsign<typename data_T::value_type, typename res_T::value_type, "
            "afd_stream_scalar_config<CONFIG_T>>(source, result);"
        )
        body = f"""\n    // AFD-STREAM-SCALAR-{name.upper()}: reuse corrected scalar activation arithmetic.
    static_assert(data_T::size == res_T::size, "activation pack width differs");
    for (unsigned i = 0; i < CONFIG_T::n_in / data_T::size; ++i) {{
        #pragma HLS PIPELINE
        data_T incoming = data.read();
        res_T outgoing;
        for (unsigned j = 0; j < data_T::size; ++j) {{
            #pragma HLS UNROLL
            typename data_T::value_type source[1] = {{incoming[j]}};
            typename res_T::value_type result[1];
            {call}
            outgoing[j] = result[0];
        }}
        res.write(outgoing);
    }}
}}"""
        patched = patched[:start] + signature + body + patched[end:]
    if "struct afd_stream_scalar_config" not in patched:
        patched = patched.replace(
            "namespace nnet {",
            """namespace nnet {
template<class C> struct afd_stream_scalar_config : C {
    static const unsigned n_in = 1;
    static const unsigned io_type = nnet::io_parallel;
};
""",
            1,
        )
    target.write_text(patched, encoding="utf8")
    return {
        "patch": "stream-activation-scalar-parity",
        "path": str(target),
        "status": "APPLIED" if patched != raw else "ALREADY_APPLIED",
        "before_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(patched.encode()).hexdigest(),
        "hardware_verified": False,
    }


# ---------------------------------------------------------------- interpolation
# A sampled lookup table has two error sources: the index map truncation (one
# table interval, e.g. 8/1024 for ELU) and the table value quantization.  The
# stock generated activations use the nearest lower sample with no interpolation,
# so the index error directly becomes an activation error.  This repair keeps the
# same sampled table and adds one linear interpolation step, reducing the index
# error to the interpolation residual (h^2/8 * |f''|), which for ELU on [-8, 0]
# and softsign on [-R, R] is orders of magnitude below the output LSB.  It is
# version pinned and restricted to the named activation function body.


def _replace_function_block(text, start_marker, end_marker, new_block):
    if text.count(start_marker) != 1:
        raise ValueError(f"unrecognized activation function marker x{text.count(start_marker)}")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[:start] + new_block + text[end:]


def patch_elu_interpolation(project, version):
    """Replace the generated ELU table lookup with a table + linear interpolation body."""
    target = (project / "firmware/nnet_utils/nnet_activation.h").resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError("ELU interpolation target must be inside the generated project")
    if version != "1.3.0":
        raise ValueError(f"unrecognized hls4ml version for ELU interpolation: {version}")
    raw = target.read_text(encoding="utf-8")
    record = {
        "patch": "activation-linear-interpolation",
        "activation": "elu",
        "path": str(target),
        "hls4ml_version": version,
        "before_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "scope": "generated ELU negative-branch lookup only; sampled table unchanged",
        "hardware_verified": False,
    }
    if "// AFD-INTERPOLATED-ELU" in raw:
        return dict(record, status="ALREADY_APPLIED", after_sha256=record["before_sha256"])
    start_marker = (
        "template <class data_T, class param_T, class res_T, typename CONFIG_T>\nvoid elu("
    )
    end_marker = "\ntemplate <class data_T, class res_T, typename CONFIG_T> void elu("
    new_block = """template <class data_T, class param_T, class res_T, typename CONFIG_T>
void elu(data_T data[CONFIG_T::n_in], const param_T alpha, res_T res[CONFIG_T::n_in]) {
    // AFD-INTERPOLATED-ELU: stock sampled table over [-8, 0] plus one linear
    // interpolation step; the positive branch stays exact.  Requires
    // table_size % 8 == 0 (declared by the deployment).
    typedef ap_fixed<32, 16> elu_work_t;
#ifdef __HLS_SYN__
    bool initialized = false;
    typename CONFIG_T::table_t elu_table[CONFIG_T::table_size];
#else
    static bool initialized = false;
    static typename CONFIG_T::table_t elu_table[CONFIG_T::table_size];
#endif
    if (!initialized) {
        init_elu_table<CONFIG_T, CONFIG_T::table_size>(elu_table);
        initialized = true;
    }

    #pragma HLS PIPELINE

    for (int ii = 0; ii < CONFIG_T::n_in; ii++) {
        data_T datareg = data[ii];
        if (datareg >= 0) {
            res[ii] = (res_T)datareg;
        } else {
            elu_work_t mag = (elu_work_t)(-datareg);
            // The sampled table covers [-8, 0]: clamp before scaling so the work
            // type cannot wrap for BN outputs far outside the table range.
            if (mag > (elu_work_t)8) mag = (elu_work_t)8;
            elu_work_t scaled = mag * (CONFIG_T::table_size / 8);
            int index = (int)scaled;
            elu_work_t frac = scaled - (elu_work_t)index;
            if (index < 0) {
                index = 0;
                frac = 0;
            }
            if (index > CONFIG_T::table_size - 2) {
                index = CONFIG_T::table_size - 2;
                frac = 1;
            }
            typename CONFIG_T::table_t y0 = elu_table[index];
            typename CONFIG_T::table_t y1 = elu_table[index + 1];
            typename CONFIG_T::table_t dy = (typename CONFIG_T::table_t)(y1 - y0);
            typename CONFIG_T::table_t y = (typename CONFIG_T::table_t)(y0 + (typename CONFIG_T::table_t)(dy * frac));
            res[ii] = alpha * y;
        }
    }
}
"""
    patched = _replace_function_block(raw, start_marker, end_marker, new_block)
    after = hashlib.sha256(patched.encode("utf-8")).hexdigest()
    target.write_text(patched, encoding="utf-8")
    return dict(record, status="APPLIED", after_sha256=after)


def patch_softsign_interpolation(project, version, table_range):
    """Replace the generated softsign table lookup with a table + linear interpolation body.

    The range-widened init function must already be applied; the body reads the
    same sampled table and samples softsign over [-table_range, +table_range].
    """
    if isinstance(table_range, bool) or not isinstance(table_range, int) or table_range < 8:
        raise ValueError("softsign interpolation table_range must be an integer >= 8")
    target = (project / "firmware/nnet_utils/nnet_activation.h").resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError("softsign interpolation target must be inside the generated project")
    if version != "1.3.0":
        raise ValueError(f"unrecognized hls4ml version for softsign interpolation: {version}")
    raw = target.read_text(encoding="utf-8")
    record = {
        "patch": "activation-linear-interpolation",
        "activation": "softsign",
        "table_range": table_range,
        "path": str(target),
        "hls4ml_version": version,
        "before_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "scope": "generated softsign lookup only; sampled table unchanged",
        "hardware_verified": False,
    }
    if "// AFD-INTERPOLATED-SOFTSIGN" in raw:
        return dict(record, status="ALREADY_APPLIED", after_sha256=record["before_sha256"])
    span = 2 * table_range
    start_marker = (
        "template <class data_T, class res_T, typename CONFIG_T>\n"
        "void softsign(data_T data[CONFIG_T::n_in], res_T res[CONFIG_T::n_in]) {"
    )
    end_marker = "\n// *************************************************\n//       ELU Activation"
    new_block = f"""template <class data_T, class res_T, typename CONFIG_T>
void softsign(data_T data[CONFIG_T::n_in], res_T res[CONFIG_T::n_in]) {{
    // AFD-INTERPOLATED-SOFTSIGN: stock sampled table over [-{table_range}, +{table_range}] plus
    // one linear interpolation step.  Requires (2*table_range) | table_size.
    typedef ap_fixed<32, 16> softsign_work_t;
#ifdef __HLS_SYN__
    bool initialized = false;
    typename CONFIG_T::table_t softsign_table[CONFIG_T::table_size];
#else
    static bool initialized = false;
    static typename CONFIG_T::table_t softsign_table[CONFIG_T::table_size];
#endif
    if (!initialized) {{
        init_softsign_table<CONFIG_T, CONFIG_T::table_size>(softsign_table);
        initialized = true;
    }}

    #pragma HLS PIPELINE

    for (int ii = 0; ii < CONFIG_T::n_in; ii++) {{
        softsign_work_t offset = (softsign_work_t)data[ii] + (softsign_work_t){table_range};
        softsign_work_t scaled = offset * (CONFIG_T::table_size / {span});
        int index = (int)scaled;
        softsign_work_t frac = scaled - (softsign_work_t)index;
        if (index < 0) {{
            index = 0;
            frac = 0;
        }}
        if (index > CONFIG_T::table_size - 2) {{
            index = CONFIG_T::table_size - 2;
            frac = 1;
        }}
        typename CONFIG_T::table_t y0 = softsign_table[index];
        typename CONFIG_T::table_t y1 = softsign_table[index + 1];
        typename CONFIG_T::table_t dy = (typename CONFIG_T::table_t)(y1 - y0);
        res[ii] = (res_T)(y0 + (typename CONFIG_T::table_t)(dy * frac));
    }}
}}
"""
    patched = _replace_function_block(raw, start_marker, end_marker, new_block)
    after = hashlib.sha256(patched.encode("utf-8")).hexdigest()
    target.write_text(patched, encoding="utf-8")
    return dict(record, status="APPLIED", after_sha256=after)
