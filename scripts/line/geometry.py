"""Shared producer layout checks and the existing signed fixed-point packing rule.

Consumers may validate samples with these helpers; sample dimensions must never replace
the hardware element counts recorded by a producer.
"""
from __future__ import annotations

import math
import re


def positive_int(value, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f'{field}: expected positive integer; actual {value!r}')
    return value


def parse_precision(text: str) -> tuple[int, int]:
    match = re.fullmatch(r'ap_fixed<(\d+),(\d+)>', str(text).strip())
    if match is None:
        raise ValueError(f'precision: expected ap_fixed<W,I>; actual {text!r}')
    total, integer = map(int, match.groups())
    if not 1 <= integer <= total:
        raise ValueError(f'precision: expected 1 <= I <= W; actual {text!r}')
    return total, integer


def quantise(value: float, total: int, integer: int) -> int:
    """Preserve the verified AP_TRN floor/clamp rule, shared by ROM and UART."""
    scaled = math.floor(float(value) * (2 ** (total - integer)))
    low, high = -(2 ** (total - 1)), 2 ** (total - 1) - 1
    return max(low, min(high, scaled))


def _sample(node, field: str) -> tuple[tuple[int, ...], list[float]]:
    if isinstance(node, (list, tuple)):
        if not node:
            raise ValueError(f'{field}: expected nonempty sample dimension; actual empty')
        children = [_sample(v, f'{field}[{i}]') for i, v in enumerate(node)]
        shape = children[0][0]
        if any(child[0] != shape for child in children):
            raise ValueError(f'{field}: expected rectangular sample; actual ragged layout')
        return (len(node), *shape), [v for _, values in children for v in values]
    if type(node) not in (int, float) or not math.isfinite(node):
        raise ValueError(f'{field}: expected finite numeric sample value; actual {node!r}')
    return (), [float(node)]


def flatten_sample(row, field: str = 'sample') -> list[float]:
    return _sample(row, field)[1]


def sample_rows(payload, field: str = 'samples') -> list[list[float]]:
    if isinstance(payload, dict):
        payload = next((payload[k] for k in ('inputs', 'samples', 'data') if k in payload), None)
    if not isinstance(payload, (list, tuple)) or not payload:
        raise ValueError(f'{field}: expected nonempty sample table; actual {payload!r}')
    parsed = [_sample(row, f'{field}[{i}]') for i, row in enumerate(payload)]
    shape = parsed[0][0]
    if not shape:
        raise ValueError(f'{field}: expected rows of sample values; actual bare scalar')
    if any(item[0] != shape for item in parsed):
        raise ValueError(f'{field}: expected consistent sample shapes; actual ragged rows')
    return [values for _, values in parsed]


def declared_elements(shape) -> int:
    if not isinstance(shape, (list, tuple)) or len(shape) < 2:
        raise ValueError(f'model.input_shape: expected sample axis and element dimensions; actual {shape!r}')
    result = 1
    for index, dimension in enumerate(shape):
        positive_int(dimension, f'model.input_shape[{index}]')
        if index:
            result *= dimension
    return result
