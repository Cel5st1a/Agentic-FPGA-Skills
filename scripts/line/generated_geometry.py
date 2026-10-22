"""Explicit hls4ml generated-source rules for AXIS element types and sample counts.

TDATA describes a beat, not a sample. The generated Python bridge supplies complete
tensor extents through convert_data; user reference rows never supply these extents.
Unrecognised generated syntax is an error requiring a new tested rule.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

STREAM_PARAM = re.compile(r'hls::stream\s*<\s*(\w+)\s*>\s*&\s*(\w+)')
ARRAY_TYPE = re.compile(
    r'typedef\s+nnet::array\s*<\s*([^<>]*(?:<[^<>]*>[^<>]*)*)\s*,\s*'
    r'([0-9][0-9*\s]*?)\s*>\s*(\w+)\s*;')
CONVERT = re.compile(
    r'nnet::convert_data\s*<\s*(\w+)\s*,\s*(\w+)\s*,\s*([^>]+)>\s*'
    r'\(\s*(\w+)\s*,\s*(\w+)\s*\)')


def _error(field, expected, actual):
    raise ValueError(f'{field}: expected {expected}; actual {actual!r}')


def _product(expression: str, field: str) -> int:
    if re.fullmatch(r'\s*[0-9]+(?:\s*\*\s*[0-9]+)*\s*', expression) is None:
        _error(field, 'positive literal dimensions multiplied with *; new syntax needs a rule', expression)
    count = 1
    for part in expression.split('*'):
        value = int(part.strip())
        if value <= 0:
            _error(field, 'positive dimensions', expression)
        count *= value
    return count


def stream_facts(verilog_dir: Path, top: str) -> dict:
    firmware = None
    probe = verilog_dir.resolve()
    for _ in range(6):
        candidate = probe / 'firmware'
        if (candidate / 'defines.h').is_file() and (candidate / f'{top}.h').is_file():
            firmware = candidate
            break
        if probe == probe.parent:
            break
        probe = probe.parent
    if firmware is None:
        _error('interface.element_types.firmware', f'{top}.h and defines.h next to generated RTL', None)
    header = firmware / f'{top}.h'
    defines = firmware / 'defines.h'
    parameters = STREAM_PARAM.findall(header.read_text(encoding='utf-8'))
    if len(parameters) != 2:
        _error('interface.element_types', 'exactly one input and one output stream', parameters)
    aliases = {}
    for inner, lanes, name in ARRAY_TYPE.findall(defines.read_text(encoding='utf-8')):
        if name in aliases:
            _error('interface.element_types.' + name, 'unique typedef', name)
        aliases[name] = (inner.strip(), lanes)
    sides = {}
    for side, (type_name, port) in zip(('input', 'output'), parameters):
        inner, expression = aliases.get(type_name, ('', ''))
        match = re.fullmatch(r'ap_fixed\s*<\s*(\d+)\s*,\s*(\d+)\s*>', inner)
        if match is None:
            _error(f'interface.element_types.{side}', 'signed ap_fixed<W,I> generated typedef; no precision fallback', inner)
        bits, integer = map(int, match.groups())
        if not 1 <= integer <= bits:
            _error(f'interface.element_types.{side}', '1 <= I <= W', inner)
        sides[side] = {'type': type_name, 'port': port,
            'lanes': _product(expression, f'interface.stream.{side}_lanes_per_beat'),
            'grid': {'total_bits': bits, 'integer_bits': integer, 'signed': True,
                     'source': 'firmware_defines'}}
    bridge = firmware.parent / f'{top}_bridge.cpp'
    if not bridge.is_file():
        _error('interface.sample_elements.bridge', 'generated bridge with whole-tensor convert_data extents', str(bridge))
    conversions = CONVERT.findall(bridge.read_text(encoding='utf-8'))
    for side, description in sides.items():
        type_name, port = description['type'], description['port']
        sizes = set()
        for source_type, target_type, expression, source, target in conversions:
            is_input = (source_type in ('float', 'double') and target_type == type_name
                        and source == port and target == port + '_ap')
            is_output = (source_type == type_name and target_type in ('float', 'double')
                         and source == port + '_ap' and target == port)
            if (side == 'input' and is_input) or (side == 'output' and is_output):
                sizes.add(_product(expression, f'interface.sample_elements.{side}'))
        if len(sizes) != 1:
            _error(f'interface.sample_elements.{side}', 'one consistent generated bridge extent', sorted(sizes))
        description['elements'] = sizes.pop()
    return {**sides, 'firmware': str(firmware), 'source': 'generated_bridge',
            'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (header, defines, bridge)}}
