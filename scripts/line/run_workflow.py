"""Run an Odatix candidate batch, compare four numerical levels and select a measured design."""

import argparse
import csv
import importlib.metadata
import json
import os
import re
import socket
import subprocess
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml
from fixed_reference import verify_fixed
from hls_artifacts import MANIFEST, verify_hls
from independent_board import audit_capture
from odatix_batch import file_digest, read_json, write_json
from workflow_verify import choose, compare_levels, effective_identity

SKILLS = Path(__file__).resolve().parents[2]


def stamp():
    return datetime.now(timezone.utc).isoformat()


def command(arguments, cwd, log, env):
    started = time.monotonic()
    with log.open("xb") as handle:
        result = subprocess.run(
            list(map(str, arguments)), cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT
        )
    write_json(
        log.with_suffix(".command.json"),
        {
            "command": list(map(str, arguments)),
            "exit": result.returncode,
            "wall_s": time.monotonic() - started,
        },
    )
    return result.returncode


@contextmanager
def hardware_server(tool, output):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 3121)) == 0:
            raise RuntimeError("board server is already occupied")
    with (
        (output / "hw-server.stdout").open("xb") as out,
        (output / "hw-server.stderr").open("xb") as err,
    ):
        server = subprocess.Popen(
            [tool, "-s", "tcp::3121"],
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            for _ in range(30):
                if server.poll() is not None:
                    raise RuntimeError("owned hardware server exited")
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", 3121)) == 0:
                        break
                time.sleep(0.5)
            else:
                raise RuntimeError("owned hardware server failed to start")
            yield
        finally:
            if server.poll() is None:
                # The .bat launcher spawns hw_server.exe as a child; terminate() alone
                # leaves that child listening on tcp::3121 and would make the next
                # candidate's hardware phase fail.  Kill the whole process tree.
                subprocess.run(
                    ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=15)


def native_rank(source, destination):
    import odatix.dse.objectives as implementation
    from odatix.dse.objectives import Objective, Objectives, non_dominated_sort

    rows = read_json(source)
    objectives = Objectives([Objective("adapter_cycles"), Objective("dsp")])
    costs = [
        objectives.costs({"adapter_cycles": r["adapter_cycles"], "dsp": r["resources"]["dsp"]})
        for r in rows
    ]
    fronts = [[rows[index]["id"] for index in front] for front in non_dominated_sort(costs)]
    write_json(
        destination,
        {
            "odatix_version": importlib.metadata.version("odatix"),
            "method": "odatix.dse.objectives.non_dominated_sort",
            "fronts": fronts,
            "input_sha256": file_digest(source),
            "costs": [list(cost) for cost in costs],
            "implementation_sha256": file_digest(Path(implementation.__file__)),
        },
    )


def bind_screen_row(output, identifier):
    batch = read_json(output / "scan/b/batch-result.json")
    matches = [row for row in batch["candidates"] if row["id"] == identifier]
    if batch["odatix_returncode"] != 0 or len(matches) != 1:
        raise ValueError("candidate lacks a bound Odatix result")
    job = matches[0]
    folder = output / "scan/b/work/workflows/candidates" / job["token"]
    if (
        Path(job["job_directory"]).resolve() != folder.resolve()
        or job["status"] != "success"
        or not (folder / "workflow_done").is_file()
    ):
        raise ValueError("candidate Odatix job did not complete")
    worker = read_json(folder / "candidate-result.json")
    if worker["status"] != "success" or any(
        worker[k] != job[k] for k in ("token", "key", "payload_digest")
    ):
        raise ValueError("Odatix candidate worker identity differs")
    payload = folder / "payload"
    for name in ("metrics.json", "gates.json"):
        actual = file_digest(payload / name)
        if actual != job["output_sha256"].get(name) or actual != worker["output_sha256"].get(name):
            raise ValueError("candidate metrics or numerical records changed")
    metrics, gates = read_json(payload / "metrics.json"), read_json(payload / "gates.json")
    run = payload / "r/p/c"
    if (
        Path(metrics["run"]).resolve() != run.resolve()
        or metrics["id"] != identifier
        or Path(metrics["contract"]).resolve() != (run / f"contract/p/{identifier}.yaml").resolve()
    ):
        raise ValueError("candidate run escaped its own Odatix job")
    verify_hls(run)
    if file_digest(run / MANIFEST) != metrics["hls_manifest_sha256"]:
        raise ValueError("candidate HLS manifest changed")
    if not metrics["eligible"] or any(
        gates["gates"][key]["status"] != "PASS" for key in ("G1", "G2", "G3")
    ):
        raise ValueError("candidate's first three numerical levels did not pass")
    fixed_path = payload / "g2/reference.json"
    fixed_gate = gates["gates"]["G2"]
    if (
        Path(fixed_gate["reference"]).resolve() != fixed_path.resolve()
        or file_digest(fixed_path) != fixed_gate["reference_sha256"]
    ):
        raise ValueError("independent reference escaped the Odatix numerical record")
    fixed_values = verify_fixed(run / "p2-convert/project", payload / "g2")
    simulation = read_json(run / "evidence/csim-evidence.json")
    sim = simulation["artifacts"]["csim_results"]
    sim_path = run / "p3-build/csim" / sim["file"]
    if sim_path.parent != run / "p3-build/csim" or file_digest(sim_path) != sim["sha256"]:
        raise ValueError("C simulation output differs from its actual artifact")
    if simulation["contract_sha256"] != file_digest(Path(metrics["contract"])):
        raise ValueError("C simulation belongs to another configuration")
    return metrics, fixed_path, fixed_values, sim_path


def physical(run, caps):
    board = run / "p3-build/host-board"
    bit = board / "board_top.bit"
    proof = run / "evidence/host-vivado-bitstream-evidence.json"
    if file_digest(bit) != read_json(proof)["artifacts"]["bitstream"]["sha256"]:
        raise ValueError("bitstream differs from its current build evidence")
    report = read_json(run / "evidence/host-vivado-impl-evidence.json")
    for name in ("routed.dcp", "timing_routed.rpt", "utilization_routed.rpt"):
        if file_digest(board / name) != report["artifacts"][name]["sha256"]:
            raise ValueError("routed physical evidence changed")
    mapping = {"CLB LUTs": "lut", "CLB Registers": "ff", "DSPs": "dsp", "Block RAM Tile": "bram"}
    resources = {mapping[k]: v["used"] for k, v in report["resources"].items() if k in mapping}
    declared = read_json(board / "wrapper.json")["clock"]["core_mhz"]
    clocks = re.findall(
        r"(?m)^[ \t]*clk_raw[ \t]+\{[^}\r\n]+\}[ \t]+"
        r"(\d+(?:\.\d+)?)[ \t]+(\d+(?:\.\d+)?)[ \t]*\r?$",
        (board / "timing_routed.rpt").read_text(encoding="utf8"),
    )
    clock = float(clocks[0][1]) if len(clocks) == 1 and float(clocks[0][0]) == 10 else None
    timing = report["timing"]
    passed = clock == declared == 100 and timing["wns_ns"] >= 0 and timing["whs_ns"] >= 0
    passed = (
        passed
        and set(resources) == set(mapping.values())
        and all(
            0 <= item["used"] <= item["available"]
            for k, item in report["resources"].items()
            if k in mapping
        )
    )
    passed = passed and all(
        value <= caps.get(key, float("inf")) for key, value in resources.items()
    )
    batches = list(board.glob("odatix-*/batch-result.json"))
    owned = []
    for batch in batches:
        data = read_json(batch)
        if data["status"] != "success" or data["odatix_returncode"] != 0:
            continue
        for job in data["candidates"]:
            folder = Path(job["job_directory"])
            marker = folder / "payload/host-build-complete.json"
            if (
                job["status"] == "success"
                and (folder / "workflow_done").is_file()
                and marker.is_file()
            ):
                if file_digest(marker) == job["output_sha256"].get(marker.name):
                    owned.append(str(batch))
    if len(owned) != 1:
        raise ValueError("physical build lacks a unique completed Odatix workflow")
    return {
        "physical_pass": passed,
        "clock_mhz": clock,
        "wns_ns": timing["wns_ns"],
        "whs_ns": timing["whs_ns"],
        "resources": resources,
        "bitstream": str(bit),
        "bitstream_sha256": file_digest(bit),
        "physical_odatix": owned[0],
    }


def full_candidate(row, g1, proof, runtime, output, started, env):
    run, contract = Path(row["run"]), Path(row["contract"])
    verify_hls(run)
    if file_digest(run / MANIFEST) != row["hls_manifest_sha256"]:
        raise ValueError("HLS candidate identity changed after Odatix selection")
    cfg = yaml.safe_load(contract.read_text(encoding="utf8"))
    for name, expected in proof["models"].items():
        if file_digest(contract.parent / name) != expected:
            raise ValueError("candidate changed the original model")
    original = (contract.parent / cfg["verification"]["inputs"]).resolve()
    if file_digest(original) != proof["inputs"]:
        raise ValueError("candidate changed the original inputs")
    folder = output / row["id"]
    folder.mkdir()
    common = [
        runtime["python"],
        SKILLS / "scripts/line/run_uart.py",
        "--deployment",
        contract,
        "--run",
        run,
        "--skills",
        SKILLS,
        "--python",
        runtime["python"],
    ]
    result = {
        "id": row["id"],
        "run": str(run),
        "contract": str(contract),
        "configuration_sha256": file_digest(contract),
        "effective_sha256": effective_identity(run / "p2-convert/project"),
        "comparison": {"complete": False},
    }
    if command([*common, "--reuse-hls", "--build-only"], run, folder / "build.log", env):
        result["failure"] = "physical_build_failed"
        return result
    result.update(physical(run, runtime.get("caps", {})))
    if not result["physical_pass"]:
        result["failure"] = "physical_constraints"
        return result
    with hardware_server(runtime["hw_server"], folder):
        code = command(
            [
                *common,
                "--verify-only",
                "--port",
                runtime["port"],
                "--cable-serial",
                runtime["cable"],
            ],
            run,
            folder / "capture.log",
            env,
        )
    raw = np.asarray(read_json(original), dtype=float)
    capture = run / "hardware-validation"
    audit = audit_capture(
        capture,
        run,
        raw.reshape(len(raw), -1),
        g1,
        started,
        original_path=original,
        cable=runtime["cable"],
        target=runtime["target"],
    )
    if audit["bitstream_sha256"] != result["bitstream_sha256"]:
        raise ValueError("board output belongs to another bitstream")
    gate = row["gate_feedback"]["gates"]["G2"]
    independent = Path(gate["reference"])
    if file_digest(independent) != gate["reference_sha256"]:
        raise ValueError("independent fixed reference changed")
    fixed = read_json(independent)
    for name, expected in fixed["source_sha256"].items():
        if file_digest(Path(name)) != expected:
            raise ValueError("fixed-reference dependency changed")
    simulation = read_json(run / "evidence/csim-evidence.json")["artifacts"]["csim_results"]
    sim_path = run / "p3-build/csim" / simulation["file"]
    levels = {
        "G1": g1.tolist(),
        "G2": fixed["values"],
        "G3": np.loadtxt(sim_path, ndmin=2).tolist(),
        "G4": audit["values"],
    }
    summary = read_json(capture / "summary.json")
    counter = summary.get("cycle_counter", {})
    ticks = summary.get("compute_cycles", [])
    if (
        len(ticks) != len(g1)
        or counter.get("metric") != "host_adapter_round_trip"
        or counter.get("includes_uart_wire_time") is not False
        or counter.get("clock_mhz") != 100
    ):
        raise ValueError("board latency measurement is incomplete")
    result.update(
        {
            "comparison": compare_levels(levels),
            "adapter_cycles": max(ticks),
            "board_audit": audit,
            "program_and_capture_exit": code,
            "level_sources": {
                "G1": str(output / "g1/reference.npy"),
                "G2": str(independent),
                "G3": str(sim_path),
                "G4": str(capture / "events.jsonl"),
            },
        }
    )
    result["level_sha256"] = {k: file_digest(Path(v)) for k, v in result["level_sources"].items()}
    write_json(folder / "four-levels.json", levels)
    with (folder / "four-values.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample", "output", "G1", "G2", "G3", "G4"])
        for i in range(len(g1)):
            for j in range(g1.shape[1]):
                writer.writerow([i, j, *(levels[key][i][j] for key in ("G1", "G2", "G3", "G4"))])
    write_json(folder / "comparison.json", result)
    return result


def finalize(output):
    state = read_json(output / "workflow.json")
    runtime = state["runtime"]
    g1 = np.load(output / "g1/reference.npy", allow_pickle=False)
    proof = read_json(output / "g1/reference.json")
    if file_digest(output / "g1/reference.npy") != proof["reference"]:
        raise ValueError("original reference changed")
    rows = state["measured"]
    verified_rows = []
    for row in rows:
        if not row.get("comparison", {}).get("complete"):
            continue
        metrics, fixed_path, fixed_values, sim_path = bind_screen_row(output, row["id"])
        if row["run"] != metrics["run"] or row["contract"] != metrics["contract"]:
            raise ValueError("measured candidate differs from its Odatix job")
        if (
            Path(row["level_sources"]["G2"]).resolve() != fixed_path.resolve()
            or Path(row["level_sources"]["G3"]).resolve() != sim_path.resolve()
        ):
            raise ValueError("numerical source differs from its bound worker")
        for key, source in row["level_sources"].items():
            if file_digest(Path(source)) != row["level_sha256"][key]:
                raise ValueError("raw numerical evidence changed")
        run, contract = Path(row["run"]), Path(row["contract"])
        cfg = yaml.safe_load(contract.read_text(encoding="utf8"))
        if file_digest(contract) != row["configuration_sha256"] or any(
            file_digest(contract.parent / name) != expected
            for name, expected in proof["models"].items()
        ):
            raise ValueError("candidate or original model changed")
        source = (contract.parent / cfg["verification"]["inputs"]).resolve()
        if file_digest(source) != proof["inputs"]:
            raise ValueError("original verification inputs changed")
        inputs = np.asarray(read_json(source), dtype=float)
        audit = audit_capture(
            run / "hardware-validation",
            run,
            inputs.reshape(len(inputs), -1),
            g1,
            state["started_utc"],
            original_path=source,
            cable=runtime["cable"],
            target=runtime["target"],
        )
        levels = {
            "G1": g1.tolist(),
            "G2": fixed_values,
            "G3": np.loadtxt(sim_path, ndmin=2).tolist(),
            "G4": audit["values"],
        }
        if levels != read_json(output / row["id"] / "four-levels.json"):
            raise ValueError("four-level values differ from their raw sources")
        actual = compare_levels(levels)
        if actual != row["comparison"]:
            raise ValueError("four-level comparison differs from the saved output values")
        if file_digest(Path(row["bitstream"])) != row["bitstream_sha256"]:
            raise ValueError("selected bitstream changed")
        if audit["bitstream_sha256"] != row["bitstream_sha256"]:
            raise ValueError("board capture belongs to another physical bitstream")
        physical_result = physical(run, runtime.get("caps", {}))
        if any(row[key] != value for key, value in physical_result.items()):
            raise ValueError("physical selection metrics differ from their raw reports")
        summary = read_json(run / "hardware-validation/summary.json")
        if max(summary["compute_cycles"]) != row["adapter_cycles"]:
            raise ValueError("latency ranking metric changed")
        effective = effective_identity(run / "p2-convert/project")
        if row.get("effective_sha256", effective) != effective:
            raise ValueError("effective arithmetic identity changed")
        verified_rows.append({**row, "effective_sha256": effective})
    native = read_json(output / "odatix-ranking.json")
    if native["input_sha256"] != file_digest(output / "ranking-input.json"):
        raise ValueError("native ranking input changed")
    if read_json(output / "ranking-input.json") != [
        r for r in rows if r.get("comparison", {}).get("pass")
    ]:
        raise ValueError("native ranking input differs from the measured results")
    runtime = state["runtime"]
    number = len(list(output.glob("ranking-check-*.json"))) + 1
    checked = output / f"ranking-check-{number:02}.json"
    env = {
        k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("API_KEY", "TOKEN"))
    }
    native_python = Path(runtime["odatix"]).with_name("python.exe")
    if command(
        [native_python, __file__, "--native-rank", output / "ranking-input.json", checked],
        output,
        output / f"ranking-check-{number:02}.log",
        env,
    ):
        raise ValueError("native Odatix re-audit failed")
    fresh = read_json(checked)
    if any(
        native[key] != fresh[key]
        for key in ("fronts", "costs", "input_sha256", "method", "odatix_version")
    ):
        raise ValueError("saved Odatix ranking differs from fresh native selection")
    selection = choose(verified_rows, fresh["fronts"])
    state.update(
        status="PASS",
        selection=selection,
        completed_utc=stamp(),
        verified_effective_candidates={r["id"]: r["effective_sha256"] for r in verified_rows},
        native_reaudit=str(checked),
    )
    write_json(output / "workflow.json", state)
    report(output, state)
    return state


def report(output, state):
    lines = [
        "# 四组数值验证与 Odatix 优选报告",
        "",
        f"当前状态为 {state['status']}。",
        "",
        "G1 为原模型，G2 为独立定点运算，G3 为厂商 C 仿真，G4 为实际板卡采集。",
        "",
        "| 候选 | 四组完整 | 六项比较通过 | 板内适配器周期 | DSP | WNS ns |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in state["measured"]:
        comparison = row.get("comparison", {})
        lines.append(
            f"| {row['id']} | {comparison.get('complete', False)} | "
            f"{comparison.get('pass', False)} | {row.get('adapter_cycles', '未测')} | "
            f"{row.get('resources', {}).get('dsp', '未测')} | {row.get('wns_ns', '未测')} |"
        )
        for pair, item in comparison.get("pairs", {}).items():
            lines.append(
                f"\n- {row['id']} {pair} 比较 {item['values']} 个值，"
                f"超差 {item['violations']}，最大绝对误差 {item['max_abs_error']:.9g}。"
            )
        values = output / row["id"] / "four-levels.json"
        if values.is_file():
            example = read_json(values)
            lines.extend(
                [
                    "",
                    f"{row['id']} 的四组数值节选如下。完整数据见 `{row['id']}/four-values.csv`。",
                    "",
                    "| 样本 | 输出 | G1 | G2 | G3 | G4 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for i in range(min(2, len(example["G1"]))):
                for j in range(min(2, len(example["G1"][i]))):
                    lines.append(
                        f"| {i} | {j} | "
                        + " | ".join(f"{example[k][i][j]:.9g}" for k in ("G1", "G2", "G3", "G4"))
                        + " |"
                    )
    lines.extend(["", "## 失败与定位", ""])
    for row in state.get("screening", []):
        if not row.get("eligible"):
            mismatch = row.get("configuration_feedback", {}).get("mismatches", [])
            if mismatch:
                lines.append(f"- {row['id']} 的实际生成配置差异为 {' / '.join(mismatch)}。")
            else:
                lines.append(
                    f"- {row['id']}。{json.dumps(row.get('gate_feedback'), ensure_ascii=False)}"
                )
            if row.get("error_tail"):
                lines.extend(["", "```text", row["error_tail"][-1500:], "```", ""])
    for row in state["measured"]:
        if row.get("failure"):
            lines.append(f"- {row['id']}。{row['failure']}。")
    if state.get("selection"):
        selected = next(r for r in state["measured"] if r["id"] == state["selection"]["selected"])
        cfg = yaml.safe_load(Path(selected["contract"]).read_text(encoding="utf8"))
        lines.extend(
            [
                "",
                "## Odatix 多方案优选",
                "",
                f"获选候选为 {state['selection']['selected']}。",
                f"实际排序为 {' → '.join(state['selection']['ranking'])}。",
                "Odatix 原生非支配排序使用实测适配器周期及 DSP。",
                "同一前沿按周期、DSP、LUT 和候选标识确定顺序。",
                "适配器周期包含核外围等待，串口传输时间另计。",
                f"选中配置的精度为 `{cfg['precision']}`，全局复用因子为 {cfg['reuse_factor']}。",
                "具名层覆盖见所选契约，完整模型与权重身份见原始参考记录。",
                f"实际位流为 `{selected['bitstream']}`。",
            ]
        )
    if state.get("error"):
        lines.append(f"\n流程错误为 {state['error']}。")
    (output / "REPORT.zh-CN.md").write_text("\n".join(lines) + "\n", encoding="utf8")


def workflow(deployment, cases, runtime_path, output):
    deployment, output = deployment.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = read_json(runtime_path)
    started = stamp()
    state = {
        "status": "RUNNING",
        "started_utc": started,
        "measured": [],
        "screening": [],
        "deployment": str(deployment),
        "runtime": runtime,
        "cases": read_json(cases),
    }
    write_json(output / "workflow.json", state)
    env = {
        k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("API_KEY", "TOKEN"))
    }
    env.update(
        S15_ODATIX_EXE=runtime["odatix"], TF_NUM_INTRAOP_THREADS="2", TF_NUM_INTEROP_THREADS="1"
    )
    (output / "g1").mkdir()
    if command(
        [
            runtime["python"],
            Path(__file__).with_name("independent_reference.py"),
            deployment,
            output / "g1",
        ],
        output,
        output / "original-reference.log",
        env,
    ):
        raise ValueError("original model reference failed")
    g1 = np.load(output / "g1/reference.npy", allow_pickle=False)
    proof = read_json(output / "g1/reference.json")
    scan = output / "scan"
    code = command(
        [
            runtime["python"],
            Path(__file__).with_name("scan_hls.py"),
            "--deployment",
            deployment,
            "--cases",
            cases.resolve(),
            "--output",
            scan,
            "--skills",
            SKILLS,
            "--python",
            runtime["python"],
            "--odatix",
            runtime["odatix"],
            "--jobs",
            "1",
            "--finalists",
            str(len(state["cases"])),
            "--four-values",
            "--compiler",
            runtime["compiler"],
        ],
        output,
        output / "odatix-scan.log",
        env,
    )
    if not (scan / "selection.json").is_file():
        detail = (output / "odatix-scan.log").read_text(encoding="utf8", errors="replace")[-3000:]
        state.update(status="ERROR", error=detail, ended_utc=stamp(), odatix_scan_exit=code)
        write_json(output / "workflow.json", state)
        report(output, state)
        return state
    selection = read_json(scan / "selection.json")
    batch = read_json(scan / "b/batch-result.json")
    for row in selection["cases"]:
        job = next(item for item in batch["candidates"] if item["id"] == row["id"])
        payload = Path(job["job_directory"]) / "payload"
        conversion = payload / "r/p/c/evidence/convert-evidence.json"
        if conversion.is_file():
            data = read_json(conversion)
            row["configuration_feedback"] = data.get("configuration_audit", {})
        if not row.get("eligible"):
            log = Path(job["job_directory"]) / "candidate-console.log"
            if log.is_file():
                row["error_tail"] = log.read_text(encoding="utf8", errors="replace")[-3000:]
    state["screening"] = selection["cases"]
    state["odatix_scan_exit"] = code
    for row in selection["cases"]:
        if not row.get("eligible"):
            continue
        try:
            result = full_candidate(row, g1, proof, runtime, output, started, env)
        except (ValueError, AssertionError, OSError, KeyError) as exc:
            result = {"id": row["id"], "comparison": {"complete": False}, "failure": repr(exc)}
        state["measured"].append(result)
        write_json(output / "workflow.json", state)
        report(output, state)
    valid = [r for r in state["measured"] if r.get("comparison", {}).get("pass")]
    state["status"] = "INCOMPLETE"
    write_json(output / "workflow.json", state)
    report(output, state)
    if len(valid) < 2:
        return state
    write_json(output / "ranking-input.json", valid)
    native_python = Path(runtime["odatix"]).with_name("python.exe")
    if command(
        [
            native_python,
            __file__,
            "--native-rank",
            output / "ranking-input.json",
            output / "odatix-ranking.json",
        ],
        output,
        output / "native-ranking.log",
        env,
    ):
        raise ValueError("native Odatix ranking failed")
    return finalize(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-rank", nargs=2, type=Path)
    parser.add_argument("--finalize", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.native_rank:
        native_rank(*args.native_rank)
    elif args.finalize:
        print(json.dumps(finalize(args.finalize)["selection"]))
    else:
        if not all((args.deployment, args.cases, args.runtime, args.output)):
            parser.error("deployment, cases, runtime and fresh output are required")
        try:
            state = workflow(args.deployment, args.cases, args.runtime, args.output)
        except (ValueError, OSError, KeyError, AssertionError) as exc:
            traceback.print_exc()
            path = args.output / "workflow.json"
            if path.is_file():
                state = read_json(path)
                state.update(status="ERROR", error=str(exc), ended_utc=stamp())
                write_json(path, state)
                report(args.output, state)
            raise SystemExit(1) from exc
        print(state["status"], flush=True)
        raise SystemExit(0 if state["status"] == "PASS" else 1)
