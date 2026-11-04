#!/usr/bin/env python
"""Read actual generated Dense/Conv reuse factors and complete Softmax scheduling knobs.

No EDA, contract changes, measured-resource claims, or mandatory full-pipeline reruns.
Reports are immutable attempts; all estimates are explicitly separate from measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import yaml

#: Kernel aliases that carry a per-layer reuse-factor lever, mapped to how their parallel
#: multiplier count is derived. Both come from the shipped `nnet_utils` headers:
#:   * `DenseResource_rf_leq_nin`  -> ceil(n_in*n_out / rf)      (rf <= n_in)
#:   * `DenseResource_rf_gt_nin_rem0` -> ceil(n_in*n_out / n_in)  (rf > n_in)
#: The convolution kernels use the same `multiplier_limit` expression over their own
#: (kernel_size * n_chan) x n_filt product, which the generated `*_mult` struct records.
KERNEL_LEQ_NIN = "DenseResource_rf_leq_nin"
KERNEL_GT_NIN = "DenseResource_rf_gt_nin_rem0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------------------
# Validity. This is hls4ml's own predicate, not a re-derivation. When the installed
# package is importable we call it directly; otherwise we reproduce it verbatim and say so
# in the report, because a second copy of a criterion is exactly the kind of drift this
# tree exists to avoid.
# --------------------------------------------------------------------------------------
def hls4ml_valid_reuse_factors():
    """Return (callable, provenance). Raises nothing; provenance records the source."""
    try:
        from hls4ml.backends.fpga.fpga_backend import FPGABackend

        def valid(n_in: int, n_out: int, rf: int) -> bool:
            # `_validate_reuse_factor` is a pure function of its three arguments (it reads
            # no instance state), so it is called unbound with self=None rather than
            # instantiating a backend just to ask a question about arithmetic.
            return bool(FPGABackend._validate_reuse_factor(None, n_in, n_out, rf))

        import hls4ml

        return valid, f"hls4ml {hls4ml.__version__} FPGABackend._validate_reuse_factor"
    except Exception as exc:  # pragma: no cover - fallback path

        def valid(n_in: int, n_out: int, rf: int) -> bool:
            multfactor = min(n_in, rf)
            multiplier_limit = int(math.ceil((n_in * n_out) / float(multfactor)))
            a = ((multiplier_limit % n_out) == 0) or (rf >= n_in)
            a = a and (((rf % n_in) == 0) or (rf < n_in))
            return bool(a and (((n_in * n_out) % rf) == 0))

        return valid, (
            "reproduced copy of hls4ml's FPGABackend._validate_reuse_factor "
            f"(import failed: {type(exc).__name__}: {exc})"
        )


def valid_factors(n_in: int, n_out: int, valid) -> list[int]:
    return [rf for rf in range(1, n_in * n_out + 1) if valid(n_in, n_out, rf)]


def multiplier_count(n_in: int, n_out: int, rf: int) -> int:
    """Dense/Conv arithmetic-model estimate ceil(MACs/RF), not a physical count.

    Compare with emitted_multiplier_limit where available; zero-weight removal,
    backend sharing and mapping can change the actual hardware. RF is not latency.
    """
    return int(math.ceil((n_in * n_out) / float(rf)))


# --------------------------------------------------------------------------------------
# Parse the generated firmware. The writer emits the layer name as a comment immediately
# before each parameter struct, which is the only place the mapping is recorded without
# loading TensorFlow.
# --------------------------------------------------------------------------------------
STRUCT_HEAD_RE = re.compile(r"^\s*struct\s+(?P<name>\w+)\s*:\s*(?P<base>[\w:]+)\s*\{")
KERNEL_RE = re.compile(r"using\s+kernel\s*=\s*nnet::(\w+)\s*<")


def _unsigned(body: str, key: str):
    m = re.search(rf"static\s+const\s+unsigned\s+{key}\s*=\s*([^;]+);", body)
    if not m:
        return None
    expr = m.group(1).strip()
    try:
        return int(expr)
    except ValueError:
        return expr  # keep the expression; it is still evidence


def emitted_multiplier_limit(body: str):
    """`multiplier_limit` as the generator wrote it, evaluated with the struct's own fields.

    The generated expression is `DIV_ROUNDUP(n_in * n_out, reuse_factor) - n_zeros /
    reuse_factor`, kept as an unevaluated macro expression, so it is evaluated here against
    the struct's own integers. This is the firmware's own statement of how many parallel
    multipliers the kernel is built with, which makes it an independent check on
    `multiplier_count` rather than a second copy of it: if the two ever disagree, the
    generated expression wins as a declaration. Neither value proves physical sharing;
    the selected kernel and synthesized mapping still govern that claim.
    """
    m = re.search(r"static\s+const\s+unsigned\s+multiplier_limit\s*=\s*([^;]+);", body)
    if not m:
        return None
    expression = re.sub(r"\s+", "", m.group(1))
    if expression.isdigit():
        return int(expression)
    if expression != "DIV_ROUNDUP(n_in*n_out,reuse_factor)-n_zeros/reuse_factor":
        return None
    fields = {name: _unsigned(body, name) for name in ("n_in", "n_out", "reuse_factor", "n_zeros")}
    if any(not isinstance(value, int) for value in fields.values()):
        return None
    if fields["reuse_factor"] <= 0:
        return None
    div_roundup = lambda a, b: -(-a // b)  # noqa: E731
    value = div_roundup(fields["n_in"] * fields["n_out"], fields["reuse_factor"])
    value -= fields["n_zeros"] // fields["reuse_factor"]
    return value


def _iter_structs(text: str):
    """Yield (layer_comment, struct_name, base, body) for each `struct X : base { ... };`.

    Scanned line by line rather than with one dot-matches-all regex: the first version used
    an optional comment group with `re.S`, which let the comment skip over a whole struct
    and swallow the next one, so `encoder_conv_0` was reported twice (once under its layer
    name and once under its struct name). The generated file has one struct per block and
    the layer name on the line above it, so a line-wise read is both simpler and correct.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        head = STRUCT_HEAD_RE.match(lines[index])
        if not head:
            index += 1
            continue
        # The layer name is the nearest preceding non-blank line, when it is a comment.
        comment = ""
        for back in range(index - 1, max(index - 4, -1), -1):
            stripped = lines[back].strip()
            if not stripped:
                continue
            if stripped.startswith("//"):
                comment = stripped.lstrip("/").strip()
            break
        body_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip() != "};":
            body_lines.append(lines[index])
            index += 1
        yield comment, head.group("name"), head.group("base"), "\n".join(body_lines)
        index += 1


def parse_parameters(path: Path) -> dict:
    """Layer name -> lever record, for every layer that carries a reuse-factor lever.

    The generated header describes a convolution with two structs:

        // encoder_conv_0
        struct config3_mult : nnet::dense_config { ... n_in=5, n_out=8, reuse_factor=5 ... };
        struct config3 : nnet::conv1d_config { ... n_chan=1, n_filt=8, reuse_factor=5 ... };

    The `conv1d_config` struct is the layer; the `<conv struct>_mult` struct is its multiply
    block and carries the factor the kernel actually uses. Note both facts that make naive
    classification fail:

      * `config3_mult` is named after its **struct** (`config3`), not the layer
        (`encoder_conv_0`), so the two correlate by struct name -- correlating by layer name
        reported each convolution twice; and
      * `config3_mult` **carries a layer comment too**, so "has a comment" does not mean
        "is a Dense layer" -- an earlier version used that test and misfiled every conv's
        multiply block as a separate Dense layer.

    So classification is structural, not comment-based: the set of convolution structs is
    established first, and only then is each struct named `<conv>_mult` recognised as that
    conv's multiply block. Whatever remains is a genuine Dense layer.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    structs = list(_iter_structs(text))

    conv_structs = {
        name for _c, name, base, _b in structs if base.endswith(("conv1d_config", "conv2d_config"))
    }
    mult_names = {f"{name}_mult" for name in conv_structs}

    conv_records: dict[str, dict] = {}  # conv struct name -> record
    mult_records: dict[str, dict] = {}  # conv struct name -> its multiply block
    records: dict[str, dict] = {}  # final layer name -> record

    for comment, name, base, body in structs:
        n_in, n_out = _unsigned(body, "n_in"), _unsigned(body, "n_out")
        reuse_factor = _unsigned(body, "reuse_factor")

        if base.endswith(("conv1d_config", "conv2d_config")):
            record = {
                "kind": "conv",
                "spatial_dimensions": 2 if base.endswith("conv2d_config") else 1,
                "filt_height": _unsigned(body, "filt_height"),
                "layer": comment or name,
                "struct": name,
                "reuse_factor": reuse_factor,
                "n_chan": _unsigned(body, "n_chan"),
                "n_filt": _unsigned(body, "n_filt"),
                "filt_width": _unsigned(body, "filt_width"),
            }
            if record["n_chan"] is None or record["n_filt"] is None:
                continue
            conv_records[name] = record
            continue

        if base.endswith("dense_config") and n_in is not None and n_out is not None:
            kernel_m = KERNEL_RE.search(body)
            kernel = kernel_m.group(1) if kernel_m else None
            block = {
                "struct": name,
                "n_in": n_in,
                "n_out": n_out,
                "reuse_factor": reuse_factor,
                "kernel": kernel,
                "emitted_multiplier_limit": emitted_multiplier_limit(body),
            }
            if name in mult_names:
                # The multiply block's comment carries the real HLS layer name
                # (`// encoder_conv_0` above `struct config3_mult`), while the conv's own
                # `conv1d_config` struct in this firmware has no comment at all -- so the
                # name must be taken from the block, not from the conv struct.
                mult_records[name[: -len("_mult")]] = {**block, "layer": comment or None}
            else:
                records[comment or name] = {**block, "kind": "dense", "layer": comment or name}

    for struct_name, rec in conv_records.items():
        mult = mult_records.get(struct_name)
        if mult:
            if mult.get("layer"):
                rec["layer"] = mult["layer"]
            rec["mult_n_in"] = mult["n_in"]
            rec["mult_n_out"] = mult["n_out"]
            rec["mult_reuse_factor"] = mult.get("reuse_factor")
            rec["emitted_multiplier_limit"] = mult.get("emitted_multiplier_limit")
            rec["kernel"] = mult.get("kernel")
            if rec.get("reuse_factor") is None:
                rec["reuse_factor"] = mult.get("reuse_factor")
        records.setdefault(rec["layer"], rec)
    return records


# --------------------------------------------------------------------------------------
def factor_request(layer: str, contract: dict) -> dict:
    """Resolve explicit requests, not emitted values; unknown LayerType is never guessed."""

    def result(value, source, conflict=False):
        valid = type(value) is int and value > 0
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            value, valid = int(value), True
        return {
            "value": value if valid and not conflict else None,
            "source": source,
            "conflict": conflict,
        }

    def without_nulls(value):
        if isinstance(value, dict):
            return {k: without_nulls(v) for k, v in value.items() if v is not None}
        return value

    config = without_nulls(contract.get("hls_config") or {})
    overrides = contract.get("layer_reuse_factors") or {}
    if not isinstance(config, dict) or not isinstance(overrides, dict):
        return result(None, "invalid_mapping")
    layers = config.get("LayerName") or {}
    if not isinstance(layers, dict):
        return result(None, "invalid_mapping")
    own = [v for k, v in overrides.items() if str(k).lower() == layer.lower()]
    named = [v for k, v in layers.items() if str(k).lower() == layer.lower()]
    if len(own) > 1 or len(named) > 1:
        return result(None, "ambiguous_layer", True)
    block = named[0] if named else {}
    if not isinstance(block, dict):
        return result(None, "invalid_mapping")
    named_rf = block.get("ReuseFactor")
    if own and named_rf is not None and own[0] != named_rf:
        return result(None, "conflicting_layer_requests", True)
    if own:
        return result(own[0], "layer_reuse_factors")
    if named_rf is not None:
        return result(named_rf, "hls_config.LayerName")
    types = config.get("LayerType") or {}
    if not isinstance(types, dict):
        return result(None, "invalid_mapping")
    if any(isinstance(v, dict) and "ReuseFactor" in v for v in types.values()):
        return result(None, "unknown_layer_type")
    model = config.get("Model") or {}
    if not isinstance(model, dict):
        return result(None, "invalid_mapping")
    global_rf = contract.get("reuse_factor")
    if "ReuseFactor" in model and model["ReuseFactor"] != global_rf:
        return result(None, "conflicting_global_requests", True)
    return result(global_rf, "global")


def effective_factor(layer: str, contract: dict):
    return factor_request(layer, contract)["value"]


def lever_dims(rec: dict):
    """(n_in, n_out) of the multiply block whose count the factor controls, or None.

    For a Dense layer this is the layer's own (n_in, n_out). For a convolution it is the
    emitted `*_mult` block: `kernel_size * n_chan` by `n_filt`, read from the sibling
    struct when present and rebuilt from the conv's own fields otherwise.
    """
    if rec["kind"] == "dense":
        if rec.get("n_in") is None or rec.get("n_out") is None:
            return None
        return int(rec["n_in"]), int(rec["n_out"])
    if rec["kind"] == "conv":
        if rec.get("mult_n_in") is not None and rec.get("mult_n_out") is not None:
            return int(rec["mult_n_in"]), int(rec["mult_n_out"])
        try:
            height = int(rec["filt_height"]) if rec.get("spatial_dimensions", 1) == 2 else 1
            return height * int(rec["filt_width"]) * int(rec["n_chan"]), int(rec["n_filt"])
        except (TypeError, ValueError):
            return None
    return None


def candidate_costs(n_in: int, n_out: int, factors: list[int]) -> list[dict]:
    """Arithmetic estimates only; reuse factors are not measured latency."""
    return [
        {
            "reuse_factor": rf,
            "predicted_multipliers": multiplier_count(n_in, n_out, rf),
            "measured_latency_cycles": None,
            "measured_resources": None,
        }
        for rf in factors
    ]


def sharing_diagnostic(rec: dict) -> dict:
    kernel = rec.get("kernel")
    applicable = None
    if kernel == "DenseLatency":
        applicable = False
    elif kernel in (KERNEL_LEQ_NIN, KERNEL_GT_NIN):
        applicable = True
    return {
        "kernel": kernel,
        "rf_sharing_estimate_applicable": applicable,
        "measured_multiplier_count": None,
        "note": "DenseLatency does not establish Resource-kernel sharing from RF alone. "
        "Inspect emitted strategy/kernel, HLS schedule and actual synthesized mapping before "
        "repeating implementation. Unknown kernels remain unknown; even a Resource kernel "
        "does not make arithmetic estimates physical measurements.",
    }


def softmax_scheduling(path: Path) -> list[dict]:
    records = []
    for comment, name, _base, body in _iter_structs(path.read_text(encoding="utf-8")):
        if "softmax" not in name.lower() and "softmax_implementation" not in body:
            continue
        lanes = _unsigned(body, "n_in")
        factors = [None, 1]
        factors += [f for f in (2, 4) if isinstance(lanes, int) and f <= lanes]
        records.append(
            {
                "layer": comment or name,
                "struct": name,
                "elements": lanes,
                "exp_table_size": _unsigned(body, "exp_table_size"),
                "inv_table_size": _unsigned(body, "inv_table_size"),
                "recommended_factor": None,
                "choices": [{"softmax_exp_read_factor": factor} for factor in factors],
                "measured_resources": None,
                "measured_latency_cycles": None,
                "note": "None preserves current scheduling. Factor 1 limits exponent-table read "
                "unrolling; finite factors trade parallel reads for scheduling. Converter "
                "support is checked before patching. Keep complete exp/sum/normalization; "
                "measure each candidate before selecting. No universal fastest factor.",
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only RF and full-Softmax scheduling advice")
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--utilization", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    contract_path, params_path = args.deployment.resolve(), args.parameters.resolve()
    for path in (contract_path, params_path):
        if not path.is_file():
            raise SystemExit(f"not a file: {path}")
    if args.out and args.out.exists():
        raise SystemExit("advice output exists; use a fresh attempt path")
    contract = read_yaml(contract_path)
    valid, provenance = hls4ml_valid_reuse_factors()
    layers, scheduling = parse_parameters(params_path), softmax_scheduling(params_path)
    if not layers and not scheduling:
        raise SystemExit("no supported Dense/Conv reuse or Softmax scheduling knobs found")
    rows = []
    for layer, rec in sorted(layers.items()):
        dims = lever_dims(rec)
        requested = effective_factor(layer, contract)
        actual = rec.get("mult_reuse_factor", rec.get("reuse_factor"))
        entry = {
            "layer": layer,
            "kind": rec["kind"],
            "struct": rec["struct"],
            "sharing_diagnostic": sharing_diagnostic(rec),
            "requested_factor": requested,
            "requested_factor_resolution": factor_request(layer, contract),
            "factor_in_firmware": actual,
            "request_matches_firmware": actual == requested if requested is not None else None,
            "measured_latency_cycles": None,
            "measured_resources": None,
        }
        if factor_request(layer, contract)["source"] == "global":
            entry["factor_inherited_from_global"] = True
        if dims and isinstance(actual, int) and actual > 0:
            n_in, n_out = dims
            factors = valid_factors(n_in, n_out, valid)
            picks = candidate_costs(n_in, n_out, factors)
            entry.update(
                multiplier_inputs={"n_in": n_in, "n_out": n_out},
                valid_factors=factors,
                candidates=picks,
                predicted_multipliers_now=(
                    None
                    if sharing_diagnostic(rec)["rf_sharing_estimate_applicable"] is False
                    else multiplier_count(n_in, n_out, actual)
                ),
                candidate_scope="hypothetical arithmetic model, not measured physical sharing",
                emitted_multiplier_limit=rec.get("emitted_multiplier_limit"),
                minimum_predicted_multipliers=min(
                    (x["predicted_multipliers"] for x in picks), default=None
                ),
                recommended_factor=None,
            )
        rows.append(entry)
    report = {
        "schema_version": 2,
        "skill": "reuse-factor-resource-tuning",
        "eda_executed": False,
        "hardware_verified": False,
        "deployment": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "parameters": {"path": str(params_path), "sha256": sha256_file(params_path)},
        "validity_predicate": provenance,
        "contract": {
            "reuse_factor_global": contract.get("reuse_factor"),
            "precision": contract.get("precision"),
            "resource_fraction_max": (contract.get("constraints") or {}).get(
                "resource_fraction_max"
            ),
        },
        "refusals": [
            "This read-only tool never proposes lowering the global factor.",
            "It never changes precision, resource_fraction_max, or numerical acceptance.",
        ],
        "layers": rows,
        "softmax_scheduling": scheduling,
        "measured_resources": None,
        "measured_latency_cycles": None,
        "prediction_caveat": "Multiplier estimates describe a kernel arithmetic model, "
        "not physical resources or capacity. Backend sharing, zero weights and scheduling "
        "can alter mapping. RF is not measured latency. No automatic winner is selected.",
        "acceptance": "Complete original model, 256 complete board outputs, original Python "
        "abs(error)<=.001+.001*abs(ref). Resource/timing/HLS/cosim are diagnostics.",
        "next_action": "Only if a change is needed: keep existing attempts, change one knob, "
        "and regenerate its affected artifacts. Keep a valid core for shell-only fixes. "
        "Do not rerun accepted deployments for optional measurements.",
    }
    if args.utilization:
        if not args.utilization.is_file():
            raise SystemExit(f"not a file: {args.utilization}")
        report["utilization_input"] = {
            "path": str(args.utilization.resolve()),
            "sha256": sha256_file(args.utilization),
            "note": "raw diagnostic context only",
        }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
