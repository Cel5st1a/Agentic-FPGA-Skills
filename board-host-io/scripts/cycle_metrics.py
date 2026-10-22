"""Define the response counter without mistaking adapter elapsed time for HLS II."""
from __future__ import annotations

import math

from host_geometry import AP_VLD_STYLE, AXIS_STYLE


def cycle_counter_record(wrapper: dict, *, simulation: bool = False) -> dict:
    try:
        style = wrapper["interface"]["style"]
        mhz = wrapper["clock"]["core_mhz"]
        if isinstance(mhz, bool):
            raise ValueError("boolean clock")
        mhz = float(mhz)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cycle counter needs wrapper interface.style and clock.core_mhz") from exc
    if style not in (AXIS_STYLE, AP_VLD_STYLE):
        raise ValueError(f"unsupported cycle counter interface: {style}")
    if not math.isfinite(mhz) or mhz <= 0:
        raise ValueError("cycle counter clock.core_mhz must be finite and positive")
    # simulate_host.render_testbench forces the internal clock with always #5.
    # Report that actual clock rather than silently converting using the board clock.
    return {
        "field": "compute_cycles",
        "metric": "host_adapter_round_trip",
        "unit": "core_clock_cycles",
        "clock_mhz": 100.0 if simulation else mhz,
        "clock_source": "testbench_forced_10ns" if simulation else "wrapper.clock.core_mhz",
        "interface_style": style,
        "start_event": "adapter_samples_transport_start",
        "stop_event": "transport_samples_adapter_done",
        "completion": "matched_output_beat" if style == AXIS_STYLE else "core_ap_done_via_adapter",
        "includes_adapter_wait": True,
        "includes_uart_wire_time": False,
        "is_kernel_latency": False,
        "is_initiation_interval": False,
    }
