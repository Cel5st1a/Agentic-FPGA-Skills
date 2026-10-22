"""Contract configuration merging and emitted firmware audit, shared by both frontends."""
import copy
import hashlib
import re


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return copy.deepcopy(value)


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (_merge(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict) else value)
    return result


def apply_contract_config(base, contract, skipped_nodes):
    override = _clean({} if contract.get('hls_config') is None else contract['hls_config'])
    if not isinstance(override, dict):
        raise ValueError('hls_config must be a mapping')
    for section, value in override.items():
        if section not in {'Model', 'LayerName', 'LayerType'} or not isinstance(value, dict):
            raise ValueError(f'hls_config.{section}: expected a Model/LayerName/LayerType mapping')
    model = override.get('Model', {})
    for key in ('ChannelsLastConversion', 'TransposeOutputs'):
        if key in model:
            raise ValueError(f'hls_config.Model.{key}: unsupported for the already channels-last frontend')
    for key, requested in [('ReuseFactor', contract['reuse_factor']), ('Strategy', contract['strategy'])]:
        if key in model and model[key] != requested:
            raise ValueError(f'hls_config.Model.{key} conflicts with contract: {model[key]} != {requested}')
    precision = model.get('Precision', {})
    default = precision.get('default') if isinstance(precision, dict) else precision
    if default is not None and default != contract['precision']:
        raise ValueError('hls_config.Model.Precision conflicts with contract.precision')
    layers = override.get('LayerName', {})
    factors = {name.lower(): factor for name, factor in (contract.get('layer_reuse_factors') or {}).items()}
    base_layers = {name.lower(): block for name, block in base.get('LayerName', {}).items()}
    seen = set()
    for name, block in layers.items():
        if name.lower() in seen:
            raise ValueError(f'hls_config.LayerName.{name}: duplicate case-insensitive layer name')
        seen.add(name.lower())
        if not isinstance(block, dict):
            raise ValueError(f'hls_config.LayerName.{name} must be a mapping')
        factor = factors.get(name.lower())
        if factor is not None and 'ReuseFactor' in block and block['ReuseFactor'] != factor:
            raise ValueError(f'hls_config.LayerName.{name}.ReuseFactor conflicts with layer_reuse_factors')
        if name.lower() in {node.lower() for node in skipped_nodes} and block.get('skip', True) is not True:
            raise ValueError(f'hls_config.LayerName.{name}.skip conflicts with the numerical gate')
        derived = base_layers.get(name.lower(), {}).get('Precision')
        replacement = block.get('Precision')
        if derived and replacement:
            if not isinstance(replacement, dict) or any(
                    key in derived and value != derived[key] for key, value in replacement.items()):
                raise ValueError(f'hls_config.LayerName.{name}.Precision conflicts with model quantization')
    for name, block in override.get('LayerType', {}).items():
        if not isinstance(block, dict):
            raise ValueError(f'hls_config.LayerType.{name} must be a mapping')
    return _merge(base, override)


def firmware_config_audit(project, requested, effective, nodes=()):
    artifacts = {}
    for name in ('parameters.h', 'defines.h'):
        path = project / 'firmware' / name
        if path.is_file():
            raw = path.read_bytes()
            artifacts[name] = {'sha256': hashlib.sha256(raw).hexdigest(),
                               'text': raw.decode('utf-8', errors='replace')}
    params = artifacts.get('parameters.h', {}).get('text', '')
    defines = artifacts.get('defines.h', {}).get('text', '')
    warnings = []
    for section in ('Model', 'LayerName', 'LayerType'):
        section_value = requested.get(section) or {}
        blocks = [section_value] if section == 'Model' else section_value.values()
        for block in blocks:
            if 'TableType' in (block or {}):
                warnings.append(f'{section}.TableType is retained as requested; hls4ml 1.3.0 uses Table_t '
                                'for the emitted lookup type. Inspect emitted typedefs; no alias is applied.')
    generated = {name.strip(): body for name, body in re.findall(
        r'//\s*([^\n]+)\nstruct\s+\w+\s*:[^{]+\{(.*?)\n};', params.replace('\r\n', '\n'), re.S)}
    named = {key.lower(): value for key, value in (effective.get('LayerName') or {}).items()}
    typed = effective.get('LayerType') or {}
    checks, mismatches = [], []
    for node in nodes:
        name = node['name']
        layer_type = node['class_name']
        specific = _merge(typed.get(layer_type) or {}, named.get(name.lower()) or {})
        block = _merge(_merge(effective.get('Model') or {}, typed.get(node['class_name']) or {}),
                       named.get(name.lower()) or {})
        body = generated.get(name)
        check = {'name': name, 'class_name': node['class_name']}
        for key, cpp in [('TableSize', 'table_size'), ('ReuseFactor', 'reuse_factor')]:
            matches = re.findall(rf'\b{cpp}\s*=\s*(\d+)', body or '')
            required = key in specific or (key in block and (
                key == 'TableSize' or layer_type in {
                    'Dense', 'MatMul', 'Conv1D', 'Conv2D', 'Activation', 'Softmax', 'BatchNormalization'}))
            if key in block and (matches or required):
                actual = int(matches[0]) if matches else None
                check[key] = {'requested': block[key], 'emitted': actual}
                if actual != block[key]:
                    mismatches.append(f'{name}.{key}: requested {block[key]}, emitted {actual}')
        if len(check) == 2:
            check['status'] = 'SKIPPED_NON_APPLICABLE'
        checks.append(check)
    verified = any('TableSize' in item or 'ReuseFactor' in item for item in checks)
    return {'status': 'FAILED' if mismatches else ('PASSED' if verified else 'NOT_RUN'), 'layer_checks': checks,
            'mismatches': mismatches, 'scope': 'emitted TableSize/ReuseFactor for resolved HLS nodes; '
            'other attributes require inspection of the recorded firmware and numerical gates',
            'contract_hls_config': requested, 'hls_config_handed_to_hls4ml': effective,
            'generated_firmware': artifacts,
            'table_size_values': sorted(set(map(int, re.findall(r'table_size\s*=\s*(\d+)', params)))),
            'reuse_factor_values': sorted(set(map(int, re.findall(r'reuse_factor\s*=\s*(\d+)', params)))),
            'table_type_declarations': re.findall(r'typedef\s+(.+?)\s+\w+_table_t\s*;', defines),
            'result_type_declarations': re.findall(r'typedef\s+(.+?)\s+result_t\s*;', defines),
            'warnings': warnings}
