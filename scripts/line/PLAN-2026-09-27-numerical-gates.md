# 13-Skill 转化线路：数值判据与证据门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 逐步执行本计划时使用 test-driven-development（每个任务的红-绿循环）。Steps 使用 `- [ ]` 复选框跟踪。
>
> **本计划的落地位置不是仓库**：项目内固化的 Python 服务（`src/agentic_fpga/*`、`scripts/*.py`、`PilotTools`/`cnn_build`/`desktop_worker`）按操作者指令**视为弃用**，不再作为转化线路的执行体。线路由 13 个 skills 自身承担。
>
> **唯一工作副本：** `C:\Users\Administrator\Agentic_FPGA_Deployment_skills`（操作者 2026-09-27 指定）。所有新增与修改只在此目录进行。

**Goal:** 让 13 个 FPGA skills 成为一条可执行、可自证的转化线路：判据单一来源、逐值证据、数值门强制，未通过数值验证的构建**不允许**被打包成 bitstream 交付。

**Architecture:** 在技能库共享位 `C:\Users\Administrator\Agentic_FPGA_Deployment_skills\scripts\line\` 建一层**零依赖（仅标准库）的判据/证据/门控核心**；每个 skill 的 `scripts/` 下放**薄适配脚本**，调用真实工具（hls4ml / vitis-run / vivado / xsim）并产出统一格式的 `numerical-evidence.json`；门控按 stage 前置关系强制，P5 打包门在任何数值门未 PASS 时拒绝出 release。

**Tech Stack:** Python 3.10（标准库；转换脚本用 `D:\python_envs\hls4ml_conv` 的 TF 2.13.1 + hls4ml 1.3.0）、Bash（Git Bash）、Vitis/Vivado 2025.2、xsim、JSON evidence schema。

**Spec:** 本计划以操作者 2026-09-27 的审查结论为规格，逐条事实来源：
- 审查判定规则（冻结）：`|output − Python| ≤ max(0.001, 4 × 2^−(W−I)) + 0.01 × |Python|`
- 审查逐模型数字：见本计划 §1 表（已由本会话独立复现，逐项吻合）
- 逐值诊断证据：`D:\ML_models\_evidence\six-model-board-20260927-01\`（`hls-cosim/*/numerical-cosim-audit.json`、`sim/qkeras_mnist_cnn_rf16_trace/handshake-audit.json`）
- 现有 SKILL.md 契约：`C:\Users\Administrator\Agentic_FPGA_Deployment_skills\<skill>\SKILL.md`（13 个）

## Global Constraints

- **工作副本**：`C:\Users\Administrator\Agentic_FPGA_Deployment_skills`。`C:\Users\Administrator\.claude\skills` 只是**运行时安装点**，由 Task 1 的单向同步脚本镜像并逐文件校验 SHA-256；禁止两边同时手改（否则又制造"双源真相"）。
- 13 个 FPGA skills 在该工作副本中是**真实目录**（非 `~/.agents/skills` 的 junction）；`D:\DSHtest\install-agent-skills.ps1` 只管理 `~/.agents/skills`，不会覆盖它们。
- 该目录**不在任何 git 仓库内** → 版本安全网必须在本计划内自建（Task 1 的 zip 快照 + 后续计划 N4 的 git 化）。
- **文本编码纪律**：目录内文件为 UTF-8（新写文件**不带 BOM**）。**禁止**用 Windows PowerShell 5.1 的 `Get-Content`/`Set-Content` 对含中文的文件做读-改-写——它把无 BOM 的 UTF-8 当 ANSI/GBK 解码，会产生 U+E000 私有区乱码且**不可逆**（本计划文件在 2026-09-27 曾因此损坏一次，只能重写）。改文件用 Python（显式 `encoding='utf-8'`）或编辑器工具；PowerShell 脚本自身也只写 ASCII。
- 读取 YAML 一律用 `encoding='utf-8-sig'`（项目里已有带 BOM 的 deployment.yaml）。
- 线路核心**只依赖 Python 标准库**；不得 import 项目仓库中的 `agentic_fpga`、`numpy`（转换脚本除外，它们跑在 hls4ml 环境里）。
- 判据**单一来源**：任何 stage 的判定只能来自 `line/contract.py` 的 `Criterion`；禁止在脚本里硬编码 `rho`、`atol`、`rtol` 常量。
- **不放宽任何判据**：ρ、4 LSB 下限、900 s CSynth 预算、`resource_fraction_max` 都不得为了通过而调整。
- **不烧录**：线路到 bitstream 为止（P5 的 `zynq-deploy` 不执行 program/infer）。
- 不修改六个已交付 bitstream；它们作为本计划 Task 11 的**回归测试向量**。
- 模型文件保持冻结（SHA-256 不变）；只允许改部署参数（`reuse_factor`/`io_type`/`strategy`/`precision`/`top_dataflow`）。
- 沿用现有 SKILL.md 的脚本名与参数：**脚本必须存在于 SKILL.md 所写的位置**，名称不一致即视为线路缺陷。

## Review Focus

| 输入/条件 | 合理预期 | 本计划中由谁钉住 |
| --- | --- | --- |
| 声明判据（registry `four_effective_output_lsbs`）与执行判据（代码里的 ρ=0.10）不一致 | 线路必须 FAIL 并指出两处来源，而不是静默用宽松的那个 | Task 4 |
| 仿真工具打印 `INFO: Test PASSED` 但逐值不等 | 不得据此判 PASS | Task 8 |
| 只核验了 1 个样本却声称"验证通过" | 样本数不足必须 FAIL/NOT_RUN | Task 9 |
| 数值门未运行（预算耗尽/跳过） | 状态是 `NOT_RUN`，永远不等于 PASS | Task 5 |
| 资源或时序 OK 但数值错 | 打包门必须拒绝 | Task 10 |
| 参考数组与实际数组形状不同 | 元素数相同按记录在案的 C-order reshape；元素数不同则 FAIL 并给出原因 | Task 3 |
| 逐值证据被事后篡改 | 参考/实际的 SHA-256 写入证据，可复核 | Task 3 |
| SKILL.md 引用了不存在的脚本 | 线路自检必须报出"文档-实现"缺口 | Task 1 / Task 12 |
| 参考值是全零样本（v1 相对项退化） | 判据退化为 `max(0.001, 4 LSB)` 下限，仍可判定 | Task 2 |
| 工作副本与运行时安装点内容漂移 | 同步脚本必须报出哈希差异并以非零退出码失败 | Task 1 |

---

## §1 事实基线（本会话已复现，作为验收数字）

| 模型 | logits 最大误差 | v1 超差 | 需加宽分数位 | 概率对最大误差 | 概率 v1 超差 |
| --- | --- | --- | --- | --- | --- |
| keras_3layer | 0.456771 | 450/500 | +4 | 0.185559 | 182/500 |
| keras_conv1d_small | 0.054639 | 499/500 | +3 | 0.027375 | 320/500 |
| keras_conv2d_model | 0.116351 | 1000/1000 | +5 | 0.036008 | 188/1000 |
| branched_small_ch_last_onnx | 0.036885 | 35656/51200 | +3 | 无 softmax | — |
| autoencoder_hls4ml | 0.000915 | 0/102400 | 0（通过） | 无 softmax | — |
| qkeras_mnist_cnn (rf16) | 0.847168 | 845/1000 | +5 | 0.552734 | 141.5× |

固定装置（Task 11 使用）：`D:\ML_models\_work\<run>\tools\desktop\{python-output.json,hls-output.json,task-*.json,comparison.json}`。

## §2 文件结构（先锁定分解）

```
C:\Users\Administrator\Agentic_FPGA_Deployment_skills\
├── scripts\
│   ├── fpga_env.sh                      (已存在，不动)
│   ├── sync_skills.ps1                  (新增：工作副本 -> .claude\skills 单向镜像 + 哈希校验，ASCII only)
│   └── line\                            (新增：线路共享核心，仅标准库)
│       ├── __init__.py
│       ├── contract.py                  Criterion：两种规则的唯一实现
│       ├── evidence.py                  NumericalEvidence：逐值证据 + SHA-256
│       ├── criterion_check.py           声明判据 vs 执行判据一致性
│       ├── gates.py                     stage 前置关系 + 证据要求 + 状态机
│       ├── inventory.py                 SKILL.md <-> 脚本 存在性盘点
│       ├── verify_runs.py               对既有运行目录复算判定（回归）
│       ├── tests\                       pytest（只用标准库 + pytest）
│       └── state\baseline-<date>.json   盘点与哈希快照
├── hls4ml-convert\scripts\{convert_keras.py,convert_onnx.py}      (SKILL.md 已引用，文件缺失)
├── tensorflow-to-fpga\scripts\{detect_keras.py,convert_keras_to_hls.py}
├── vitis-build-agent\scripts\{build_stage.py,parse_reports.py,rtl_cosim_gate.py,board_sim_gate.py}
├── vivado-manager\{templates\*.tcl,scripts\run_vivado.py}
├── bitstream-package\scripts\package_bitstream.sh
├── log-analyzer\scripts\parse_vitis_log.py
└── workflow-memory\scripts\{query_workflows.py,find_best_config.py}
```

责任边界：`line/*` 只做判定与证据，**不调用任何 EDA 工具**；每个 skill 的脚本负责调工具并调用 `line` 产出证据。

---

## Task 1: 基线快照、文档-实现盘点、工作副本同步

**Files:**
- Create: `scripts/line/__init__.py`（空）
- Create: `scripts/line/inventory.py`
- Create: `scripts/sync_skills.ps1`（ASCII only）
- Test: `scripts/line/tests/test_inventory.py`
- Output: `scripts/line/state/baseline-2026-09-27.json`

**Interfaces:**
- Produces: `inventory.snapshot(skills_dir: Path) -> dict` → `{"schema_version": 1, "generated_at": str, "skills": {name: {"files": {rel: sha256}, "referenced_scripts": [...], "missing_scripts": [...]}}}`
- Produces: `sync_skills.ps1 -Source <dir> -Target <dir> [-WhatIfOnly]`，退出码 0 仅当镜像后逐文件哈希一致

- [ ] **Step 1: 写失败测试**

```python
# scripts/line/tests/test_inventory.py
from pathlib import Path
from line.inventory import snapshot

SKILLS = Path(r"C:\Users\Administrator\Agentic_FPGA_Deployment_skills")
THIRTEEN = ("fpga-env-audit", "compatibility-checker", "hls4ml-convert", "pytorch-to-fpga",
            "tensorflow-to-fpga", "vitis-build-agent", "vivado-manager", "resource-estimator",
            "timing-closure-debugger", "log-analyzer", "workflow-memory", "bitstream-package",
            "zynq-deploy")

def test_snapshot_covers_thirteen_and_hashes_every_file():
    data = snapshot(SKILLS)
    assert set(THIRTEEN) <= set(data["skills"])
    for name in THIRTEEN:
        assert data["skills"][name]["files"], name
        assert all(len(h) == 64 for h in data["skills"][name]["files"].values())

def test_snapshot_reports_documented_but_missing_scripts():
    data = snapshot(SKILLS)
    assert "scripts/convert_keras.py" in data["skills"]["hls4ml-convert"]["missing_scripts"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd C:\Users\Administrator\Agentic_FPGA_Deployment_skills\scripts; python -m pytest line/tests/test_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'line'`

- [ ] **Step 3: 最小实现**

```python
# scripts/line/inventory.py
"""盘点 13 个 skill 的现状：每个文件哈希 + SKILL.md 引用但缺失的脚本。"""
from __future__ import annotations
import hashlib, re
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_REF = re.compile(r"scripts/[A-Za-z0-9_.\-]+\.(?:py|sh)")
TEMPLATE_REF = re.compile(r"templates/[A-Za-z0-9_.\-]+\.(?:tcl|yaml|yml)")

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

def snapshot(skills_dir: Path) -> dict:
    skills_dir = Path(skills_dir)
    skills: dict[str, dict] = {}
    for directory in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = directory / "SKILL.md"
        if not skill_md.is_file():
            continue
        files = {p.relative_to(directory).as_posix(): sha256_file(p)
                 for p in sorted(directory.rglob("*"))
                 if p.is_file() and "__pycache__" not in p.parts}
        text = skill_md.read_text(encoding="utf-8")
        referenced = sorted(set(SCRIPT_REF.findall(text)) | set(TEMPLATE_REF.findall(text)))
        skills[directory.name] = {
            "files": files,
            "referenced_scripts": referenced,
            "missing_scripts": [ref for ref in referenced if ref not in files],
        }
    return {"schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "skills": skills}
```

- [ ] **Step 4: 跑通并落盘基线**

Run: `python -m pytest line/tests/test_inventory.py -v` → 2 passed
Run: `python -c "import json,pathlib;from line.inventory import snapshot;d=pathlib.Path('line/state');d.mkdir(parents=True,exist_ok=True);(d/'baseline-2026-09-27.json').write_text(json.dumps(snapshot(pathlib.Path(r'C:\Users\Administrator\Agentic_FPGA_Deployment_skills')),indent=2,ensure_ascii=False),encoding='utf-8')"`

- [ ] **Step 5: 写同步脚本（ASCII only）**

```powershell
# scripts/sync_skills.ps1 -- one-way mirror: working copy -> runtime install.
# ASCII only: Windows PowerShell 5.1 decodes BOM-less files as ANSI/GBK.
[CmdletBinding()]
param(
  [string]$Source = 'C:\Users\Administrator\Agentic_FPGA_Deployment_skills',
  [string]$Target = 'C:\Users\Administrator\.claude\skills',
  [switch]$WhatIfOnly
)
$ErrorActionPreference = 'Stop'
$names = @('fpga-env-audit','compatibility-checker','hls4ml-convert','pytorch-to-fpga',
           'tensorflow-to-fpga','vitis-build-agent','vivado-manager','resource-estimator',
           'timing-closure-debugger','log-analyzer','workflow-memory','bitstream-package',
           'zynq-deploy','scripts')
foreach ($name in $names) {
  $s = Join-Path $Source $name
  if (-not (Test-Path $s)) { throw "missing source directory: $s" }
  if ($WhatIfOnly) { Write-Host "[whatif] $name"; continue }
  robocopy $s (Join-Path $Target $name) /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $name ($LASTEXITCODE)" }
}
if ($WhatIfOnly) { exit 0 }
$mismatch = 0
foreach ($name in $names) {
  $s = Join-Path $Source $name
  foreach ($f in (Get-ChildItem $s -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' })) {
    $rel = $f.FullName.Substring($s.Length + 1)
    $other = Join-Path (Join-Path $Target $name) $rel
    if (-not (Test-Path $other)) { Write-Host "MISSING in target: $name\$rel"; $mismatch++; continue }
    if ((Get-FileHash $f.FullName -Algorithm SHA256).Hash -ne
        (Get-FileHash $other -Algorithm SHA256).Hash) { Write-Host "HASH DIFF: $name\$rel"; $mismatch++ }
  }
}
Write-Host "sync mismatches: $mismatch"
if ($mismatch -gt 0) { exit 1 }
```

Run: `powershell -File scripts\sync_skills.ps1 -WhatIfOnly` → 列出 14 项
Run: `powershell -File scripts\sync_skills.ps1` → `sync mismatches: 0`

- [ ] **Step 6: 快照存档（安全网）**

Run: `Compress-Archive -Path 'C:\Users\Administrator\Agentic_FPGA_Deployment_skills\*' -DestinationPath 'D:\ML_models\_line\skills-baseline-2026-09-27.zip' -Force`

---

## Task 2: `Criterion` —— 判据的唯一实现（两种规则）

**Files:**
- Create: `scripts/line/contract.py`
- Test: `scripts/line/tests/test_contract.py`

**Interfaces:**
- Produces: `Criterion(rule, atol, rtol, rho, floor, samples)`；`criterion_from_deployment(config: dict) -> Criterion`；`tolerance_for(criterion, reference) -> list[list[float]]`；`effective_lsb(precision: str) -> float`
- Produces: 常量 `RULE_FOUR_LSB = "four_effective_output_lsbs"`、`RULE_RANGE = "range_relative"`

- [ ] **Step 1: 写失败测试（含操作者规则的字面复现）**

```python
# scripts/line/tests/test_contract.py
import pytest
from line.contract import (Criterion, RULE_FOUR_LSB, RULE_RANGE,
                           criterion_from_deployment, tolerance_for)

def test_four_lsb_rule_matches_operator_formula():
    crit = Criterion(rule=RULE_FOUR_LSB, atol=4 * 2 ** -10, rtol=0.01)
    tol = tolerance_for(crit, [[0.0, 10.0]])
    assert tol[0][0] == pytest.approx(max(0.001, 4 * 2 ** -10))
    assert tol[0][1] == pytest.approx(max(0.001, 4 * 2 ** -10) + 0.01 * 10.0)

def test_four_lsb_floor_dominates_for_narrow_precision():
    crit = criterion_from_deployment({"precision": "ap_fixed<24,6>",
                                      "verification": {"rule": RULE_FOUR_LSB, "rtol": 0.01}})
    assert crit.atol == pytest.approx(4 * 2 ** -18)
    assert tolerance_for(crit, [[0.0]])[0][0] == pytest.approx(0.001)

def test_range_rule_uses_max_ptp_rms():
    crit = Criterion(rule=RULE_RANGE, rho=0.10, atol=4 * 2 ** -10)
    assert tolerance_for(crit, [[1.0, 3.0]])[0][0] == pytest.approx(0.10 * (5 ** 0.5))

def test_unknown_rule_is_rejected():
    with pytest.raises(ValueError):
        criterion_from_deployment({"precision": "ap_fixed<16,6>",
                                   "verification": {"rule": "whatever"}})
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest line/tests/test_contract.py -v` → FAIL

- [ ] **Step 3: 最小实现**

```python
# scripts/line/contract.py
"""数值判据的唯一实现。任何 stage 不得另行硬编码阈值。"""
from __future__ import annotations
import math
from dataclasses import dataclass

RULE_FOUR_LSB = "four_effective_output_lsbs"
RULE_RANGE = "range_relative"
RULES = (RULE_FOUR_LSB, RULE_RANGE)
FLOOR = 0.001

@dataclass(frozen=True)
class Criterion:
    rule: str
    atol: float
    rtol: float = 0.01
    rho: float = 0.10
    floor: float = FLOOR
    samples: int = 100

def effective_lsb(precision: str) -> float:
    total, integer = (int(v) for v in
                      precision.removeprefix("ap_fixed<").removesuffix(">").split(","))
    return 2.0 ** -(total - integer)

def criterion_from_deployment(config: dict) -> Criterion:
    verification = config.get("verification") or {}
    rule = verification.get("rule", RULE_FOUR_LSB)
    if rule not in RULES:
        raise ValueError(f"unknown numerical rule: {rule}")
    atol = float(verification.get("atol", 4 * effective_lsb(config["precision"])))
    return Criterion(rule=rule,
                     atol=atol,
                     rtol=float(verification.get("rtol", 0.01)),
                     rho=float(verification.get("rho", 0.10)),
                     floor=float(verification.get("floor", FLOOR)),
                     samples=int(verification.get("samples", 100)))

def _rows(values):
    if isinstance(values, (list, tuple)) and values and isinstance(values[0], (list, tuple)):
        return [list(map(float, row)) for row in values]
    return [[float(v) for v in values]]

_EMPTY_ROW = [0.0]

def tolerance_for(criterion: Criterion, reference) -> list[list[float]]:
    rows = _rows(reference) or [_EMPTY_ROW]
    if criterion.rule == RULE_FOUR_LSB:
        base = max(criterion.floor, criterion.atol)
        return [[base + criterion.rtol * abs(value) for value in row] for row in rows]
    out = []
    for row in rows:
        spread = max(row) - min(row) if row else 0.0
        rms = math.sqrt(sum(v * v for v in row) / len(row)) if row else 0.0
        out.append([max(criterion.rho * max(spread, rms), criterion.atol)] * len(row))
    return out
```

- [ ] **Step 4: 跑通** — 4 passed

- [ ] **Step 5: 用两条真实精度自检**

Run: `python -c "from line.contract import effective_lsb; print(effective_lsb('ap_fixed<16,6>'), effective_lsb('ap_fixed<24,6>'))"`
Expected: `0.0009765625 3.814697265625e-06`

---

## Task 3: `NumericalEvidence` —— 逐值证据

**Files:**
- Create: `scripts/line/evidence.py`
- Test: `scripts/line/tests/test_evidence.py`

**Interfaces:**
- Consumes: `Criterion`, `tolerance_for`
- Produces: `compare(stage, reference, actual, criterion, *, reference_source=None, actual_source=None) -> dict`，字段：`schema_version, stage, criterion, reference_source, actual_source, reference_sha256, actual_sha256, samples{expected,compared}, values{total,violations,first_failing_indices}, max_abs_error, max_error_over_tolerance, per_sample_pass_fraction, reasons, passed`

- [ ] **Step 1: 写失败测试**

```python
# scripts/line/tests/test_evidence.py
from line.contract import Criterion, RULE_FOUR_LSB
from line.evidence import compare

FOUR_LSB = Criterion(rule=RULE_FOUR_LSB, atol=4 * 2 ** -10, rtol=0.01, samples=2)

def test_small_difference_passes_and_records_hashes():
    out = compare("hls_csim", [[0.1, 0.2]], [[0.1, 0.2]], FOUR_LSB)
    assert out["passed"] and out["values"]["violations"] == 0
    assert len(out["reference_sha256"]) == 64 and len(out["actual_sha256"]) == 64

def test_large_difference_fails_with_failing_indices():
    out = compare("hls_csim", [[0.0, 10.0]], [[0.5, 10.0]], FOUR_LSB)
    assert not out["passed"] and out["values"]["violations"] == 1
    assert out["values"]["first_failing_indices"] == [0]

def test_shape_mismatch_is_a_reason_not_a_pass():
    out = compare("hls_csim", [[0.0, 1.0]], [[0.0, 1.0, 2.0]], FOUR_LSB)
    assert not out["passed"] and any("size mismatch" in r for r in out["reasons"])

def test_insufficient_sample_coverage_fails():
    out = compare("board_sim", [[0.1]], [[0.1]], FOUR_LSB)
    assert not out["passed"] and any("sample coverage" in r for r in out["reasons"])

def test_non_finite_value_is_a_reason():
    out = compare("hls_csim", [[0.0]], [[float("nan")]], FOUR_LSB)
    assert not out["passed"] and any("non-finite" in r for r in out["reasons"])
```

- [ ] **Step 2: 运行确认失败** — FAIL

- [ ] **Step 3: 最小实现**

```python
# scripts/line/evidence.py
"""逐值数值证据。样本不足、形状不符、非有限值一律 FAILED 并给出 reason。"""
from __future__ import annotations
import hashlib, json, math
from dataclasses import asdict
from .contract import tolerance_for

SCHEMA_VERSION = 1

def _digest(values) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _rows(values):
    if isinstance(values, (list, tuple)) and values and isinstance(values[0], (list, tuple)):
        return [list(map(float, row)) for row in values]
    return [[float(v) for v in values]]

def compare(stage, reference, actual, criterion, *, reference_source=None, actual_source=None) -> dict:
    out = {"schema_version": SCHEMA_VERSION, "stage": stage, "criterion": asdict(criterion),
           "reference_source": reference_source, "actual_source": actual_source,
           "reference_sha256": _digest(reference), "actual_sha256": _digest(actual),
           "reasons": [], "passed": False}
    ref, act = _rows(reference), _rows(actual)
    if len(ref) != len(act) or any(len(a) != len(b) for a, b in zip(ref, act)):
        out["reasons"].append(
            f"size mismatch: reference {[len(r) for r in ref]} vs actual {[len(r) for r in act]}")
        return out
    if any(not math.isfinite(v) for row in ref + act for v in row):
        out["reasons"].append("non-finite value in comparison")
        return out
    tolerance = tolerance_for(criterion, ref)
    errors = [[abs(r - a) for r, a in zip(ref_row, act_row)]
              for ref_row, act_row in zip(ref, act)]
    flat_errors = [e for row in errors for e in row]
    flat_tol = [t for row in tolerance for t in row]
    violations = [i for i, (e, t) in enumerate(zip(flat_errors, flat_tol)) if e > t]
    margins = [e / t for e, t in zip(flat_errors, flat_tol)]
    per_sample = [max(e) / max(t) for e, t in zip(errors, tolerance)]
    out["samples"] = {"expected": criterion.samples, "compared": len(ref)}
    out["values"] = {"total": len(flat_errors), "violations": len(violations),
                     "first_failing_indices": violations[:20]}
    out["max_abs_error"] = max(flat_errors) if flat_errors else 0.0
    out["max_error_over_tolerance"] = max(margins) if margins else 0.0
    out["per_sample_pass_fraction"] = sum(1 for m in per_sample if m <= 1.0) / len(per_sample)
    if len(ref) < criterion.samples:
        out["reasons"].append(f"sample coverage {len(ref)} < required {criterion.samples}")
    out["passed"] = not out["reasons"] and not violations
    return out
```

- [ ] **Step 4: 跑通** — 5 passed

- [ ] **Step 5: 用真实数据自证（关键）**

Run（在 `scripts` 下）:
```powershell
python -c "import json,pathlib,sys; sys.path.insert(0,'.'); from line.contract import Criterion,RULE_FOUR_LSB; from line.evidence import compare; d=pathlib.Path(r'D:\ML_models\_work\keras_3layer\tools\desktop'); ref=json.loads((d/'python-output.json').read_text(encoding='utf-8')); act=json.loads((d/'hls-output.json').read_text(encoding='utf-8')); c=Criterion(rule=RULE_FOUR_LSB,atol=4*2**-10,rtol=0.01,samples=100); out=compare('desktop',ref,act,c); print(out['values'], out['max_abs_error'], out['passed'])"
```
Expected: `{'total': 500, 'violations': 450, ...} 0.45677... False`（与审查一致）

---

## Task 4: 判据一致性检查（声明 vs 执行）

**Files:**
- Create: `scripts/line/criterion_check.py`
- Test: `scripts/line/tests/test_criterion_check.py`

**Interfaces:**
- Produces: `check_declared(registry_yaml: Path, deployment_yaml: Path) -> dict` → `{"schema_version", "declared", "enforced", "consistent", "reasons"}`

- [ ] **Step 1: 写失败测试**

```python
# scripts/line/tests/test_criterion_check.py
from line.criterion_check import check_declared

REGISTRY = r"D:\Agentic_FPGA_Deployment - 副本\experiments\six_model_baseline\models.yaml"
KERAS3 = r"D:\FPGA-Pilot\FPGA-Pilot-R2\v2-packages\keras_3layer\deployment.yaml"

def test_registry_and_deployment_agree_on_four_lsb():
    out = check_declared(REGISTRY, KERAS3)
    assert out["declared"]["rule"] == "four_effective_output_lsbs"
    assert out["consistent"] is True, out["reasons"]

def test_enforced_criterion_carries_no_rho_for_four_lsb():
    out = check_declared(REGISTRY, KERAS3)
    assert out["enforced"]["rho"] is None
    assert out["enforced"]["rtol"] == 0.01
```

- [ ] **Step 2: 运行确认失败** — FAIL

- [ ] **Step 3: 最小实现**

```python
# scripts/line/criterion_check.py
"""声明判据必须等于执行判据；不一致时线路 FAIL，而不是静默选宽松的那个。"""
from __future__ import annotations
from pathlib import Path
import yaml
from .contract import RULE_FOUR_LSB, criterion_from_deployment, effective_lsb

def check_declared(registry_yaml: Path, deployment_yaml: Path) -> dict:
    registry = yaml.safe_load(Path(registry_yaml).read_text(encoding="utf-8-sig"))
    deployment = yaml.safe_load(Path(deployment_yaml).read_text(encoding="utf-8-sig"))
    declared = dict(registry.get("tolerance") or {})
    reasons = []
    rule = declared.get("rule")
    if rule != RULE_FOUR_LSB:
        reasons.append(f"registry declares unsupported rule: {rule}")
    verification = dict(deployment.get("verification") or {})
    verification.setdefault("rule", rule)
    verification.setdefault(
        "atol", declared.get("fallback_atol", 4 * effective_lsb(deployment["precision"])))
    verification.setdefault("rtol", declared.get("rtol", 0.01))
    merged = dict(deployment, verification=verification)
    enforced = criterion_from_deployment(merged)
    if abs(enforced.rtol - float(declared.get("rtol", 0.01))) > 0:
        reasons.append("rtol differs between declaration and enforcement")
    return {"schema_version": 1, "declared": declared,
            "enforced": {"rule": enforced.rule, "atol": enforced.atol,
                         "rtol": enforced.rtol,
                         "rho": None if rule == RULE_FOUR_LSB else enforced.rho,
                         "samples": enforced.samples},
            "consistent": not reasons, "reasons": reasons}
```

- [ ] **Step 4: 跑通** — 2 passed

- [ ] **Step 5: 对六个部署配置逐个执行**

Run: 对 `D:\FPGA-Pilot\FPGA-Pilot-R2\{v2-packages,opt-packages-3,variant-packages-stream,variant-packages-7,pool-fix-packages}\<model>\deployment.yaml` 调用 `check_declared`，结果写 `scripts/line/state/criterion-consistency.json`
Expected: 六个 `consistent: true`；若某个为 false，即为必须暴露的线路缺陷

---

## Task 5: `gates.py` —— stage 前置关系与"没有证据不许前进"

**Files:**
- Create: `scripts/line/gates.py`
- Test: `scripts/line/tests/test_gates.py`

**Interfaces:**
- Produces: `NUMERICAL`、`STAGE_REQUIREMENTS: dict[str, tuple[str, ...]]`；`class LineState`，方法 `record(stage, *, passed: bool | None) -> dict`、`status(stage) -> str`、`may_run(stage) -> tuple[bool, str]`、`may_package() -> tuple[bool, list[str]]`

- [ ] **Step 1: 写失败测试**

```python
# scripts/line/tests/test_gates.py
from line.gates import LineState

def test_bitstream_requires_numerical_gates():
    state = LineState()
    for stage in ("convert_check", "hls_csim", "hls_synth", "rtl_cosim",
                  "prepare_board", "board_sim", "vivado_synth", "implement"):
        ok, reason = state.may_run(stage)
        assert ok, f"{stage}: {reason}"
        state.record(stage, passed=True)
    ok, reason = state.may_run("bitstream")
    assert ok, reason

def test_missing_numerical_gate_blocks_bitstream():
    state = LineState()
    for stage in ("convert_check", "hls_csim", "hls_synth", "prepare_board", "vivado_synth", "implement"):
        state.record(stage, passed=True)
    ok, reason = state.may_run("bitstream")
    assert not ok and "rtl_cosim" in reason

def test_not_run_is_never_passed():
    state = LineState()
    state.record("rtl_cosim", passed=None)
    assert state.status("rtl_cosim") == "NOT_RUN"

def test_packaging_refuses_without_all_numerical_evidence():
    state = LineState()
    state.record("convert_check", passed=True)
    allowed, blockers = state.may_package()
    assert not allowed and "hls_csim" in " ".join(blockers)
```

- [ ] **Step 2: 运行确认失败** — FAIL

- [ ] **Step 3: 最小实现**

```python
# scripts/line/gates.py
"""线路门控：数值门是 bitstream 与打包的前置条件；NOT_RUN 永远不等于 PASSED。"""
from __future__ import annotations

NUMERICAL = ("convert_check", "hls_csim", "rtl_cosim", "board_sim")
STAGE_REQUIREMENTS = {
    "convert_check": (),
    "prepare_hls": ("convert_check",),
    "hls_csim": ("prepare_hls",),
    "hls_synth": ("prepare_hls",),
    "rtl_cosim": ("hls_synth",),
    "prepare_board": ("hls_synth", "convert_check"),
    "board_sim": ("prepare_board",),
    "vivado_synth": ("prepare_board", "rtl_cosim"),
    "implement": ("vivado_synth",),
    "bitstream": ("implement", "rtl_cosim", "board_sim"),
    "package": ("bitstream", *NUMERICAL),
}

class LineState:
    def __init__(self):
        self._status: dict[str, str] = {}

    def record(self, stage: str, *, passed: bool | None) -> dict:
        self._status[stage] = "NOT_RUN" if passed is None else ("PASSED" if passed else "FAILED")
        return {"stage": stage, "status": self._status[stage]}

    def status(self, stage: str) -> str:
        return self._status.get(stage, "NOT_RUN")

    def may_run(self, stage: str) -> tuple[bool, str]:
        for dependency in STAGE_REQUIREMENTS.get(stage, ()):
            if self.status(dependency) != "PASSED":
                return False, f"PRECONDITION: {dependency} is {self.status(dependency)}"
        return True, ""

    def may_package(self) -> tuple[bool, list[str]]:
        blockers = [f"{stage}: {self.status(stage)}"
                    for stage in STAGE_REQUIREMENTS["package"]
                    if self.status(stage) != "PASSED"]
        return (not blockers), blockers
```

- [ ] **Step 4: 跑通** — 4 passed

- [ ] **Step 5: 回归钉死原始缺陷**

Run: `python -c "import sys;sys.path.insert(0,'.');from line.gates import STAGE_REQUIREMENTS;print('rtl_cosim' in STAGE_REQUIREMENTS['bitstream'], 'board_sim' in STAGE_REQUIREMENTS['bitstream'])"`
Expected: `True True`（旧线路此处为 False，是"能出 bitstream 却未验数值"的根因）

---

## Task 6: P2 转换门脚本（补齐 `hls4ml-convert` 缺失脚本）

**Files:**
- Create: `hls4ml-convert/scripts/convert_keras.py`、`hls4ml-convert/scripts/convert_onnx.py`
- Test: `hls4ml-convert/tests/test_convert_cli.py`（`--dry-run` 路径，不需要 EDA）

**Interfaces:**
- Consumes: `line.contract.criterion_from_deployment`、`line.evidence.compare`
- Produces: `python convert_keras.py --deployment <yaml> --reference <json> --output <dir> --evidence <json> [--dry-run]`；退出码 0 仅当 `convert_check` 数值门 PASSED

- [ ] **Step 1: 写失败测试**：`--dry-run` 用固定装置验证判据接线（读 deployment、读参考、调用 `compare`、写 evidence），不调 hls4ml
- [ ] **Step 2: 运行确认失败**：`python -m pytest hls4ml-convert/tests/test_convert_cli.py -v` → FAIL
- [ ] **Step 3: 实现**：加载模型（`tf.keras.models.model_from_json` + `load_weights`；ONNX 走 `onnx.load_model`）→ `hls4ml.converters.convert_from_*`，参数全部取自 deployment.yaml（precision / reuse_factor / strategy / io_type / top_dataflow / clock_period_ns / part）→ 编译并跑 C++ 与 Python 参考的逐值比较 → 写 `numerical-evidence.json`
- [ ] **Step 4: 跑通**：2 passed
- [ ] **Step 5: 用 `keras_3layer` 真跑一次**（hls4ml_conv 环境）：断言 evidence `passed == false` 且 `values.violations == 450`——**线路必须复现审查结论**

---

## Task 7: P3 构建门脚本（`vitis-build-agent`）

**Files:**
- Create: `vitis-build-agent/scripts/build_stage.py`、`vitis-build-agent/scripts/parse_reports.py`
- Test: `vitis-build-agent/tests/test_parse_reports.py`

**Interfaces:**
- Produces: `build_stage.py --stage {csim,csynth,cosim,impl} --project <dir> --evidence <json>`；`parse_reports.py --report <xml|rpt> -> {dsp, bram, lut, ff, best_clock_ns, latency_cycles}`

- [ ] Step 1：解析器先写测试（对固定 rpt 文本断言字段与数值）
- [ ] Step 2：运行确认失败
- [ ] Step 3：实现解析器（XML `AreaEstimates` / `UserAssignments`）与 CLI（`vitis-run --mode hls --tcl build_prj.tcl`）
- [ ] Step 4：跑通
- [ ] **Step 5**：对 `keras_3layer` 既有 `encoder_csynth.xml` 跑解析，断言与已知值一致（`rf=8` 时 DSP 804 / `rf=16` 时 402）

---

## Task 8: RTL 逐值门 —— 不采信 `INFO: Test PASSED`

**Files:**
- Create: `vitis-build-agent/scripts/rtl_cosim_gate.py`
- Test: `vitis-build-agent/tests/test_rtl_cosim_gate.py`

**Interfaces:**
- Produces: `rtl_cosim_gate.py --csim-log <log> --rtl-log <log> --evidence <json> [--traced]`；输出 `{"passed": bool, "status": "PASSED|FAILED|NOT_RUN", "tool_marker": str, "values": {...}}`

- [ ] **Step 1: 写失败测试（三条）**

```python
def test_marker_without_matching_values_fails(tmp_path):
    # csim/rtl 两份结果日志逐值不同，但 stdout 含 'INFO: Test PASSED'
    ...
    assert result["passed"] is False and result["tool_marker"] == "INFO: Test PASSED"

def test_absent_traced_phase_is_not_run_not_passed(tmp_path):
    ...
    assert result["status"] == "NOT_RUN"

def test_identical_value_logs_pass(tmp_path):
    ...
    assert result["passed"] is True and result["values"]["violations"] == 0
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**：解析两份 `*_results.log` → `line.evidence.compare("rtl_cosim", ...)`；只有"逐值相等 + traced 相位存在"才 PASSED；`INFO: Test PASSED` 只作附注字段，**不参与判定**
- [ ] **Step 4: 跑通**
- [ ] **Step 5: 用审查证据自证**：对 `D:\ML_models\_evidence\six-model-board-20260927-01\hls-cosim\autoencoder_attempt01\numerical-cosim-audit.json` 复算，断言 `passed == false`（732/1024 差异）

---

## Task 9: 板级 N 样本逐值门

**Files:**
- Create: `vitis-build-agent/scripts/board_sim_gate.py`
- Test: `vitis-build-agent/tests/test_board_sim_gate.py`

**Interfaces:**
- Produces: `board_sim_gate.py --response <hex> --expected <json> --contract <json> --samples 100 --evidence <json>`；样本数不足即 FAILED

- [ ] Step 1：测试断言"只解出 1 个样本却声明 100"→ FAILED，reason 含 `sample coverage`
- [ ] Step 2：运行确认失败
- [ ] Step 3：实现 `decode_response` + 逐样本比较 + 证据落盘
- [ ] Step 4：跑通
- [ ] **Step 5**：对 qkeras 的 `handshake-audit.json` 复算，断言输入拍不足（774 < 784）被判 **FAILED**，不是 PASS

---

## Task 10: P5 打包门（`bitstream-package`）

**Files:**
- Create: `bitstream-package/scripts/package_bitstream.sh`（内部调用 `line/gates.py` 的 `may_package()`）
- Test: `bitstream-package/tests/test_package_gate.py`

- [ ] **Step 1: 写失败测试**：证据缺失/FAILED 时脚本退出码非 0 且**不产出** release 目录；只有 `may_package()` 为真才产出，并在 `MANIFEST.json` 写入 `numerical_gates` 逐项状态与每个 evidence 的 SHA-256
- [ ] Step 2：运行确认失败
- [ ] Step 3：实现
- [ ] Step 4：跑通
- [ ] **Step 5**：对现有六个交付包试跑——预期**全部被拒**（这正是线路修复的目标行为），结果写 `scripts/line/state/legacy-packages-rejected.json`

---

## Task 11: 回归——六个既有运行在新线路下的判定

**Files:**
- Create: `scripts/line/verify_runs.py`
- Test: `scripts/line/tests/test_verify_runs.py`（合成向量）

- [ ] **Step 1**：测试：合成向量必须复现 `(violations, max_abs_error)` 的既定值
- [ ] **Step 2**：运行确认失败
- [ ] **Step 3**：实现 `verify_runs.py --runs D:\ML_models\_work --out <json>`，对每个 run 计算 logits 对与 softmax 对的逐值证据
- [ ] **Step 4**：跑通
- [ ] **Step 5**：跑真实数据并与 §1 表**逐格对齐**（450/500、499/500、1000/1000、35656/51200、0/102400、845/1000）；任何一格不符即为线路缺陷

---

## Task 12: 文档对齐与线路自检

**Files:**
- Modify: 各 SKILL.md 中被引用但缺失的脚本条目（Task 1 的 `missing_scripts` 清单）
- Create: `scripts/line/README.md`、`scripts/run_line.sh`
- Test: `scripts/line/tests/test_docs_alignment.py`

- [ ] **Step 1**：测试断言 `inventory.snapshot()` 的 `missing_scripts` 为空，或全部在 SKILL.md 中标注为 deprecated
- [ ] Step 2：运行确认失败
- [ ] Step 3：补齐或下线脚本，写 `run_line.sh`（P1 -> P5，不烧录，逐步产出 evidence）
- [ ] Step 4：跑通
- [ ] **Step 5**：端到端冒烟：`bash scripts/run_line.sh --deployment <keras_3layer.yaml> --workdir D:\ML_models\_line\smoke`，断言在数值门处**按预期失败**（而非产出 bitstream）

---

## 自检（写完计划后已逐项核对）

- **Spec 覆盖**：审查的六个数与两个层级（HLS/Python、RTL/HLS）分别由 Task 3/11 与 Task 8/9 覆盖；口径冲突由 Task 2/4 覆盖；假通过由 Task 5/8/9/10 覆盖；缺脚本由 Task 1/6/7/12 覆盖；工作副本漂移由 Task 1 的同步脚本覆盖。
- **无占位符**：Task 1-5 给了完整可运行代码；Task 6-12 给了接口签名、退出码契约与验收断言，内部实现按同一 TDD 节奏补全。
- **类型一致**：`Criterion` / `compare` / `LineState` 的字段名在 Task 2/3/5 定义，Task 6-11 只消费，不重定义。
- **Review Focus**：表内 10 行各有归属任务，未留空。

## 后续计划（不在本计划内，需单独批准）

- **N1 精度修复线路**：把 §1 的"需加宽分数位"变成自动化的 precision 搜索（P2 门内），并对六个模型重出 bitstream。
- **N2 softmax 端到端**：主机侧 softmax / 精确 IP / 记录为限制，三选一（需要板级 ABI 决策）。
- **N3 pooling RTL 缺陷**：`pooling2d_cl` 在 Vitis 2025.2 DATAFLOW 下的调度缺陷（AE/qkeras），三条候选路线。
- **N4 线路版本化**：把工作副本纳入 git（当前不在任何仓库内），使每个 skill 变更有 commit 可追溯。
