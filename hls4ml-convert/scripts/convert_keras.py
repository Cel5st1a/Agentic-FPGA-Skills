#!/usr/bin/env python
"""Path A of the hls4ml-convert skill: trained Keras model -> hls4ml HLS project.

This script is the executable form of the step that `hls4ml-convert/SKILL.md`
documents as Path A. It exists because the SKILL.md references
`scripts/convert_keras.py` and that file was absent, so a memoryless agent could
not execute the skill.

Design rules (operator principles, 2026-09-27):
  * Generic: every deployment parameter comes from the deployment contract; the
    hls4ml config is built from this skill's own template. No model name, shape or
    precision is hardcoded here.
  * Memoryless: one command, absolute paths, all inputs named on the command line,
    and a machine-readable evidence file written next to the project.
  * No acceptance claim: this script converts and records; the numeric verdict
    belongs to the criterion in `scripts/line/contract.py`.

Gated quantity (declarative, not hardcoded): the contract may set
`verification.gate_output` to `auto` (default), `logits` or `probabilities`.

  * `logits`  - the trailing softmax node is skipped in hardware and the Python
                reference is computed with that activation replaced by linear, so
                the gate compares the same quantity on both sides. This is the
                documented default for a model whose last layer carries softmax,
                because hls4ml's softmax is an approximate lookup table whose error
                exceeds the frozen rule (see hls4ml-convert/known-issues.md).
  * `probabilities` - nothing is skipped; the gate compares final probabilities.

Requires the conversion environment the SKILL.md names:
  D:\\python_envs\\hls4ml_conv\\Scripts\\python.exe   (TF 2.13.1 / hls4ml 1.3.0)

Usage:
  python convert_keras.py --deployment <deployment.yaml> --output <project-dir>
                          [--inputs <inputs.json>] [--reference-out <json>]
                          [--evidence <json>] [--template <yaml>]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

import yaml
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_config import apply_contract_config, firmware_config_audit
from generated_compat import patch_po2_product, pin_output_grid

SKILL = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = SKILL / "templates" / "hls4ml_config_template.yaml"
KERAS_JSON_FRONTENDS = {"keras_json_weights", "qkeras_json_weights"}
KERAS_FILE_FRONTENDS = {"keras_h5", "keras_archive"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def sample_shape(contract: dict) -> tuple[int, ...]:
    """Hardware input shape: the declared input_shape minus its batch dimension.

    The contract's ModelSpec requires the declared rank to include the batch
    dimension ("input_shape rank must match layout, including batch dimension"),
    so dropping axis 0 is the contract's own rule, not a guess.
    """
    shape = list(contract["model"]["input_shape"])
    if len(shape) < 2:
        raise SystemExit(f"input_shape must include a batch dimension: {shape}")
    return tuple(shape[1:])


def board_part(contract_path: Path, contract: dict) -> str:
    board_profile = contract.get("board_profile")
    if not board_profile:
        raise SystemExit("contract has no board_profile; the target part cannot be derived")
    profile_path = (contract_path.parent / board_profile).resolve()
    if not profile_path.is_file():
        raise SystemExit(f"board profile not found: {profile_path}")
    profile = read_yaml(profile_path)
    for key in ("part", "target_part"):
        if profile.get(key):
            return str(profile[key])
    fpga = profile.get("fpga") or {}
    if fpga.get("part"):
        return str(fpga["part"])
    raise SystemExit(f"board profile has no part field: {profile_path}")


def load_model(contract_path: Path, contract: dict):
    import tensorflow as tf

    model_spec = contract["model"]
    frontend = model_spec.get("frontend")
    model_path = (contract_path.parent / model_spec["path"]).resolve()
    associated = [
        (contract_path.parent / p).resolve() for p in model_spec.get("associated_files") or []
    ]

    if frontend in KERAS_JSON_FRONTENDS:
        if len(associated) != 1:
            raise SystemExit("Keras JSON frontends need exactly one associated weights file")
        text = model_path.read_text(encoding="utf-8")
        if frontend == "qkeras_json_weights":
            # QKeras layers (QConv2D, QActivation, ...) are not known to tf.keras, so
            # `tf.keras.models.model_from_json` raises "Unknown layer: 'QConv2D'" and the
            # contract cannot be converted at all. QKeras ships its own loader, which
            # registers the quantized objects before rebuilding the graph; that is the
            # documented way to load a quantized JSON model.
            import qkeras.utils

            model = qkeras.utils.quantized_model_from_json(text)
            loader = {
                "loader": "qkeras.utils.quantized_model_from_json+load_weights",
                "weights": associated[0].name,
            }
        else:
            model = tf.keras.models.model_from_json(text)
            loader = {"loader": "model_from_json+load_weights", "weights": associated[0].name}
        model.load_weights(str(associated[0]))
        model.build((None, *sample_shape(contract)))
        return model, loader
    if frontend in KERAS_FILE_FRONTENDS:
        model = tf.keras.models.load_model(str(model_path), compile=False)
        return model, {"loader": "load_model", "container": model_path.suffix}
    raise SystemExit(f"frontend not supported by Path A (Keras): {frontend}")


def softmax_nodes(model) -> list[str]:
    """Names of layers whose softmax activation becomes its own hls4ml node."""
    names = []
    for layer in model.layers:
        activation = getattr(layer, "activation", None)
        if getattr(activation, "__name__", None) == "softmax":
            names.append(layer.name)
    return names


def resolve_softmax_nodes(model, layer_names: list[str], layer_config: dict):
    """hls4ml nodes that carry the softmax activation, and any that cannot be resolved.

    A Dense/Conv layer with a softmax activation becomes two hls4ml nodes: the layer
    itself and a `<layer>_softmax` activation node. Only the activation node may be
    skipped. Skipping the owning layer deletes that layer's arithmetic: run
    20260927-02 attempt 02 did exactly that and produced a 14.904 absolute logit error
    against the same reference, which is not a precision effect but a wrong network.
    A standalone `Activation('softmax')` layer *is* the activation node.
    """
    nodes: list[str] = []
    unresolved: list[str] = []
    by_name = {layer.name: layer for layer in model.layers}
    for name in layer_names:
        activation_node = f"{name}_softmax"
        if activation_node in layer_config:
            nodes.append(activation_node)
        elif name in layer_config and type(by_name.get(name)).__name__ == "Activation":
            nodes.append(name)
        else:
            unresolved.append(name)
    return nodes, unresolved


def load_inputs(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in ("inputs", "samples", "data"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        raise SystemExit(f"inputs file has no samples: {path}")
    return payload


def forward_reference(model, inputs, *, drop_softmax: bool):
    """Python reference for the gated quantity, in the same order as the inputs."""
    import keras
    import numpy as np

    layers = list(model.layers)
    saved = []
    if drop_softmax:
        for layer in layers:
            activation = getattr(layer, "activation", None)
            if getattr(activation, "__name__", None) == "softmax":
                saved.append((layer, getattr(layer, "activation", None)))
                layer.activation = keras.activations.linear
    try:
        # Replaying the layer list positionally (`values = layer(values)`) works in Keras 2
        # but not in Keras 3, where the first layer of a Functional model is an InputLayer
        # and an InputLayer is not callable on a value -- it raises `TypeError: too many
        # positional arguments` from `inspect.Signature.bind`. Measured on
        # `imported-mlp-cnn-20260917`: both encoders (MLP and A-CNN) begin with an
        # InputLayer, so the Python reference could never be computed for them, and the
        # regression test `_line/tests/test_convert_keras3_reference.py` pins this.
        #
        # Calling the model itself is the equivalent forward pass in Keras 2 and in Keras 3,
        # and it leaves the softmax swap above intact: that swap mutates the very layer
        # objects the functional graph closes over, so the model call sees the linear
        # activation.
        values = np.asarray(inputs, dtype=np.float32)
        return np.asarray(model(values))
    finally:
        for layer, activation in saved:
            layer.activation = activation


def write_dat(path: Path, values) -> None:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    rows = array.reshape(len(array), -1)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(" ".join(f"{value:.9g}" for value in row) + "\n")


def qkeras_layer_precisions(model) -> dict:
    """Per-layer `Precision` blocks derived from a quantized model's own quantizers.

    A QKeras model *is* its quantization: `QActivation(quantized_relu(6,0))` saturates its
    output to `ufixed<6,0>` with rounding, and the network's later layers were trained on
    that. Converting such a model with a single uniform default precision replaces the
    quantized activation with a plain 20-bit ReLU, so the hardware computes a different
    function from the model the reference runs (measured on the qkeras_mnist_cnn contract:
    1000/1000 values outside tolerance, max abs error 50.8 against logits of order 30,
    argmax agreeing with the model on 11/100 samples).

    Only the `Precision` sub-block is taken from hls4ml's own derivation. `ReuseFactor`
    and `Strategy` stay the contract's: injecting those per layer instead changes which
    kernel hls4ml emits and how it packs the weight files, which is the separate defect
    documented in `build_hls_config` (D-007).
    """
    import hls4ml.utils

    derived = hls4ml.utils.config_from_keras_model(model, granularity="name")
    precisions = {}
    for layer, block in (derived.get("LayerName") or {}).items():
        precision = (block or {}).get("Precision")
        if precision:
            precisions[layer] = {key: value for key, value in precision.items()}
    return precisions


def build_hls_config(template: dict, contract: dict, skipped_nodes: list[str],
                     layer_precisions: dict | None = None):
    """hls4ml config built from this skill's template plus contract overrides.

    The template is the config source on purpose. Injecting a per-layer
    `config_from_keras_model(..., granularity="name")` block instead makes hls4ml emit
    per-layer named precision types and write the weight files in the unpacked order,
    while the generated `dense_resource` kernel consumes them in the reuse-factor-packed
    order: the hardware then computes a different function from the same model. Measured
    on the keras_3layer contract: with the per-layer block the CSim output was
    uncorrelated with the reference (max abs error 14.9 on a +-15 range); with this
    template-only config the weight files match the layout the kernel reads (the swap
    experiment in run 20260927-02 reproduced the reference to quantization error).

    `layer_precisions` therefore carries precision only (see `qkeras_layer_precisions`):
    it is applied for quantized models, whose activations are part of the model's
    definition, and never for plain Keras models.
    """
    model_section = dict(template.get("Model") or {})
    # PyTorch-only knobs must not leak into a Keras conversion: with
    # ChannelsLastConversion enabled for an already channels-last Keras model, hls4ml
    # 1.3.0 writes Dense weights in a different order than the kernel reads them, so the
    # generated hardware implements a different function (see the template comment).
    for _pytorch_only in ("ChannelsLastConversion", "TransposeOutputs"):
        model_section.pop(_pytorch_only, None)
    model_section["Precision"] = {"default": contract["precision"]}
    model_section["ReuseFactor"] = contract["reuse_factor"]
    model_section["Strategy"] = contract["strategy"]
    config = {"Model": model_section, "LayerName": {}}
    for layer, precision in (layer_precisions or {}).items():
        config["LayerName"].setdefault(layer, {})["Precision"] = precision
    for node in skipped_nodes:
        config["LayerName"].setdefault(node, {})["skip"] = True
    for layer, factor in (contract.get("layer_reuse_factors") or {}).items():
        config["LayerName"].setdefault(layer, {})["ReuseFactor"] = factor
    return apply_contract_config(config, contract, skipped_nodes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--inputs", type=Path, default=None, help="defaults to contract verification.inputs")
    parser.add_argument("--reference-out", type=Path, default=None, help="gate reference JSON")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()

    import hls4ml
    import tensorflow as tf

    contract_path = args.deployment.resolve()
    contract = read_yaml(contract_path)
    project_dir = args.output.resolve()
    if project_dir.exists() and any(project_dir.iterdir()):
        raise SystemExit(f"output directory is not empty, refusing to overwrite: {project_dir}")

    template = read_yaml(args.template)
    shape = sample_shape(contract)
    part = board_part(contract_path, contract)
    verification = contract.get("verification") or {}
    gate_output = str(verification.get("gate_output", "auto")).lower()
    if gate_output not in {"auto", "logits", "probabilities"}:
        raise SystemExit(f"unknown verification.gate_output: {gate_output}")

    model, loader = load_model(contract_path, contract)
    trailing_softmax = softmax_nodes(model)
    drop_softmax = gate_output == "logits" or (gate_output == "auto" and bool(trailing_softmax))

    backend = "Vitis" if contract.get("backend") == "hls4ml-vitis" else template.get("Backend", "Vivado")

    # hls4ml names the activation node `<layer>_softmax` when a Dense/Conv layer carries
    # the activation, and keeps the layer name for a standalone Activation layer. The
    # names are derived from the Keras graph, not from a per-layer config block, so the
    # config stays template-driven (see build_hls_config).
    skipped_nodes: list[str] = []
    unresolved_nodes: list[str] = []
    if drop_softmax:
        by_name = {layer.name: layer for layer in model.layers}
        for name in trailing_softmax:
            if name not in by_name:
                unresolved_nodes.append(name)
            elif type(by_name[name]).__name__ == "Activation":
                skipped_nodes.append(name)
            else:
                skipped_nodes.append(f"{name}_softmax")
        if unresolved_nodes or not skipped_nodes:
            raise SystemExit(
                "cannot resolve the softmax node to skip, refusing to convert a "
                f"different network: softmax layers={trailing_softmax}, "
                f"unresolved={unresolved_nodes}"
            )

    # A quantized (QKeras/Brevitas) model defines its own per-layer precisions; honouring
    # them is part of converting the same function. Plain Keras models must stay on the
    # template-only config (see build_hls_config for the measurement behind that rule).
    layer_precisions = {}
    if contract["model"].get("frontend") in {"qkeras_json_weights", "brevitas"} or \
            contract["model"].get("quantized"):
        layer_precisions = qkeras_layer_precisions(model)
    hls_config = build_hls_config(template, contract, skipped_nodes, layer_precisions)

    project_dir.mkdir(parents=True, exist_ok=True)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        hls_model = hls4ml.converters.convert_from_keras_model(
            model,
            hls_config=hls_config,
            output_dir=str(project_dir),
            part=part,
            clock_period=float(contract["clock_period_ns"]),
            io_type=contract["io_type"],
            backend=backend,
        )
        output_grid_record = pin_output_grid(hls_model, contract['precision']) if layer_precisions else None
        if output_grid_record:
            hls_config = hls_model.config.config['HLSConfig']
        hls_model.write()
    conversion_log = capture.getvalue()
    sys.stdout.write(conversion_log)
    # Only projects that actually use the exponential product need this verified repair.
    uses_po2 = any('nnet::product::weight_exponential' in p.read_text(encoding='utf-8')
                   for p in (project_dir / 'firmware').glob('*.h'))
    compat_record = patch_po2_product(project_dir, hls4ml.__version__) if uses_po2 else None
    # hls4ml 1.3.0 answers an unsupported ReuseFactor with a printed warning and a silent
    # substitution (backends/fpga/fpga_backend.py:set_closest_reuse_factor). That is not
    # cosmetic: measured on the qkeras_mnist_cnn contract, the substituted convolution
    # (requested 16, used 18) no longer matched the model - max abs error 27.5 on logits of
    # order 30, 989/1000 values outside tolerance - while a reuse factor hls4ml accepts
    # (rf=1, same precision) brought the same design to 0.095, and enough accumulator
    # precision then brought it to 0.023 with 0/1000. A silent substitution is therefore
    # treated as a failed conversion, reported with the values hls4ml would accept.
    substitutions = [line.strip() for line in conversion_log.splitlines()
                     if "Invalid ReuseFactor=" in line]
    if substitutions:
        # A refused conversion must not leave a project behind: the emitted kernel already
        # contains the substitution, and a leftover directory would both block a retry in
        # the same run and invite a later stage to synthesise the wrong design.
        shutil.rmtree(project_dir, ignore_errors=True)
        evidence = {
            "schema_version": 1,
            "stage": "hls4ml_convert",
            "skill": "hls4ml-convert",
            "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
            "template": {"path": str(args.template), "sha256": sha256_file(args.template)},
            "status": "FAILED",
            "reasons": [
                "hls4ml substituted a ReuseFactor this contract asked for; the substitution "
                "changes the emitted kernel and, measured on this line, the arithmetic",
                *substitutions,
            ],
            "reuse_factor_requested": contract["reuse_factor"],
            "output_dir": str(project_dir),
            "numerical_verdict": "NOT_EVALUATED_BY_THIS_SCRIPT",
        }
        if args.evidence:
            args.evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.evidence.resolve().write_text(
                json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        raise SystemExit(
            "hls4ml substituted a ReuseFactor instead of using the contract's value:\n  "
            + "\n  ".join(substitutions)
            + "\nChoose a reuse_factor naming one of the valid values for every layer "
              "(the message lists them per layer); the line does not accept a silent "
              "substitution because it changes the hardware's arithmetic."
        )

    # Testbench data and the Python side of the gate.
    inputs_path = args.inputs
    if inputs_path is None:
        entry = verification.get("inputs")
        inputs_path = (contract_path.parent / entry).resolve() if entry else None
    reference_record = {"inputs": None, "reference": None, "shape": None}
    if inputs_path and Path(inputs_path).is_file():
        samples = load_inputs(Path(inputs_path))
        reference = forward_reference(model, samples, drop_softmax=drop_softmax)
        tb_data = project_dir / "tb_data"
        tb_data.mkdir(exist_ok=True)
        write_dat(tb_data / "tb_input_features.dat", samples)
        write_dat(tb_data / "tb_output_predictions.dat", reference)
        reference_out = args.reference_out or (project_dir.parent / "python-output.json")
        Path(reference_out).write_text(
            json.dumps(reference.tolist(), ensure_ascii=False), encoding="utf-8"
        )
        reference_record = {
            "inputs": {"path": str(inputs_path), "sha256": sha256_file(Path(inputs_path))},
            "reference": {"path": str(reference_out), "sha256": sha256_file(Path(reference_out))},
            "shape": list(reference.shape),
            "samples": int(reference.shape[0]),
        }

    firmware = sorted(p.name for p in (project_dir / "firmware").glob("*") if p.is_file())
    evidence = {
        "schema_version": 1,
        "stage": "hls4ml_convert",
        "skill": "hls4ml-convert",
        "skill_path": str(SKILL / "SKILL.md"),
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "template": {"path": str(args.template), "sha256": sha256_file(args.template)},
        "environment": {
            "python": sys.version.split()[0],
            "tensorflow": tf.__version__,
            "hls4ml": hls4ml.__version__,
        },
        "model_loader": loader,
        "compatibility_repairs": [compat_record] if compat_record else [],
        "output_grid_pin": output_grid_record,
        "configuration_audit": firmware_config_audit(project_dir, contract.get('hls_config') or {}, hls_config,
            nodes=[{'name': n.name, 'class_name': n.class_name} for n in hls_model.get_layers()]),
        "input_shape_used": list(shape),
        "target_part": part,
        "gate_quantity": {
            "declared": gate_output,
            "resolved": "pre_softmax_logits" if drop_softmax else "probabilities",
            "softmax_layers": trailing_softmax,
            "skipped_hls4ml_nodes": skipped_nodes,
            "unresolved_hls4ml_nodes": unresolved_nodes,
        },
        "config_applied": {
            "precision": contract["precision"],
            "reuse_factor": contract["reuse_factor"],
            "strategy": contract["strategy"],
            "io_type": contract["io_type"],
            "clock_period_ns": contract["clock_period_ns"],
            "backend": backend,
            "top_dataflow_declared": contract.get("top_dataflow", True),
            # hls4ml 1.3.0 has no top-dataflow knob (grep of the installed package for
            # TopDataflow/top_dataflow returns nothing); the emitted `#pragma HLS DATAFLOW`
            # follows Strategy: Resource. The contract field is recorded, not claimed.
            "top_dataflow_enforced_by_hls4ml": False,
            "config_source": "templates/hls4ml_config_template.yaml + contract",
            "layer_reuse_factors": contract.get("layer_reuse_factors") or {},
            # For a quantized model the per-layer precisions come from the model's own
            # quantizers; recording them makes the hardware's arithmetic auditable against
            # the model definition instead of against a global default.
            "layer_precisions_from_model": layer_precisions,
        },
        "reference": reference_record,
        "output_dir": str(project_dir),
        "firmware_files": firmware,
        "hls4ml_config_path": str(project_dir / "hls4ml_config.yml"),
        "hls4ml_config_sha256": sha256_file(project_dir / "hls4ml_config.yml")
        if (project_dir / "hls4ml_config.yml").is_file()
        else None,
        "numerical_verdict": "NOT_EVALUATED_BY_THIS_SCRIPT",
    }
    evidence_path = args.evidence or (project_dir.parent / "convert-evidence.json")
    evidence_path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if evidence['configuration_audit']['status'] == 'FAILED':
        raise SystemExit('generated configuration mismatch: ' + '; '.join(evidence['configuration_audit']['mismatches']))
    print(json.dumps({
        "project": str(project_dir),
        "gate_quantity": evidence["gate_quantity"]["resolved"],
        "skipped_nodes": skipped_nodes,
        "samples": reference_record.get("samples"),
        "evidence": str(evidence_path),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
