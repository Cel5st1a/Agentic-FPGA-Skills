"""Read-only physical-mapping and routing diagnostics; never invokes vendor tools."""

import argparse
import hashlib
import json
import re
from pathlib import Path

RESOURCE_NAMES = (
    "Slice LUTs",
    "CLB LUTs",
    "LUT as Logic",
    "LUT as Memory",
    "Slice Registers",
    "CLB Registers",
    "Block RAM Tile",
    "DSPs",
)


def number(text):
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return None


def resource_value(cells, indices, key):
    index = indices.get(key)
    return number(cells[index]) if index is not None and index < len(cells) else None


def parse_utilization(text):
    resources = dict.fromkeys(RESOURCE_NAMES)
    columns = None
    for line in text.splitlines():
        cells = [x.strip() for x in line.split("|")]
        if "Site Type" in cells:
            columns = {
                name: cells.index(name) for name in ("Used", "Available", "Util%") if name in cells
            }
            continue
        if len(cells) < 3:
            continue
        name = cells[1].rstrip("*").strip()
        if name not in resources:
            continue
        # Headerless rows accept only the exact legacy six-column resource layout.
        indices = (
            columns
            if columns is not None
            else ({"Used": 2, "Available": 5, "Util%": 6} if len(cells) == 8 else {})
        )
        resources[name] = {
            "used": resource_value(cells, indices, "Used"),
            "available": resource_value(cells, indices, "Available"),
            "util_percent": resource_value(cells, indices, "Util%"),
        }
    state = re.search(r"Design State\s*:\s*([^\r\n|]+)", text)
    return {
        "design_state": state.group(1).strip() if state else None,
        "resources": resources,
        "acceptance_decision": None,
        "dominant_layer": None,
        "note": "These are actual report counts at the named design state, not final routed "
        "performance. Top-level counts do not attribute cost to Softmax, core or transport. "
        "If LUT/register cost dominates DSP cost, inspect existing hierarchy/RTL before "
        "assuming larger RF fixes routing. Missing hierarchy remains unknown. No resource gate.",
    }


def seconds(value):
    try:
        parts = value.split(":")
        if len(parts) == 1:
            return float(value)
        if len(parts) == 3:
            hour, minute, second = map(float, parts)
            if 0 <= minute < 60 and 0 <= second < 60:
                return 3600 * hour + 60 * minute + second
    except ValueError:
        pass
    return None


def parse_route(text):
    phases = re.findall(r"^Phase\s+\d+(?:\.\d+)*\s+[^\r\n|]+", text, re.M)
    overlaps = re.findall(r"Number of Nodes with overlaps\s*=\s*(\d+)", text)
    times = re.findall(r"Time \(s\): cpu = ([\d:.]+) ; elapsed = ([\d:.]+)", text)
    valid_times = [(seconds(cpu), seconds(elapsed)) for cpu, elapsed in times]
    valid_times = [
        (cpu, elapsed) for cpu, elapsed in valid_times if cpu is not None and elapsed is not None
    ]
    return {
        "last_phase": phases[-1].strip() if phases else None,
        "last_overlap_count": int(overlaps[-1]) if overlaps else None,
        "overlap_observations": [int(x) for x in overlaps],
        "last_reported_cpu_s": valid_times[-1][0] if valid_times else None,
        "last_reported_elapsed_s": valid_times[-1][1] if valid_times else None,
        "errors": [x for x in text.splitlines() if x.startswith("ERROR:")],
        "physical_completion_verified": False,
        "board_numerical_verified": False,
        "stalled": None,
        "stop_recommended": False,
        "note": "Phase times are cumulative within a tool execution, not added together. "
        "A log snapshot alone cannot prove liveness, stall, convergence, tool completion or "
        "board correctness. Correlate PID plus birth, CPU and log timestamps. Overlaps can "
        "increase at a new iteration. No wall limit or automatic cancellation is introduced.",
    }


def nonlinear_pressure(parameters, defines, activation_header=None):
    """Declared table footprint, never BRAM mapping, capacity or a chosen precision."""

    def clean(text):
        return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)

    parameters, defines = clean(parameters), clean(defines)
    # Preserve different aliases with the same underlying type.
    aliases = {
        alias: underlying
        for underlying, alias in re.findall(r"\btypedef\s+([^;]+?)\s+(\w+)\s*;", defines)
    }

    def width(name):
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name].strip()
        match = re.fullmatch(r"ap_u?(?:fixed|int)\s*<\s*(\d+)\s*(?:,[^<>]*)?>", name)
        return int(match[1]) if match and int(match[1]) > 0 else None

    rows = []
    for name, body in re.findall(r"\bstruct\s+(\w+)\s*:[^{]+\{([^{}]*)\}\s*;", parameters):
        constants = {
            key: int(value)
            for key, value in re.findall(r"\b(?:unsigned|int)\s+(\w+)\s*=\s*(\d+)\s*;", body)
        }
        types = {
            role: underlying.strip()
            for underlying, role in re.findall(r"\btypedef\s+([^;]+?)\s+(\w+)\s*;", body)
        }
        tables = {}
        for role, key in [
            ("exp", "exp_table_size"),
            ("inv", "inv_table_size"),
            ("table", "table_size"),
        ]:
            if key not in constants:
                continue
            bits = width(types.get(role + "_table_t" if role != "table" else "table_t", ""))
            size = constants[key]
            tables[role] = {
                "entries": size,
                "width_bits": bits,
                "storage_bits_single_copy": size * bits if bits else None,
            }
        if not tables:
            continue
        reads = None
        stable_selected = (
            re.search(r"\bimplementation\s*=\s*nnet::softmax_implementation::stable\s*;", body)
            is not None
        )
        if activation_header is not None and stable_selected:
            pattern = (
                r"for\s*\(unsigned i = 0; i < CONFIG_T::n_slice; i\+\+\)\s*\{\s*"
                r"#pragma HLS unroll(?: factor=(\d+))?\s*"
                r"unsigned x = softmax_idx_from_real_val<typename CONFIG_T::inp_norm_t, "
                r"CONFIG_T::exp_table_size>"
            )
            matches = list(re.finditer(pattern, clean(activation_header)))
            lanes = constants.get("n_slice")
            if len(matches) == 1 and lanes:
                reads = min(lanes, int(matches[0][1])) if matches[0][1] else lanes
        rows.append(
            {
                "struct": name,
                "elements": constants.get("n_in"),
                "tables": tables,
                "read_parallelism": reads,
                "measured_resources": None,
                "recommended_table_size": None,
                "note": "Footprints are one declared copy, not physical RAM counts. Read "
                "parallelism is an exact-template source unroll bound, not measured ports. "
                "Unknown types/templates stay null. Consult actual hierarchy/DRC; never "
                "enlarge all types/tables or select a universal RF from these estimates.",
            }
        )
    return rows


def interface_pressure(wrapper):
    """Describe explicit generated beats and ports, without inventing missing geometry."""
    interface = wrapper.get("interface") or {}
    stream = interface.get("stream") or {}
    is_stream = interface.get("style") == "axis_stream_with_ap_ctrl_hs"

    def positive(key):
        value = stream.get(key) if is_stream else None
        return value if type(value) is int and value > 0 else None

    return {
        "input_beats": positive("input_beats_per_sample"),
        "output_beats": positive("output_beats_per_sample"),
        "input_port_bits": positive("tdata_bits"),
        "output_port_bits": positive("output_tdata_bits"),
        "measured_resources": None,
        "capacity_decision": None,
        "note": "A complete sample and an interface beat have different extents. "
        "Inspect whole-sample variable selection, per-lane replication and actual "
        "synthesis hierarchy; io_stream alone does not imply a narrow physical port. "
        "Constant-shift adapters trade input registers for selection logic.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utilization", type=Path, action="append", default=[])
    parser.add_argument("--log", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--defines", type=Path)
    parser.add_argument("--activation-header", type=Path)
    parser.add_argument("--wrapper", type=Path)
    args = parser.parse_args()
    if not (args.utilization or args.log or args.parameters or args.wrapper):
        parser.error("supply an existing report/log, generated parameters or wrapper")
    if (args.defines or args.activation_header) and not args.parameters:
        parser.error("defines/activation-header require generated parameters")
    if args.out.exists():
        parser.error("output exists; use a fresh analysis path")
    report = {
        "schema_version": 1,
        "eda_executed": False,
        "hardware_verified": False,
        "sources": [],
        "analyses": [],
    }
    for kind, paths, analyze in [
        ("utilization", args.utilization, parse_utilization),
        ("route", args.log, parse_route),
    ]:
        for path in paths:
            data = path.read_bytes()  # analyze exactly the bytes whose hash is recorded
            identity = {
                "kind": kind,
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            report["sources"].append(identity)
            report["analyses"].append(
                {"source": identity, **analyze(data.decode("utf-8-sig", errors="replace"))}
            )
    generated = {}
    for kind in ("parameters", "defines", "activation_header", "wrapper"):
        path = getattr(args, kind)
        if path is not None:
            raw = path.read_bytes()
            report["sources"].append(
                {
                    "kind": kind,
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                }
            )
            generated[kind] = raw.decode("utf-8-sig", errors="replace")
    if "parameters" in generated:
        report["nonlinear_pressure"] = nonlinear_pressure(
            generated["parameters"],
            generated.get("defines", ""),
            generated.get("activation_header"),
        )
    if "wrapper" in generated:
        report["interface_pressure"] = interface_pressure(json.loads(generated["wrapper"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(str(args.out))


if __name__ == "__main__":
    main()
