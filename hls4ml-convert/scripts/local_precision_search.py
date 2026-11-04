"""Opt-in, single-role Keras precision trials using generated host C++; never EDA.

First passing caller-ordered candidate is selected over the observed inputs only.
No default contract, installed dependency, original model or board is modified.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PRECISION_ROLES = (
    "weight",
    "bias",
    "result",
    "accum",
    "table",
    "exp_table",
    "inv_table",
    "inv_inp",
)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def compare_outputs(reference, actual):
    unknown = {"passed": False, "violations": None, "max_abs": None, "max_ratio": None}
    try:
        if not reference or not actual or len(reference) != len(actual):
            return dict(unknown, status="INVALID_SHAPE")
        errors, ratios = [], []
        for expected, received in zip(reference, actual, strict=True):
            if not expected or len(expected) != len(received):
                return dict(unknown, status="INVALID_SHAPE")
            for ref, value in zip(expected, received, strict=True):
                ref, value = float(ref), float(value)
                if not math.isfinite(ref) or not math.isfinite(value):
                    return dict(unknown, status="NON_FINITE")
                error = abs(value - ref)
                if not math.isfinite(error):
                    return dict(unknown, status="NUMERIC_OVERFLOW")
                errors.append(error)
                ratios.append(error / (0.001 + 0.001 * abs(ref)))
        return {
            "passed": max(ratios) <= 1,
            "violations": sum(r > 1 for r in ratios),
            "max_abs": max(errors),
            "max_ratio": max(ratios),
            "status": "OBSERVED",
            "values": len(errors),
            "scope": "host C++ observations, not board evidence",
        }
    except (TypeError, ValueError, OverflowError):
        return dict(unknown, status="INVALID_VALUES")


def local_override(contract, layer, role, precision):
    config = copy.deepcopy(contract)
    if config.get("hls_config") is None:
        config["hls_config"] = {}
    block = config["hls_config"].setdefault("LayerName", {}).setdefault(layer, {})
    for key in block:
        if isinstance(key, str) and key.replace("_", "").lower() == (role + "t").replace("_", ""):
            block[key] = precision
    setting = block.get("Precision", {})
    if isinstance(setting, str):
        setting = {"default": setting}
    if not isinstance(setting, dict):
        raise ValueError("local role override requires a Precision mapping or default type")
    block["Precision"] = setting
    setting[role] = precision
    return config


def verify_originals(before, paths, result, output):
    after = {}
    for name, path in paths.items():
        try:
            after[name] = digest(path)
        except OSError:
            after[name] = None
    result["original_inputs_unchanged"] = before == after
    if before != after:
        result["status"] = "INVALIDATED_INPUT_CHANGE"
        result["selected_precision"] = None
        result["identity_after"] = after
        write_json(output / "summary.json", result)
        raise ValueError("original model, inputs or contract changed during search")
    write_json(output / "summary.json", result)


def host_predict(project, name, samples, n_in, n_out, compiler, *, trace_sizes=None):
    """Compile the same generated firmware/bridge, not a Python arithmetic surrogate."""
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise ValueError("unsupported generated project name")
    work = project.parent / "host-csim"
    work.mkdir()  # A failed compiler attempt is immutable too.
    library = work / ("model.dll" if os.name == "nt" else "model.so")
    command = [
        str(compiler),
        "-std=c++14",
        "-O0",
        "-shared",
        "-fPIC",
        "-Ifirmware/ap_types",
        f'-DWEIGHTS_DIR="{(project / "firmware/weights").as_posix()}"',
        f"firmware/{name}.cpp",
        f"{name}_bridge.cpp",
        "-o",
        str(library),
    ]
    write_json(work / "command.json", command)
    start = time.monotonic()
    run = subprocess.run(
        command, cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    (work / "stdout.txt").write_text(run.stdout, encoding="utf-8")
    (work / "stderr.txt").write_text(run.stderr, encoding="utf-8")
    write_json(
        work / "compiler-result.json",
        {
            "exit_code": run.returncode,
            "duration_s": time.monotonic() - start,
            "observed_at": stamp(),
        },
    )
    if run.returncode:
        raise RuntimeError(f"host compiler failed with exit {run.returncode}; see {work}")
    context = (
        os.add_dll_directory(str(compiler.parent)) if os.name == "nt" else contextlib.nullcontext()
    )
    with context:
        dll = ctypes.CDLL(str(library))
        function = getattr(dll, name + "_float")
        function.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
        function.restype = None
        trace_sizes = trace_sizes or {}
        traces = {name: [] for name in trace_sizes}
        if trace_sizes:

            class TraceData(ctypes.Structure):
                _fields_ = [("name", ctypes.c_char_p), ("data", ctypes.c_void_p)]

            allocate = dll.allocate_trace_storage
            allocate.argtypes, allocate.restype = [ctypes.c_size_t], None
            collect = dll.collect_trace_output
            collect.argtypes, collect.restype = [ctypes.POINTER(TraceData)], None
            free = dll.free_trace_storage
            free.argtypes, free.restype = [], None
            trace_data = (TraceData * len(trace_sizes))()
            allocate(ctypes.sizeof(ctypes.c_float))
        outputs = []
        try:
            for row in samples:
                if len(row) != n_in:
                    raise ValueError("input row does not match generated core")
                data, result = (ctypes.c_float * n_in)(*row), (ctypes.c_float * n_out)()
                function(data, result)
                outputs.append(list(result))
                if trace_sizes:
                    collect(trace_data)
                    for item in trace_data:
                        key = item.name.decode("utf-8")
                        values = ctypes.cast(item.data, ctypes.POINTER(ctypes.c_float))
                        traces[key].append([values[i] for i in range(trace_sizes[key])])
        finally:
            if trace_sizes:
                free()
    write_json(work / "outputs.json", outputs)
    if trace_sizes:
        write_json(work / "layer-outputs.json", traces)
    return outputs


def role_precision(node, role):
    if any(cls.__name__ == "Softmax" for cls in type(node).__mro__):
        implementation = str(node.get_attr("implementation")).lower().split("::")[-1]
        used = {
            "stable": {"exp_table", "inv_table", "inv_inp", "accum", "result"},
            "latency": {"exp_table", "inv_table", "inv_inp", "accum", "result"},
            "legacy": {"table", "result"},
        }.get(implementation, {"result"})
        if role not in used:
            raise ValueError(f"precision role {role} not used by Softmax {implementation}")
    if role in {"weight", "bias"} and role in node.weights:
        return str(node.get_weights(role).type.precision)
    if role == "result":
        return str(node.get_output_variable().type.precision)
    declared = {attribute.name for attribute in node.expected_attributes}
    if (
        role in PRECISION_ROLES
        and role + "_t" in declared
        and node.get_attr(role + "_t") is not None
    ):
        return str(node.get_attr(role + "_t").precision)
    raise ValueError("target layer/precision role not present in generated graph")


def batch_trace_safe(model):
    """Conservatively retain legacy observation for custom or nested stateful graphs."""
    pending, seen = [model], set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if getattr(node, "stateful", False):
            return False
        if not type(node).__module__.startswith(("keras.", "tensorflow.", "tf_keras.")):
            return False
        if type(node).__name__ == "Lambda":
            return False
        activation = getattr(node, "activation", None)
        if activation is not None and not getattr(activation, "__module__", "").startswith(
            ("keras.", "tensorflow.", "tf_keras.")
        ):
            return False
        pending.extend(getattr(node, "layers", ()))
    return True


def layer_references(model, inputs):
    """Only compare outputs with an explicit corresponding Keras boundary.

    Fused nonlinear Dense outputs cannot stand in for their preactivation node.
    Unmapped, fused or transformed boundaries are explicitly left unknown.
    """
    import tensorflow as tf

    activation_boundaries = (tf.keras.layers.Activation,)
    # Importing QKeras can initialize Keras globals. Ordinary graphs should not
    # acquire a new optional dependency or change their observation context.
    if any(type(layer).__module__.startswith("qkeras.") for layer in model.layers):
        from qkeras import QActivation

        activation_boundaries += (QActivation,)

    selected = []
    for layer in model.layers:
        activation = getattr(layer, "activation", None)
        if activation is not None and activation != tf.keras.activations.linear:
            if not isinstance(layer, activation_boundaries):
                continue
        if isinstance(layer.output, (list, tuple)):
            continue
        selected.append(layer)
    if not selected:
        return {}
    if not batch_trace_safe(model):
        # Preserve legacy behavior for stateful graphs; batching can change update order.
        values = [
            tf.keras.Model(model.inputs, layer.output)(inputs, training=False) for layer in selected
        ]
    else:
        observer = tf.keras.Model(model.inputs, [layer.output for layer in selected])
        values = observer(inputs, training=False)
        if not isinstance(values, (list, tuple)):
            values = [values]
    return {
        layer.name: value.numpy().reshape(len(inputs), -1).tolist()
        for layer, value in zip(selected, values, strict=True)
    }


def diagnose_layers(graph, original, actual):
    records, first = [], None
    for node in graph.get_layers():
        if node.name not in actual:
            continue
        ref = original.get(node.name)
        comparison = compare_outputs(ref, actual[node.name]) if ref is not None else None
        records.append({"layer": node.name, "comparison": comparison})
        if first is None and comparison is not None and not comparison["passed"]:
            first = node.name
    return {
        "layers": records,
        "first_observed_divergence": first,
        "scope": (
            "mapped layer outputs on supplied inputs; not accumulator internals or board proof"
        ),
    }


def search(deployment, output, layer, role, candidates, *, compiler, trace_layers=False):
    output, deployment, compiler = (
        Path(output).resolve(),
        Path(deployment).resolve(),
        Path(compiler).resolve(),
    )
    output.mkdir()  # Never overwrite a previous search, including a failed one.
    import convert_keras as converter
    import hls4ml
    import numpy as np
    from contract_config import original_output_mode
    from numeric_diagnostics import parameter_grid_diagnostics

    contract = converter.read_yaml(deployment)
    if role not in PRECISION_ROLES or contract.get("io_type") != "io_parallel":
        raise ValueError("supports listed local precision roles and io_parallel only")
    if contract["model"].get("frontend") not in (
        converter.KERAS_FILE_FRONTENDS | converter.KERAS_JSON_FRONTENDS
    ):
        raise ValueError("supports Keras file and JSON with original associated weights only")
    quantized = bool(contract["model"].get("quantized")) or (
        contract["model"].get("frontend") == "qkeras_json_weights"
    )
    if quantized and role in {"weight", "bias", "result"}:
        raise ValueError("quantized model weight, bias and result quantizers must remain original")
    original_output_mode(contract.get("verification") or {})
    model_path = (deployment.parent / contract["model"]["path"]).resolve()
    inputs_path = (deployment.parent / contract["verification"]["inputs"]).resolve()
    original_paths = {"model": model_path, "inputs": inputs_path, "contract": deployment}
    original_paths.update(
        {
            f"associated_file_{i}": (deployment.parent / name).resolve()
            for i, name in enumerate(contract["model"].get("associated_files") or [])
        }
    )
    before = {name: digest(path) for name, path in original_paths.items()}
    template = converter.read_yaml(converter.DEFAULT_TEMPLATE)
    model, _ = converter.load_model(deployment, contract)
    layer_precisions = converter.qkeras_layer_precisions(model) if quantized else {}
    inputs = np.asarray(converter.load_inputs(inputs_path), dtype=np.float32)
    reference = converter.forward_reference(model, inputs, drop_softmax=False)
    reference = reference.reshape(len(inputs), -1).tolist()
    samples = inputs.reshape(len(inputs), -1).tolist()
    write_json(output / "inputs.json", samples)
    write_json(output / "original-python-reference.json", reference)
    original_layers = layer_references(model, inputs) if trace_layers else {}
    if trace_layers:
        write_json(output / "original-layer-reference.json", original_layers)
    result = {
        "hardware_verified": False,
        "original_identity": before,
        "eda_executed": False,
        "started_at": stamp(),
        "original_model_sha256": before["model"],
        "target_layer": layer,
        "parameter_role": role,
        "trace_layers": trace_layers,
        "requested_candidates": list(candidates),
        "input_sha256": before["inputs"],
        "contract_sha256": before["contract"],
        "attempts": [],
        "selected_precision": None,
        "selection_policy": (
            "baseline first, then first passing caller-ordered candidate; not optimum"
        ),
        "compiler": str(compiler),
        "hls4ml": hls4ml.__version__,
        "compiler_sha256": digest(compiler) if compiler.is_file() else None,
        "python": sys.version,
        "status": "NO_PASS",
    }
    write_json(output / "summary.json", result)
    for index, precision in enumerate([None, *candidates]):
        attempt = output / f"attempt-{index:03d}"
        attempt.mkdir()
        trial = {"precision": precision, "started_at": stamp(), "comparison": None}
        log = io.StringIO()
        try:
            config = (
                local_override(contract, layer, role, precision)
                if precision is not None
                else copy.deepcopy(contract)
            )
            write_json(attempt / "contract-configuration.json", config)
            with contextlib.redirect_stdout(log):
                hls_config = converter.build_hls_config(template, config, [], layer_precisions)
                trial["effective_hls_config"] = hls_config
                graph = hls4ml.converters.convert_from_keras_model(
                    model,
                    hls_config=hls_config,
                    output_dir=str(attempt / "project"),
                    part=converter.board_part(deployment, contract),
                    clock_period=float(config["clock_period_ns"]),
                    io_type="io_parallel",
                    backend="Vitis"
                    if config.get("backend") == "hls4ml-vitis"
                    else template.get("Backend", "Vivado"),
                )
                output_grid = (
                    converter.pin_output_grid(graph, config["precision"]) if quantized else None
                )
                trial["effective_hls_config"] = graph.config.config["HLSConfig"]
                target = next((node for node in graph.get_layers() if node.name == layer), None)
                if target is None:
                    raise ValueError("target layer not present in generated graph")
                trial["actual_role_precision"] = role_precision(target, role)
                # Preserve the old parameter field for its existing callers only.
                if role in {"weight", "bias"}:
                    trial["actual_parameter_precision"] = trial["actual_role_precision"]
                if precision is not None:
                    requested = str(graph.config.backend.convert_precision_string(precision))
                    if trial["actual_role_precision"] != requested:
                        raise ValueError("requested local precision was not applied to actual role")
                trial["actual_layer_types"] = {}
                trace_sizes = {}
                for node in graph.get_layers():
                    types = {"result": role_precision(node, "result")}
                    for parameter in (key for key in PRECISION_ROLES if key != "result"):
                        try:
                            types[parameter] = role_precision(node, parameter)
                        except ValueError:
                            pass
                    trial["actual_layer_types"][node.name] = types
                    if trace_layers and node.get_attr("function_cpp"):
                        if len(list(node.get_variables())) != 1:
                            continue
                        node.set_attr("trace", True)
                        trace_sizes[node.name] = node.get_output_variable().size()
                graph.config.trace_output = trace_layers
                trial["parameter_diagnostics"] = parameter_grid_diagnostics(graph)
                graph.write()
                project = attempt / "project"
                uses_po2 = any(
                    "nnet::product::weight_exponential" in p.read_text(encoding="utf-8")
                    for p in (project / "firmware").glob("*.h")
                )
                factor = config.get("softmax_exp_read_factor")
                trial["generated_compatibility"] = {
                    "output_grid": output_grid,
                    "po2": converter.patch_po2_product(project, hls4ml.__version__)
                    if uses_po2
                    else None,
                    "softmax_exp_reads": converter.configure_softmax_exp_reads(
                        project, hls4ml.__version__, factor
                    )
                    if factor is not None
                    else None,
                }
            if len(graph.get_input_variables()) != 1 or len(graph.get_output_variables()) != 1:
                raise ValueError("single input/output required")
            if graph.get_input_variables()[0].size() != len(samples[0]):
                raise ValueError("generated input shape mismatch")
            if graph.get_output_variables()[0].size() != len(reference[0]):
                raise ValueError("generated output shape mismatch")
            actual = host_predict(
                attempt / "project",
                graph.config.get_project_name(),
                samples,
                len(samples[0]),
                len(reference[0]),
                compiler,
                trace_sizes=trace_sizes,
            )
            if trace_layers:
                trace_file = attempt / "host-csim/layer-outputs.json"
                traced = json.loads(trace_file.read_text()) if trace_file.is_file() else {}
                trial["layer_diagnostics"] = diagnose_layers(graph, original_layers, traced)
            trial["comparison"] = compare_outputs(reference, actual)
            trial["status"] = (
                "HOST_NUMERICAL_PASS" if trial["comparison"]["passed"] else "HOST_NUMERICAL_FAIL"
            )
        except Exception as error:
            trial["status"], trial["error"] = "FAILED", f"{type(error).__name__}: {error}"
        finally:
            (attempt / "conversion.stdout.txt").write_text(log.getvalue(), encoding="utf-8")
        trial["observed_at"] = stamp()
        trial["files"] = {
            p.relative_to(attempt).as_posix(): digest(p) for p in attempt.rglob("*") if p.is_file()
        }
        result["attempts"].append(trial)
        write_json(attempt / "result.json", trial)
        result["finished_observation_at"] = stamp()
        if trial["status"] == "HOST_NUMERICAL_PASS":
            result["selected_precision"] = precision
            result["status"] = "BASELINE_HOST_PASS" if precision is None else "LOCAL_HOST_PASS"
        write_json(output / "summary.json", result)
        if result["status"] != "NO_PASS":
            break
    verify_originals(before, original_paths, result, output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layer", required=True)
    parser.add_argument("--role", choices=PRECISION_ROLES, required=True)
    parser.add_argument("--trace-layers", action="store_true")
    parser.add_argument("--candidates", nargs="+", required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    args = parser.parse_args()
    result = search(
        args.deployment,
        args.output,
        args.layer,
        args.role,
        args.candidates,
        compiler=args.compiler,
        trace_layers=args.trace_layers,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "selected_precision": result["selected_precision"],
                "hardware_verified": False,
            }
        )
    )
    return 0 if result["status"] != "NO_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
