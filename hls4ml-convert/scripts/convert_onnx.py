#!/usr/bin/env python
"""Path B of the hls4ml-convert skill: trained ONNX model -> hls4ml HLS project.

This script is the executable form of the step that `hls4ml-convert/SKILL.md`
documents as Path B. It exists because the SKILL.md references
`scripts/convert_onnx.py` and that file was absent, so a memoryless agent could
only execute Path A (Keras) and the ONNX path of the skill was unreachable.

Design rules (operator principles, 2026-09-27, same as Path A):
  * Generic: every deployment parameter comes from the deployment contract; the
    hls4ml config is built from this skill's own template. No model name, layer
    name, shape or precision is hardcoded here.
  * Memoryless: one command, absolute paths, all inputs named on the command line,
    and a machine-readable evidence file written next to the project.
  * No acceptance claim: this script converts and records; the numeric verdict
    belongs to the criterion in `scripts/line/contract.py`.

Gated quantity (declarative, not hardcoded): the contract may set
`verification.gate_output` to `auto` (default), `logits` or `probabilities`, with
the same semantics as Path A.

  * `logits`  - the `Softmax` node that produces the graph output is skipped in
                hardware (hls4ml `skip` on that layer) and removed from the Python
                reference graph (the graph output is re-pointed at the softmax
                input, i.e. the activation becomes the identity), so the gate
                compares the same pre-softmax quantity on both sides.
  * `probabilities` - nothing is skipped; the gate compares the graph output.
  * `auto`    - `logits` when such a trailing `Softmax` node exists, else
                `probabilities`.

What hls4ml 1.3.0's ONNX frontend requires of the file (measured on this line,
2026-09-28, branch `branched_small_ch_last` v2 package):
  * every `Conv` node must carry the `qonnx.custom_op.channels_last` domain: the
    handler at `hls4ml/converters/onnx/convolution.py` raises "Please convert the
    model to channels-last format with qonnx-to-channels-last" for a Conv node in
    the default domain. `ChannelsLastConversion` is the *opposite* direction
    (channels_first -> channels_last) and must stay off for a graph that is
    already channels-last: it transposes weights that already have the layout the
    generated kernel reads. The template keeps that key commented out, and this
    script removes it from the Model section if it is ever uncommented.
  * every intermediate tensor must already carry `value_info`: the frontend reads
    shapes from the graph and does not run shape inference (`onnx_to_hls.py`:
    "We don't infer the shapes because the qonnx package preprocessing does it").
    This script does not add it - a model without it is a defect of the exporter,
    not something to paper over here.
  * a model whose ops live in the qonnx custom-op domains cannot be executed by
    `onnxruntime` or by `onnx.reference.ReferenceEvaluator` directly (both fail
    with "qonnx.custom_op.channels_last:Conv(-1) is not a registered
    function/op"). The Python reference therefore runs through
    `qonnx.core.onnx_exec.execute_onnx`, which dispatches those nodes to the
    implementations shipped with qonnx. Measured on the contract above: that
    route reproduces the package's frozen `references.json` bit for bit
    (100/100 samples, max abs difference 0.0).

Requires an interpreter with hls4ml 1.3.0, `onnx` and `qonnx` (qonnx is what makes
the reference possible). The deployment workspace `.venv` (Python 3.10,
hls4ml 1.3.0, onnx 1.14.1, onnxruntime 1.23.2, qonnx 0.4.0, numpy 1.26.4) is such
an interpreter; `D:\\python_envs\\hls4ml_conv` converts as well but has no qonnx
and therefore cannot produce the Python reference. The environment actually used
is recorded in the evidence file, never assumed.

Usage:
  python convert_onnx.py --deployment <deployment.yaml> --output <project-dir>
                         [--inputs <inputs.json>] [--reference-out <json>]
                         [--evidence <json>] [--template <yaml>]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path

import yaml
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_config import apply_contract_config, firmware_config_audit

SKILL = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = SKILL / "templates" / "hls4ml_config_template.yaml"
ONNX_FRONTENDS = {"onnx"}
PYTORCH_ONLY_MODEL_KEYS = ("ChannelsLastConversion", "TransposeOutputs")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def module_version(name: str) -> str:
    """Installed version of `name`, or the string 'MISSING'. Recorded, not assumed."""
    try:
        module = importlib.import_module(name)
    except ImportError:
        return "MISSING"
    version = getattr(module, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib.metadata import version as distribution_version

        return str(distribution_version(name))
    except Exception:  # pragma: no cover - metadata missing for an installed module
        return "unknown"


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
    """The ONNX model as declared by the contract, plus how it was loaded."""
    import onnx

    model_spec = contract["model"]
    frontend = model_spec.get("frontend")
    if frontend not in ONNX_FRONTENDS:
        raise SystemExit(f"frontend not supported by Path B (ONNX): {frontend}")
    model_path = (contract_path.parent / model_spec["path"]).resolve()
    if not model_path.is_file():
        raise SystemExit(f"ONNX model not found: {model_path}")

    model = onnx.load(str(model_path))
    loader = {
        "frontend": frontend,
        "loader": "onnx.load",
        "model": str(model_path),
        "sha256": sha256_file(model_path),
        "ir_version": int(model.ir_version),
        "producer": f"{model.producer_name} {model.producer_version}".strip(),
        "opset_imports": [
            {"domain": entry.domain, "version": int(entry.version)} for entry in model.opset_import
        ],
        "nodes": [
            {"name": node.name, "op_type": node.op_type, "domain": node.domain}
            for node in model.graph.node
        ],
        "value_info_entries": len(model.graph.value_info),
        "initializers": [tensor.name for tensor in model.graph.initializer],
    }
    return model, model_path, loader


def graph_io(model) -> tuple[list, list]:
    """Graph inputs that are real inputs (initializers are constants, not inputs)."""
    initializers = {tensor.name for tensor in model.graph.initializer}
    graph_inputs = [entry for entry in model.graph.input if entry.name not in initializers]
    return graph_inputs, list(model.graph.output)


def static_shape(value_info) -> list[int]:
    """Static dimensions of a tensor, refusing a dynamic (symbolic) one."""
    dims = [dim.dim_value for dim in value_info.type.tensor_type.shape.dim]
    if not dims or any(dim <= 0 for dim in dims):
        symbolic = [dim.dim_param or dim.dim_value for dim in value_info.type.tensor_type.shape.dim]
        raise SystemExit(
            f"tensor {value_info.name} has no static shape ({symbolic}); a contract "
            "input_shape cannot be checked against a graph with dynamic dimensions"
        )
    return dims


def hls4ml_layer_name(node) -> str:
    """The name hls4ml gives a layer parsed from this node.

    Mirrors `sanitize_layer_name` in `hls4ml/converters/onnx_to_hls.py`: a name
    starting with a digit is prefixed with the lowercased class name. A layer's
    `skip` attribute is read from the config under this name when the layer is
    created, so this is the key the config has to use.
    """
    name = node.name
    if not name:
        raise SystemExit(
            f"node {node.op_type} has no name; hls4ml's ONNX frontend requires named nodes"
        )
    if name[0].isdigit():
        name = node.op_type.lower() + name
    return name


def softmax_nodes(model) -> tuple[list[str], list[str]]:
    """hls4ml layer names of the Softmax nodes: (those producing a graph output, rest).

    Only a softmax that *is* a graph output may be skipped: it is the activation the
    contract's `gate_output` refers to. Skipping an interior softmax would delete
    arithmetic the reference still applies, so it is reported as unresolved instead
    of being dropped silently. Path A resolves the equivalent Keras case by allowing
    only the `<layer>_softmax` activation node to be skipped, never the owning layer.
    """
    outputs = {entry.name for entry in model.graph.output}
    trailing, interior = [], []
    for node in model.graph.node:
        if node.op_type != "Softmax":
            continue
        if outputs.intersection(node.output):
            trailing.append(hls4ml_layer_name(node))
        else:
            interior.append(hls4ml_layer_name(node))
    return trailing, interior


def reference_graph(model, *, skipped_nodes: list[str]):
    """Graph the Python reference is computed with.

    Every skipped node is replaced, in its own position, by an `Identity` node: the
    activation becomes a no-op, the ONNX form of Path A swapping a Keras softmax
    activation for `linear`. Nothing else about the graph changes, and every declared
    shape stays where it was, so the arithmetic of every other node is exactly what
    the contract's model defines.

    The node is replaced rather than removed with the graph output re-pointed at its
    input, because re-pointing declares the skipped node's input tensor twice - once
    in `graph.value_info` (where hls4ml requires it) and once in `graph.output` - and
    qonnx refuses such a graph with "Multiple ValueInfoProto found for <tensor>".
    Measured on a fixture whose trailing Softmax feeds the graph output.
    """
    graph = copy.deepcopy(model)
    if not skipped_nodes:
        return graph, None
    from onnx import helper

    skipped = set(skipped_nodes)
    targets = [
        index
        for index, node in enumerate(graph.graph.node)
        if node.op_type == "Softmax" and hls4ml_layer_name(node) in skipped
    ]
    replaced = set()
    for index in reversed(targets):
        node = graph.graph.node[index]
        if len(node.input) != 1 or len(node.output) != 1:
            raise SystemExit(
                f"cannot replace {node.op_type} node {node.name}: expected exactly one "
                f"input and one output, got {list(node.input)} -> {list(node.output)}"
            )
        graph.graph.node.remove(node)
        graph.graph.node.insert(
            index,
            helper.make_node("Identity", [node.input[0]], [node.output[0]], name=node.name),
        )
        replaced.add(node.name)
    # The replaced set is built from the indices verified to be Softmax nodes, so names
    # need not match hls4ml's sanitised layer names: hls4ml renames a layer whose ONNX
    # node name starts with a digit (node `1softmax` -> layer `softmax1`), and comparing
    # the two strings refused a perfectly good graph. The count is what must agree.
    name_note = None
    if len(replaced) != len(skipped):
        raise SystemExit(
            "cannot replace the resolved nodes in the reference graph: resolved "
            f"{len(skipped)} ({sorted(skipped)}), replaced {len(replaced)} ({sorted(replaced)})"
        )
    if replaced != skipped:
        name_note = {
            "hls4ml_layer_names": sorted(skipped),
            "onnx_node_names": sorted(replaced),
            "why": "hls4ml sanitises layer names, so the strings differ; the nodes are the "
                   "same Softmax nodes, verified by op_type before replacement",
        }
    return graph, name_note


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


def forward_reference(
    model, samples, *, loaded_input: str, loaded_output: str, skipped_nodes: list[str]
):
    """Python reference for the gated quantity, in the same order as the inputs.

    Executed with `qonnx.core.onnx_exec.execute_onnx`, one contract sample at a
    time at batch 1: a qonnx custom-op graph is not runnable by onnxruntime or by
    `onnx.reference.ReferenceEvaluator` (both reject the custom domain), while
    qonnx dispatches those nodes to its own implementations. Each sample is fed at
    the batch size the graph declares for its input, so a contract that lists N
    samples never has to match the graph's own batch dimension.
    """
    import numpy as np
    from onnx import helper

    try:
        from qonnx.core.modelwrapper import ModelWrapper
        from qonnx.core.onnx_exec import execute_onnx
        from qonnx.transformation.infer_shapes import InferShapes
    except ImportError as error:
        # A qonnx custom-op graph cannot be executed by onnxruntime or by
        # onnx.reference.ReferenceEvaluator, so there is no second runtime to fall back
        # to: without qonnx there is no Python reference at all. Say which interpreter
        # was used, because that is the thing the operator has to change.
        raise SystemExit(
            "the Python reference requires the qonnx package (it executes the ONNX "
            "custom-op domains that onnxruntime and onnx.reference refuse), and this "
            f"interpreter does not have it: {sys.executable} ({error}). Run this script "
            "with an interpreter that has hls4ml 1.3.0, onnx and qonnx installed."
        ) from error

    graph, softmax_name_note = reference_graph(model, skipped_nodes=skipped_nodes)
    wrapper = ModelWrapper(graph)

    preprocessing: list[str] = []
    if not wrapper.check_all_tensor_shapes_specified():
        wrapper = wrapper.transform(InferShapes())
        preprocessing.append("qonnx.transformation.infer_shapes.InferShapes")
        if not wrapper.check_all_tensor_shapes_specified():
            raise SystemExit("the ONNX graph still has unspecified tensor shapes after InferShapes")

    graph_inputs, graph_outputs = graph_io(wrapper.model)
    if len(graph_inputs) != 1 or len(graph_outputs) != 1:
        raise SystemExit(
            "the Python reference requires exactly one graph input and one graph output, "
            f"found {[x.name for x in graph_inputs]} -> {[x.name for x in graph_outputs]}"
        )
    input_name = graph_inputs[0].name
    output_name = graph_outputs[0].name
    if input_name != loaded_input or output_name != loaded_output:
        raise SystemExit(
            "the graph the reference is computed on does not match the model that was "
            f"loaded: {input_name} -> {output_name} instead of {loaded_input} -> {loaded_output}"
        )
    feed_shape = (1, *static_shape(graph_inputs[0])[1:])
    dtype = helper.tensor_dtype_to_np_dtype(graph_inputs[0].type.tensor_type.elem_type)

    outputs = []
    for sample in samples:
        feed = np.asarray(sample, dtype=dtype).reshape(feed_shape)
        result = execute_onnx(wrapper, {input_name: feed})
        outputs.append(np.asarray(result[output_name], dtype=np.float64))
    reference = np.concatenate(outputs, axis=0)
    record = {
        "runtime": "qonnx.core.onnx_exec.execute_onnx",
        "loader": "onnx.load + qonnx.core.onnx_exec.execute_onnx",
        "preprocessing": preprocessing,
        "input_name": input_name,
        "output_name": output_name,
        "input_shape_fed": list(feed_shape),
        "output_shape_per_sample": list(outputs[0].shape),
        "dtype_fed": str(dtype),
        "samples": len(outputs),
        "skipped_node_name_mapping": softmax_name_note,
    }
    return reference, record


def write_dat(path: Path, values) -> None:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    rows = array.reshape(len(array), -1)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(" ".join(f"{value:.9g}" for value in row) + "\n")


def build_hls_config(template: dict, contract: dict, skipped_nodes: list[str]):
    """hls4ml config built from this skill's template plus contract overrides.

    The template is the config source on purpose, for the same reason as Path A:
    injecting a per-layer `config_from_*_model(..., granularity="name")` block instead
    makes hls4ml emit per-layer precision types and write the weight files in the
    unpacked order while the generated kernel consumes them in the reuse-factor-packed
    order, so the hardware computes a different function from the same model.
    """
    model_section = dict(template.get("Model") or {})
    # ChannelsLastConversion is a channels_first -> channels_last rewrite for
    # PyTorch/ONNX models. It must not be enabled for this path: hls4ml's ONNX Conv
    # handler already requires the graph to be channels-last (node domain
    # `qonnx.custom_op.channels_last`), so the weights are in the layout the kernel
    # reads and re-transposing them implements a different function. The key is
    # commented out in the template with the measurement that led to that decision;
    # removing it here keeps a conversion correct even if someone uncomments it.
    for _pytorch_only in PYTORCH_ONLY_MODEL_KEYS:
        model_section.pop(_pytorch_only, None)
    model_section["Precision"] = {"default": contract["precision"]}
    model_section["ReuseFactor"] = contract["reuse_factor"]
    model_section["Strategy"] = contract["strategy"]
    config = {"Model": model_section, "LayerName": {}}
    for node in skipped_nodes:
        config["LayerName"].setdefault(node, {})["skip"] = True
    for layer, factor in (contract.get("layer_reuse_factors") or {}).items():
        config["LayerName"].setdefault(layer, {})["ReuseFactor"] = factor
    return apply_contract_config(config, contract, skipped_nodes)


def interface_pragmas(project_dir: Path) -> list[str]:
    """Verbatim HLS pragmas of the generated top function. Recorded, not interpreted."""
    pragmas: list[str] = []
    for source in sorted((project_dir / "firmware").glob("*.cpp")):
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#pragma HLS"):
                pragmas.append(stripped)
    return pragmas


def directory_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument(
        "--inputs", type=Path, default=None, help="defaults to contract verification.inputs"
    )
    parser.add_argument("--reference-out", type=Path, default=None, help="gate reference JSON")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()

    import hls4ml
    import numpy as np
    import onnx

    contract_path = args.deployment.resolve()
    contract = read_yaml(contract_path)
    project_dir = args.output.resolve()
    if project_dir.exists() and any(project_dir.iterdir()):
        raise SystemExit(f"output directory is not empty, refusing to overwrite: {project_dir}")

    template = read_yaml(args.template)
    declared_sample_shape = sample_shape(contract)
    part = board_part(contract_path, contract)
    verification = contract.get("verification") or {}
    gate_output = str(verification.get("gate_output", "auto")).lower()
    if gate_output not in {"auto", "logits", "probabilities"}:
        raise SystemExit(f"unknown verification.gate_output: {gate_output}")

    model, model_path, loader = load_model(contract_path, contract)
    graph_inputs, graph_outputs = graph_io(model)
    if len(graph_inputs) != 1:
        raise SystemExit(
            "the contract declares a single input_shape, so the graph must have exactly one "
            f"global input; found {[entry.name for entry in graph_inputs]}"
        )
    if len(graph_outputs) != 1:
        raise SystemExit(
            "the gate reference is a single array, so the graph must have exactly one output; "
            f"found {[entry.name for entry in graph_outputs]}"
        )
    graph_input_shape = static_shape(graph_inputs[0])
    graph_sample_shape = tuple(graph_input_shape[1:])
    if graph_sample_shape != declared_sample_shape:
        raise SystemExit(
            f"the contract input_shape {contract['model']['input_shape']} does not describe "
            f"the ONNX graph input {graph_inputs[0].name} {graph_input_shape}: sample shapes "
            f"differ ({list(declared_sample_shape)} vs {list(graph_sample_shape)})"
        )
    loader["input_shape"] = graph_input_shape
    loader["input_name"] = graph_inputs[0].name
    loader["output_name"] = graph_outputs[0].name

    trailing_softmax, interior_softmax = softmax_nodes(model)
    drop_softmax = gate_output == "logits" or (
        gate_output == "auto" and bool(trailing_softmax)
    )
    skipped_nodes: list[str] = []
    unresolved_nodes: list[str] = []
    if drop_softmax:
        skipped_nodes = list(trailing_softmax)
        unresolved_nodes = list(interior_softmax)
        if unresolved_nodes or not skipped_nodes:
            raise SystemExit(
                "cannot resolve the softmax node to skip, refusing to convert a "
                f"different network: trailing softmax={trailing_softmax}, "
                f"interior softmax={interior_softmax}"
            )

    backend = (
        "Vitis" if contract.get("backend") == "hls4ml-vitis" else template.get("Backend", "Vivado")
    )
    hls_config = build_hls_config(template, contract, skipped_nodes)

    project_dir.mkdir(parents=True, exist_ok=True)
    # The model is handed to hls4ml as the contract's file, unmodified: hls4ml loads it
    # itself and records that path in the generated hls4ml_config.yml. Nothing about the
    # graph is rewritten to make the conversion succeed.
    hls_model = hls4ml.converters.convert_from_onnx_model(
        str(model_path),
        hls_config=hls_config,
        output_dir=str(project_dir),
        part=part,
        clock_period=float(contract["clock_period_ns"]),
        io_type=contract["io_type"],
        backend=backend,
    )
    hls_model.write()

    # Testbench data and the Python side of the gate.
    inputs_path = args.inputs
    if inputs_path is None:
        entry = verification.get("inputs")
        inputs_path = (contract_path.parent / entry).resolve() if entry else None
    reference_record = {"inputs": None, "reference": None, "shape": None}
    if inputs_path and Path(inputs_path).is_file():
        samples = load_inputs(Path(inputs_path))
        reference, runtime = forward_reference(
            model,
            samples,
            loaded_input=loader["input_name"],
            loaded_output=loader["output_name"],
            skipped_nodes=skipped_nodes,
        )
        tb_data = project_dir / "tb_data"
        tb_data.mkdir(exist_ok=True)
        write_dat(tb_data / "tb_input_features.dat", samples)
        write_dat(tb_data / "tb_output_predictions.dat", reference)
        reference_out = args.reference_out or (project_dir.parent / "python-output.json")
        Path(reference_out).write_text(
            json.dumps(reference.tolist(), ensure_ascii=False), encoding="utf-8"
        )
        inputs_array = np.asarray(samples)
        reference_record = {
            "inputs": {
                "path": str(inputs_path),
                "sha256": sha256_file(Path(inputs_path)),
                "shape": list(inputs_array.shape),
                # dtype of the decoded JSON array; the dtype the runtime actually
                # receives is the graph's own input type, recorded under runtime.
                "json_array_dtype": str(inputs_array.dtype),
                "contract_input_dtype": contract["model"].get("input_dtype"),
            },
            "reference": {
                "path": str(reference_out),
                "sha256": sha256_file(Path(reference_out)),
                "shape": list(reference.shape),
            },
            "shape": list(reference.shape),
            "samples": int(reference.shape[0]),
            "runtime": runtime,
            "skipped_node_name_mapping": runtime.get("skipped_node_name_mapping"),
        }

    firmware_dir = project_dir / "firmware"
    firmware = sorted(p.name for p in firmware_dir.glob("*") if p.is_file())
    template_model_section = template.get("Model") or {}
    evidence = {
        "schema_version": 1,
        "stage": "hls4ml_convert",
        "skill": "hls4ml-convert",
        "skill_path": str(SKILL / "SKILL.md"),
        "path": "B",
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "template": {"path": str(args.template), "sha256": sha256_file(args.template)},
        "environment": {
            "python": sys.version.split()[0],
            "interpreter": sys.executable,
            "hls4ml": hls4ml.__version__,
            "hls4ml_path": hls4ml.__file__,
            "onnx": onnx.__version__,
            "onnxruntime": module_version("onnxruntime"),
            "qonnx": module_version("qonnx"),
            "numpy": np.__version__,
            "pyyaml": module_version("yaml"),
        },
        "model_loader": loader,
        "configuration_audit": firmware_config_audit(project_dir, contract.get('hls_config') or {}, hls_config,
            nodes=[{'name': n.name, 'class_name': n.class_name} for n in hls_model.get_layers()]),
        "input_shape_used": list(graph_sample_shape),
        "contract_input_shape": list(contract["model"]["input_shape"]),
        "graph_input_shape": graph_input_shape,
        "input_layout_declared": contract["model"].get("layout"),
        "target_part": part,
        "gate_quantity": {
            "declared": gate_output,
            "resolved": "pre_softmax_logits" if drop_softmax else "probabilities",
            "softmax_nodes": trailing_softmax,
            "interior_softmax_nodes": interior_softmax,
            "skipped_hls4ml_nodes": skipped_nodes,
            "unresolved_hls4ml_nodes": unresolved_nodes,
            "name_mapping": (reference_record or {}).get("skipped_node_name_mapping"),
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
            # TopDataflow/top_dataflow returns nothing); the contract field is recorded,
            # not claimed. The pragmas actually emitted are recorded verbatim under
            # firmware_interface_pragmas.
            "top_dataflow_enforced_by_hls4ml": False,
            "config_source": "templates/hls4ml_config_template.yaml + contract",
            "layer_reuse_factors": contract.get("layer_reuse_factors") or {},
            "channels_last_conversion_applied": False,
            "pytorch_only_template_keys_present": [
                key for key in PYTORCH_ONLY_MODEL_KEYS if key in template_model_section
            ],
        },
        "reference": reference_record,
        "output_dir": str(project_dir),
        "firmware_files": firmware,
        "firmware_sha256": {
            name: sha256_file(firmware_dir / name) for name in firmware
        },
        "weights_sha256": {
            name: digest
            for name, digest in directory_hashes(firmware_dir / "weights").items()
        },
        "firmware_interface_pragmas": interface_pragmas(project_dir),
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
