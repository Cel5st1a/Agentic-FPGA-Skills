#!/usr/bin/env python
"""Reuse-factor tuning aid: locate the area-dominating layer and pick a legal factor.

Read-only analysis. Runs no EDA tool. Answers, before a 13-minute Vivado run is spent:

  * which layers have a reuse-factor lever, and what each one costs today,
  * the reuse factors hls4ml will actually accept, per layer,
  * which layer owns the area, and what changing its factor would buy,
  * the exact contract variant and commands to act on it.

Design rules (same as the rest of this skill tree):
  * Generic. No model name, layer name, shape or precision appears in this file.
    Everything comes from the deployment contract and the generated firmware.
  * Memoryless. Absolute paths on the command line; one JSON out.
  * No acceptance claim. This predicts; the RTL and the resource gate measure.
  * Read-only. It never writes into a run directory and never runs a tool.

Usage:
  python tune_reuse_factor.py --deployment <contract.yaml>
                             --parameters <p2-convert/project/firmware/parameters.h>
                             [--utilization <utilization_synth.rpt>]
                             [--out <report.json>]

Exit codes: 0 report written; 2 bad input; 3 nothing to tune.
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
    """Multiplier instances the kernel emits.

        rf <= n_in : ceil(MACs / rf)       (DenseResource_rf_leq_nin; rf is also the block)
        rf >  n_in : ceil(MACs / rf)       (DenseResource_rf_gt_nin_rem0) -- still /rf, not
                                           /n_in: the `MULT` block has one statement per
                                           reuse-loop step, and the generated
                                           `multiplier_limit = DIV_ROUNDUP(n_in*n_out, rf)`
                                           says the same thing. Measured on `mlp_64_32`'s
                                           32x4 `latent` at rf=64: 2 realised, and the config
                                           itself reads `DIV_ROUNDUP(128, 64)` = 2.

    So one expression covers both kernel variants, and it agrees with the generated
    `multiplier_limit` in every case checked.

    Measured exact for rf >= 2 on two independent models. At rf = 1 it is an UPPER BOUND:
    Vitis shares some multipliers (1024 predicted vs 426 realised on `asymmetric_cnn_fc`),
    so a prediction is never a result.
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
    generated limit wins and the prediction is wrong.
    """
    m = re.search(r"static\s+const\s+unsigned\s+multiplier_limit\s*=\s*([^;]+);", body)
    if not m:
        return None
    expr = m.group(1).strip()
    fields = {
        name: _unsigned(body, name)
        for name in ("n_in", "n_out", "reuse_factor", "n_zeros")
    }
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

    conv_structs = {name for _c, name, base, _b in structs if base.endswith("conv1d_config")}
    mult_names = {f"{name}_mult" for name in conv_structs}

    conv_records: dict[str, dict] = {}   # conv struct name -> record
    mult_records: dict[str, dict] = {}   # conv struct name -> its multiply block
    records: dict[str, dict] = {}        # final layer name -> record

    for comment, name, base, body in structs:
        n_in, n_out = _unsigned(body, "n_in"), _unsigned(body, "n_out")
        reuse_factor = _unsigned(body, "reuse_factor")

        if base.endswith("conv1d_config"):
            record = {
                "kind": "conv",
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
            block = {"struct": name, "n_in": n_in, "n_out": n_out,
                     "reuse_factor": reuse_factor, "kernel": kernel,
                     "emitted_multiplier_limit": emitted_multiplier_limit(body)}
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
def effective_factor(layer: str, contract: dict) -> int:
    """The factor a layer actually gets: its own override, else the contract's global."""
    overrides = contract.get("layer_reuse_factors") or {}
    if layer in overrides:
        return int(overrides[layer])
    return int(contract.get("reuse_factor"))


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
            return int(rec["filt_width"]) * int(rec["n_chan"]), int(rec["n_filt"])
        except (TypeError, ValueError):
            return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True,
                        help="the generated firmware/parameters.h of the run that blocked")
    parser.add_argument("--utilization", type=Path, default=None,
                        help="optional utilization_synth.rpt, to attribute the overage")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    contract_path, params_path = args.deployment.resolve(), args.parameters.resolve()
    for path in (contract_path, params_path):
        if not path.is_file():
            raise SystemExit(f"not a file: {path}")

    contract = read_yaml(contract_path)
    valid, provenance = hls4ml_valid_reuse_factors()
    layers = parse_parameters(params_path)
    if not layers:
        raise SystemExit(f"no lever-bearing layers found in {params_path}")
    limit = float((contract.get("constraints") or {}).get("resource_fraction_max", 0.8))

    rows = []
    for layer, rec in sorted(layers.items()):
        dims = lever_dims(rec)
        entry = {
            "layer": layer,
            "kind": rec["kind"],
            "struct": rec["struct"],
            "factor_in_firmware": rec.get("reuse_factor"),
            "factor_effective": effective_factor(layer, contract),
            "has_override": layer in (contract.get("layer_reuse_factors") or {}),
            "kernel": rec.get("kernel"),
        }
        if dims:
            n_in, n_out = dims
            rf = entry["factor_effective"]
            entry["multiplier_inputs"] = {"n_in": n_in, "n_out": n_out, "macs": n_in * n_out}
            entry["predicted_multipliers_now"] = multiplier_count(n_in, n_out, rf)
            entry["cycles_now"] = rf
            # Cross-check the prediction against the limit the generator itself emitted.
            emitted = rec.get("emitted_multiplier_limit")
            if emitted is not None:
                entry["emitted_multiplier_limit"] = emitted
                entry["prediction_agrees_with_firmware"] = emitted == entry["predicted_multipliers_now"]
            valid_set = valid_factors(n_in, n_out, valid)
            entry["valid_factors"] = valid_set
            entry["factor_now_is_valid"] = rf in valid_set
            candidates = [r for r in valid_set if r >= rf] or valid_set[-1:]
            picks = []
            for cand in candidates:
                pred = multiplier_count(n_in, n_out, cand)
                picks.append({"factor": cand, "predicted_multipliers": pred,
                              "cycles": cand,
                              "shrink_vs_now": (entry["predicted_multipliers_now"] / pred)
                              if pred else None})
            entry["candidates"] = picks
            # Recommend the smallest legal factor that already achieves the layer's MINIMUM
            # multiplier count. Rationale, all measured:
            #   * "the next factor up" is not a fix: on a 256x4 Dense the smallest increase is
            #     rf=2 -> 512 multipliers, built and measured at LUT 1.33x / DSP 2.89x, still
            #     no fit, while rf=256 -> 4 multipliers fits with ~6x headroom;
            #   * the largest legal factor is also not the answer: on that same layer it is
            #     rf=1024 -> 1 multiplier, which buys nothing over rf=256 (4 multipliers) and
            #     costs 4x the cycles. The gain vanishes once the count stops falling;
            #   * a purely relative target cannot find this, because whether the minimum is
            #     reached depends on the layer's shape, not on its current overage.
            # Recommend the smallest legal factor that reaches the layer's theoretical
            # minimum multiplier count, which is one multiplier per output (`n_out`): the
            # count is ceil(MACs / rf), so it stops falling once rf >= MACs / n_out.
            #
            # Derived rather than searched, and each edge is measured:
            #   * "the next factor up" is not a fix -- on a 256x4 Dense the smallest increase
            #     is rf=2 -> 512 multipliers (built: LUT 1.33x, DSP 2.89x, still no fit);
            #   * the largest legal factor is not the answer either -- rf=1024 -> 1 multiplier
            #     buys nothing over rf=256 -> 4 and costs 4x the cycles;
            #   * going below one-per-output is impossible, so capping at n_out is the floor.
            macs = entry["multiplier_inputs"]["macs"]
            needed = -(-macs // n_out)  # smallest rf reaching n_out multipliers
            entry["minimum_predicted_multipliers"] = n_out
            entry["factor_for_minimum_multipliers"] = needed
            at_or_above = [p["factor"] for p in picks if p["factor"] >= needed]
            if at_or_above:
                entry["recommended_factor"] = min(at_or_above)
                entry["recommendation_note"] = (
                    f"rf={needed} is the smallest factor reaching the layer's minimum of "
                    f"{n_out} multiplier(s) (one per output); larger factors reduce nothing "
                    "and only add cycles."
                )
            else:
                # No legal factor reaches the floor; take the largest legal one.
                entry["recommended_factor"] = picks[-1]["factor"]
                entry["recommendation_note"] = (
                    f"no legal factor reaches the {n_out}-multiplier floor for this shape; the "
                    "largest legal factor is proposed. If the design still does not fit, this "
                    "layer's lever is exhausted -- change io_type or precision (both need the "
                    "numerical gates re-earned), or conclude it does not fit the part."
                )
            # Flag a layer living on a factor inherited from the global, which is how a
            # Dense layer silently ends up fully parallel when the global is lowered for
            # some other layer's sake.
            if not entry["has_override"]:
                entry["factor_inherited_from_global"] = True
                entry["inheritance_warning"] = (
                    f"'{layer}' has no layer_reuse_factors entry, so it inherits the "
                    f"contract's global reuse_factor={contract.get('reuse_factor')}. If the "
                    "global factor was lowered to satisfy a different layer, this layer was "
                    "lowered with it."
                )
        rows.append(entry)

    ranked = sorted(rows, key=lambda r: r.get("predicted_multipliers_now", 0), reverse=True)
    top = ranked[0] if ranked and ranked[0].get("predicted_multipliers_now") else None

    report = {
        "schema_version": 1,
        "skill": "reuse-factor-resource-tuning",
        "deployment": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "parameters": {"path": str(params_path), "sha256": sha256_file(params_path)},
        "validity_predicate": provenance,
        "contract": {
            "precision": contract.get("precision"),
            "reuse_factor_global": contract.get("reuse_factor"),
            "layer_reuse_factors": contract.get("layer_reuse_factors") or {},
            "io_type": contract.get("io_type"),
            "resource_fraction_max": limit,
        },
        "layers": rows,
        "area_dominating_layer": top["layer"] if top else None,
        "numerical_effect": (
            "none expected in value, but the firmware changes, so convert and csim must be "
            "re-run on a NEW run directory and the verdict re-earned from the new evidence. "
            "Reuse factor can only change a result by REORDERING accumulator additions; "
            "factors that preserve the order are bit-identical, ones that interleave are not."
        ),
        "prediction_caveat": (
            "predicted_multipliers is exact for factor >= 2 and an upper bound at factor 1, "
            "where Vitis shares multipliers. The RTL and the resource gate are the "
            "authority; this only chooses what to try."
        ),
        "refusals": [
            "This tool never proposes lowering the contract's global reuse_factor.",
            "It never changes precision, resource_fraction_max, or the numerical criterion.",
            "It never writes into a run directory; act through make_contract_variant.py and a new run id.",
        ],
    }

    if top and top.get("recommended_factor") and top.get("has_override") is not None:
        overrides = dict(contract.get("layer_reuse_factors") or {})
        overrides[top["layer"]] = top["recommended_factor"]
        report["recommendation"] = {
            "layer": top["layer"],
            "factor_now": top["factor_effective"],
            "factor_proposed": top["recommended_factor"],
            "predicted_multipliers_now": top["predicted_multipliers_now"],
            "predicted_multipliers_after": multiplier_count(
                *lever_dims(layers[top["layer"]]), top["recommended_factor"]
            ),
            "layer_reuse_factors": overrides,
            "make_variant": (
                "python make_contract_variant.py --deployment <contract> --name <variant> "
                f"--set 'layer_reuse_factors={json.dumps(overrides)}'"
            ),
            "then": (
                "python bootstrap_run.py --deployment <variant contract> --run <NEW run id>; "
                "then run_line.py with convert,csim,csynth,wrapper,boardsim,synth,impl,"
                "bitstream,package"
            ),
            "verify_after": (
                "count `myproject_mul_` instantiations in p2-convert/project/myproject_prj/"
                "solution1/syn/verilog/ and compare with predicted_multipliers_after"
            ),
        }

    if args.utilization and args.utilization.is_file():
        report["utilization_input"] = {
            "path": str(args.utilization.resolve()),
            "sha256": sha256_file(args.utilization.resolve()),
            "note": "read-only context; the gate verdict belongs to vivado-manager",
        }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.out.resolve().write_text(text, encoding="utf-8")

    print(f"validity predicate: {provenance}")
    print(f"{'layer':22} {'kind':9} {'rf':>5} {'valid':>6} {'MACs':>7} {'mult now':>9}  recommended")
    for row in ranked:
        if "predicted_multipliers_now" not in row:
            continue
        print(f"{row['layer']:22} {row['kind']:9} {row['factor_effective']:>5} "
              f"{str(row['factor_now_is_valid']):>6} {row['multiplier_inputs']['macs']:>7} "
              f"{row['predicted_multipliers_now']:>9}  {row.get('recommended_factor')}")
    if top:
        rec = report.get("recommendation")
        print()
        print(f"area-dominating layer: {top['layer']}")
        if rec:
            print(f"  factor {rec['factor_now']} -> {rec['factor_proposed']}: "
                  f"predicted multipliers {rec['predicted_multipliers_now']} -> "
                  f"{rec['predicted_multipliers_after']}")
            print(f"  layer_reuse_factors: {json.dumps(rec['layer_reuse_factors'])}")
    print()
    print(report["prediction_caveat"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
