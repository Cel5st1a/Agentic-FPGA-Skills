"""Read-only parameter-grid observations; no model mutation or precision selection."""

import math
import re


def fixed_grid_summary(values, precision):
    result = {
        "precision": str(precision),
        "hardware_verified": False,
        "scope": "observed HLS parameter data; not a proof over all inputs",
        "rounding_effect": "not inferred",
    }
    match = re.fullmatch(r"(?:ap_)?(u?fixed)<\s*(\d+)\s*,\s*(-?\d+)(?:\s*,[^>]*)?>", str(precision))
    if not match:
        return dict(result, status="UNKNOWN_TYPE")
    kind, width, integer = match.groups()
    width, integer = int(width), int(integer)
    if width <= 0 or integer > width:
        return dict(result, status="INVALID_TYPE")
    step = math.ldexp(1.0, integer - width)
    if not step:
        return dict(result, status="UNREPRESENTABLE_HOST_RANGE")
    data = [float(value) for value in values]
    if not data:
        return dict(result, status="NO_VALUES", count=0)
    if any(not math.isfinite(value) for value in data):
        return dict(result, status="NON_FINITE_VALUES", count=len(data))
    signed = kind == "fixed"
    bound = math.ldexp(1.0, integer - int(signed))
    low, high = (-bound if signed else 0.0), bound - step
    return dict(
        result,
        status="OBSERVED",
        count=len(data),
        step=step,
        observed_min=min(data),
        observed_max=max(data),
        representable_min=low,
        representable_max=high,
        sub_lsb_nonzero_count=sum(0 < abs(v) < step for v in data),
        out_of_range_count=sum(v < low or v > high for v in data),
    )


def parameter_grid_diagnostics(hls_model):
    records = []
    for node in hls_model.get_layers():
        for weight in node.get_weights():
            identity = {"layer": node.name, "parameter": weight.name}
            try:
                values = weight.data
                if hasattr(values, "reshape"):
                    values = values.reshape(-1).tolist()
                record = fixed_grid_summary(values, weight.type.precision)
            except (TypeError, ValueError, OverflowError, AttributeError) as exc:
                record = {"status": "UNAVAILABLE", "reason": str(exc), "hardware_verified": False}
            records.append(dict(identity, **record))
    return records
