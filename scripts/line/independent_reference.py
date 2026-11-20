"""Recompute original-model outputs in the model's own framework environment."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hls4ml-convert/scripts"))
from contract_config import model_frontend  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def keras_reference(frontend, model_file, associated, data):
    import tensorflow as tf

    tf.config.threading.set_intra_op_parallelism_threads(1)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    if frontend in {"keras_archive", "keras_h5"}:
        model = tf.keras.models.load_model(str(model_file), compile=False)
    else:
        if len(associated) != 1:
            raise ValueError("Keras JSON requires exactly one weight file")
        custom = {}
        if frontend == "qkeras_json_weights":
            import qkeras

            qkeras.utils._add_supported_quantized_objects(custom)
        model = tf.keras.models.model_from_json(
            model_file.read_text(encoding="utf-8"), custom_objects=custom
        )
        model.load_weights(str(associated[0]))
    data = data.reshape(len(data), *model.input_shape[1:])
    output = model(data, training=False)
    if isinstance(output, (dict, list, tuple)):
        raise ValueError("independent reference requires one output tensor")
    return output, {"tensorflow": tf.__version__}


def onnx_reference(model_file, associated, data):
    import onnx
    from onnx.external_data_helper import uses_external_data
    from qonnx.core.modelwrapper import ModelWrapper
    from qonnx.core.onnx_exec import execute_onnx

    original = onnx.load(str(model_file), load_external_data=False)
    if associated or any(uses_external_data(tensor) for tensor in original.graph.initializer):
        raise ValueError("independent ONNX reference requires a self-contained model")
    model = ModelWrapper(original)
    weights = {tensor.name for tensor in model.graph.initializer}
    inputs = [value for value in model.graph.input if value.name not in weights]
    if len(inputs) != 1 or len(model.graph.output) != 1:
        raise ValueError("independent ONNX reference requires one input and one output")
    input_tensor, output_tensor = inputs[0], model.graph.output[0]
    input_shape = model.get_tensor_shape(input_tensor.name)
    if not input_shape or input_shape[0] != 1 or any(d <= 0 for d in input_shape):
        raise ValueError("independent ONNX reference requires a static batch-one input")
    dtype = onnx.helper.tensor_dtype_to_np_dtype(input_tensor.type.tensor_type.elem_type)
    rows = data.astype(dtype, copy=False).reshape(len(data), *input_shape[1:])
    results = [
        execute_onnx(model, {input_tensor.name: row[np.newaxis, ...]})[output_tensor.name]
        for row in rows
    ]
    if any(np.ndim(row) == 0 or row.shape[0] != 1 for row in results):
        raise ValueError("independent ONNX reference requires one output per input")
    return np.concatenate(results, axis=0), {"onnx": onnx.__version__, "runtime": "qonnx"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("contract", type=Path)
    p.add_argument("out", type=Path)
    args = p.parse_args()
    contract = yaml.safe_load(args.contract.read_text(encoding="utf-8-sig"))
    model_spec, verification = contract["model"], contract["verification"]
    base = args.contract.parent
    model_file = base / model_spec["path"]
    associated = [base / name for name in (model_spec.get("associated_files") or [])]
    sample_file = base / verification["inputs"]
    frontend = model_frontend(model_spec)
    data = np.asarray(json.loads(sample_file.read_text(encoding="utf-8-sig")))
    if data.ndim < 2 or len(data) == 0 or len(data) != verification["samples"]:
        raise ValueError("input count or shape differs from deployment contract")
    if frontend == "onnx":
        result, runtime = onnx_reference(model_file, associated, data)
    elif frontend in {"keras_archive", "keras_h5", "keras_json_weights", "qkeras_json_weights"}:
        result, runtime = keras_reference(frontend, model_file, associated, data.astype(np.float32))
    else:
        raise ValueError(f"independent reference frontend is unqualified: {frontend!r}")
    tensor = np.asarray(result, dtype=np.float64)
    if (
        tensor.ndim < 2
        or tensor.shape[0] != len(data)
        or not tensor.size
        or not np.isfinite(tensor).all()
    ):
        raise ValueError("independent reference invalid")
    # The wire carries a flat sample in the original framework's tensor order.
    outputs = tensor.reshape(len(data), -1)
    np.save(args.out / "reference.npy", outputs, allow_pickle=False)
    proof = {
        "models": {p.relative_to(base).as_posix(): sha(p) for p in (model_file, *associated)},
        "inputs": sha(sample_file),
        "reference": sha(args.out / "reference.npy"),
        "shape": list(outputs.shape),
        "output_tensor_shape": list(tensor.shape),
        "frontend": frontend,
        **runtime,
    }
    (args.out / "reference.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
