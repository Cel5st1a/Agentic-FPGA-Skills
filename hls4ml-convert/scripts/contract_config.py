"""Contract configuration merging and emitted firmware audit, shared by both frontends."""

import copy
import hashlib
import re
from pathlib import Path


def model_frontend(model):
    """Resolve the legacy Keras label from its explicit storage format."""
    frontend = model.get("frontend")
    if frontend != "keras":
        return frontend
    formats = {
        "keras_json_weights": (".json", 1),
        "keras_h5": (".h5", 0),
        "keras_archive": (".keras", 0),
    }
    storage = model.get("format")
    if storage not in formats:
        raise ValueError(f"frontend 'keras' requires a supported model.format, got {storage!r}")
    suffix, associated = formats[storage]
    model_suffix = Path(model.get("path", "")).suffix.lower()
    if model_suffix != suffix or len(model.get("associated_files") or []) != associated:
        raise ValueError(f"model path or associated files do not match format {storage!r}")
    return storage


def select_backend(contract, template):
    requested = contract.get("backend")
    if requested in (None, ""):
        return template.get("Backend", "Vivado")
    key = str(requested).strip().casefold().removeprefix("hls4ml-")
    vendors = {"vitis": "Vitis", "vivado": "Vivado", "quartus": "Quartus",
               "catapult": "Catapult"}
    if key not in vendors:
        raise ValueError(f"Unsupported explicit hls4ml backend: {requested!r}")
    return vendors[key]


def original_output_mode(verification):
    """This optimization version deploys the complete original network."""
    mode = str(verification.get("gate_output", "auto")).lower()
    if mode not in {"auto", "probabilities"}:
        raise ValueError("complete original model required; logits-only output is unsupported")
    return "probabilities"


def _cpp_struct_fields(text):
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    structs = {}
    for name, parent, body in re.findall(
        r"\bstruct\s+(\w+)\s*(?::\s*([\w:]+))?\s*\{(.*?)\};", text, re.S
    ):
        fields = dict(
            re.findall(
                r"\b(?:static\s+)?(?:const|constexpr)\s+[^;=]+?\s+(\w+)\s*=\s*([^;]+);", body
            )
        )
        value = (parent.split("::")[-1], fields)
        # Multiple incompatible declarations are ambiguous, never pick an arbitrary one.
        structs[name] = value if name not in structs or structs[name] == value else ("", {})
    return structs


def _cpp_field(structs, name, field, seen=()):
    if (name, field) in seen or name not in structs:
        return None
    parent, fields = structs[name]
    if field not in fields:
        return _cpp_field(structs, parent, field, seen + ((name, field),))
    value = fields[field].strip()
    if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*", value):
        literal = value.rstrip("uUlL")
        return int(literal, 16 if literal.lower().startswith("0x") else 10)
    if re.fullmatch(r"\w+", value):
        return _cpp_field(structs, name, value, seen + ((name, field),))
    return None  # Deliberately do not guess arbitrary C++ expressions.


def _cpp_has_field(structs, name, field, seen=()):
    if name in seen or name not in structs:
        return False
    parent, fields = structs[name]
    return field in fields or _cpp_has_field(structs, parent, field, seen + (name,))


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return copy.deepcopy(value)


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else value
        )
    return result


NONLINEAR_TYPE_ROLES = ("table", "exp_table", "inv_table", "inv_inp")


def _bind_nonlinear_precision(block, location):
    """Bind these backend TypeAttributes; Precision alone is ignored by hls4ml 1.3.0."""
    precision = block.get("Precision")
    if not isinstance(precision, dict):
        return
    for role in NONLINEAR_TYPE_ROLES:
        if role not in precision:
            continue
        target = role + "_t"
        aliases = [k for k in block if k.replace("_", "").lower() == target.replace("_", "")]
        value = precision[role]
        if any(block[k] != value for k in aliases):
            raise ValueError(f"{location}.Precision.{role} conflicts with explicit {target}")
        for key in aliases:
            del block[key]
        block[target] = precision.pop(role)


def apply_contract_config(base, contract, skipped_nodes):
    override = _clean({} if contract.get("hls_config") is None else contract["hls_config"])
    if not isinstance(override, dict):
        raise ValueError("hls_config must be a mapping")
    for section, value in override.items():
        if section not in {"Model", "LayerName", "LayerType"} or not isinstance(value, dict):
            raise ValueError(f"hls_config.{section}: expected a Model/LayerName/LayerType mapping")
    model = override.get("Model", {})
    for key in ("ChannelsLastConversion", "TransposeOutputs"):
        if key in model:
            raise ValueError(
                f"hls_config.Model.{key}: unsupported for the already channels-last frontend"
            )
    for key, requested in [
        ("ReuseFactor", contract["reuse_factor"]),
        ("Strategy", contract["strategy"]),
    ]:
        if key in model and model[key] != requested:
            raise ValueError(
                f"hls_config.Model.{key} conflicts with contract: {model[key]} != {requested}"
            )
    precision = model.get("Precision", {})
    default = precision.get("default") if isinstance(precision, dict) else precision
    if default is not None and default != contract["precision"]:
        raise ValueError("hls_config.Model.Precision conflicts with contract.precision")
    layers = override.get("LayerName", {})
    factors = {
        name.lower(): factor for name, factor in (contract.get("layer_reuse_factors") or {}).items()
    }
    base_layers = {name.lower(): block for name, block in base.get("LayerName", {}).items()}
    seen = set()
    for name, block in layers.items():
        if name.lower() in seen:
            raise ValueError(f"hls_config.LayerName.{name}: duplicate case-insensitive layer name")
        seen.add(name.lower())
        if not isinstance(block, dict):
            raise ValueError(f"hls_config.LayerName.{name} must be a mapping")
        factor = factors.get(name.lower())
        if factor is not None and "ReuseFactor" in block and block["ReuseFactor"] != factor:
            raise ValueError(
                f"hls_config.LayerName.{name}.ReuseFactor conflicts with layer_reuse_factors"
            )
        if (
            name.lower() in {node.lower() for node in skipped_nodes}
            and block.get("skip", True) is not True
        ):
            raise ValueError(f"hls_config.LayerName.{name}.skip conflicts with the numerical gate")
        derived = base_layers.get(name.lower(), {}).get("Precision")
        replacement = block.get("Precision")
        if derived and replacement:
            if not isinstance(replacement, dict) or any(
                key in derived and value != derived[key] for key, value in replacement.items()
            ):
                raise ValueError(
                    f"hls_config.LayerName.{name}.Precision conflicts with model quantization"
                )
    for name, block in override.get("LayerType", {}).items():
        if not isinstance(block, dict):
            raise ValueError(f"hls_config.LayerType.{name} must be a mapping")
    merged = _merge(base, override)
    blocks = [("Model", merged.get("Model", {}))]
    blocks.extend(
        (f"{section}.{name}", block)
        for section in ("LayerName", "LayerType")
        for name, block in merged.get(section, {}).items()
    )
    for location, block in blocks:
        if block.get("skip"):
            raise ValueError(f"{location}.skip cannot remove the complete original model")
        if str(block.get("Implementation", "")).strip().lower().split("::")[-1] == "argmax":
            raise ValueError(f"{location}.Implementation argmax cannot replace full Softmax")
    for section in ("LayerName", "LayerType"):
        for name, block in merged.get(section, {}).items():
            _bind_nonlinear_precision(block, f"hls_config.{section}.{name}")
    return merged


def firmware_config_audit(project, requested, effective, nodes=()):
    artifacts = {}
    for name in ("parameters.h", "defines.h"):
        path = project / "firmware" / name
        if path.is_file():
            raw = path.read_bytes()
            artifacts[name] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "text": raw.decode("utf-8", errors="replace"),
            }
    params = artifacts.get("parameters.h", {}).get("text", "")
    defines = artifacts.get("defines.h", {}).get("text", "")
    warnings = []
    for section in ("Model", "LayerName", "LayerType"):
        section_value = requested.get(section) or {}
        blocks = [section_value] if section == "Model" else section_value.values()
        for block in blocks:
            if "TableType" in (block or {}):
                warnings.append(
                    f"{section}.TableType is retained as requested; hls4ml 1.3.0 uses Table_t "
                    "for the emitted lookup type. Inspect emitted typedefs; no alias is applied."
                )
    generated = {
        name.strip(): body
        for name, body in re.findall(
            r"//\s*([^\n]+)\nstruct\s+\w+\s*:[^{]+\{(.*?)\n};", params.replace("\r\n", "\n"), re.S
        )
    }
    generated_names = {
        name.strip(): struct
        for name, struct in re.findall(
            r"//\s*([^\n]+)\nstruct\s+(\w+)\s*:", params.replace("\r\n", "\n")
        )
    }
    header_texts = [params]
    inherited_sources = {}
    for path in sorted((project / "firmware" / "nnet_utils").glob("*.h")):
        raw = path.read_bytes()
        header_texts.append(raw.decode("utf-8", errors="replace"))
        inherited_sources[path.name] = hashlib.sha256(raw).hexdigest()
    structs = _cpp_struct_fields("\n".join(header_texts))
    named = {key.lower(): value for key, value in (effective.get("LayerName") or {}).items()}
    typed = effective.get("LayerType") or {}
    checks, mismatches = [], []
    for node in nodes:
        name = node["name"]
        layer_type = node["class_name"]
        specific = _merge(typed.get(layer_type) or {}, named.get(name.lower()) or {})
        block = _merge(
            _merge(effective.get("Model") or {}, typed.get(node["class_name"]) or {}),
            named.get(name.lower()) or {},
        )
        body = generated.get(name)
        struct = generated_names.get(name, "")
        check = {"name": name, "class_name": node["class_name"]}
        for key, cpp in [
            ("TableSize", "table_size"),
            ("ExpTableSize", "exp_table_size"),
            ("InvTableSize", "inv_table_size"),
            ("ReuseFactor", "reuse_factor"),
        ]:
            if cpp in block:
                if key in block and block[key] != block[cpp]:
                    mismatches.append(f"{name}.{key}: conflicting spelling {cpp}={block[cpp]}")
                block[key] = block[cpp]
            if cpp in specific:
                specific[key] = specific[cpp]
            matches = re.findall(rf"\b{cpp}\s*=\s*(\d+)", body or "")
            required = key in specific or (
                key in block
                and (
                    key != "ReuseFactor"
                    or layer_type
                    in {
                        "Dense",
                        "MatMul",
                        "Conv1D",
                        "Conv2D",
                        "Activation",
                        "Softmax",
                        "BatchNormalization",
                    }
                )
            )
            if key in block and (matches or required):
                actual = int(matches[0]) if matches else None
                actual = _cpp_field(structs, struct, cpp)
                if key == "TableSize" and any(
                    _cpp_has_field(structs, struct, f) for f in ("exp_table_size", "inv_table_size")
                ):
                    actual = {
                        f: _cpp_field(structs, struct, f)
                        for f in ("exp_table_size", "inv_table_size")
                    }
                check[key] = {"requested": block[key], "emitted": actual}
                expected = (
                    {
                        f: block.get(k, block[key])
                        for f, k in [
                            ("exp_table_size", "ExpTableSize"),
                            ("inv_table_size", "InvTableSize"),
                        ]
                    }
                    if isinstance(actual, dict)
                    else block[key]
                )
                if actual != expected:
                    mismatches.append(f"{name}.{key}: requested {block[key]}, emitted {actual}")
        if len(check) == 2:
            check["status"] = "SKIPPED_NON_APPLICABLE"
        checks.append(check)
    verified = any(len(item) > 2 and "status" not in item for item in checks)
    return {
        "status": "FAILED" if mismatches else ("PASSED" if verified else "NOT_RUN"),
        "diagnostic_only": True,
        "layer_checks": checks,
        "mismatches": mismatches,
        "scope": "emitted TableSize/ReuseFactor for resolved HLS nodes; "
        "other attributes require inspection of the recorded firmware and numerical gates",
        "contract_hls_config": requested,
        "hls_config_handed_to_hls4ml": effective,
        "generated_firmware": artifacts,
        "inherited_header_sha256": inherited_sources,
        "table_size_values": sorted(
            set(
                map(
                    int,
                    re.findall(
                        r"\b(?:table_size|exp_table_size|inv_table_size)\s*=\s*(\d+)\s*;", params
                    ),
                )
            )
        ),
        "reuse_factor_values": sorted(
            set(map(int, re.findall(r"reuse_factor\s*=\s*(\d+)", params)))
        ),
        "table_type_declarations": re.findall(r"typedef\s+(.+?)\s+\w+_table_t\s*;", defines),
        "result_type_declarations": re.findall(r"typedef\s+(.+?)\s+result_t\s*;", defines),
        "warnings": warnings,
    }
