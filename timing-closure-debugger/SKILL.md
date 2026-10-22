---
name: timing-closure-debugger
description: >
  Use when Vitis HLS or Vivado timing reports show setup/hold violations,
  a previously working model fails on another FPGA target, or a pipeline,
  DSP mapping or fanout change needs evidence that it affects the failing path.
allowed-tools: Bash, Read, Grep, Glob, Write
---

# Timing Closure Debugger

以真实 routed 路径定位问题，以生成调度/RTL 验证候选生效，再以新 run 的物理门禁判断修复。源码变化、工具接受选项和 HLS 估计均不能替代这条证据链。

本 skill 没有 `parse_timing.py`、`analyze_critical_path.py` 或 `classify_violation.py`。读取实际 run 中的报告；`vivado-manager/scripts/run_vivado.py` 负责生成与解析物理阶段证据。

## 定位

1. 从阶段 evidence 的 `artifacts` 找 `timing_routed.rpt`、`utilization_routed.rpt`、`drc_routed.rpt` 和 routed checkpoint；UART 顶层通常在 `p3-build/host-board`，ROM 顶层在 `p3-build/board`。核对 part 的封装/速度等级、实际核心时钟、板卡 profile 与约束哈希。相同模型和器件系列不代表跨板时序相同。
2. 读取 WNS/TNS/WHS/THS、最差路径的 source/destination、逻辑与路由延迟、DSP 级联、控制网扇出和层级资源。将路径映射回生成的函数/config 及算子；以延迟贡献判断优先级，不用固定 fanout 数值排除问题。
3. HLS 顶层最差估计与 routed 最差实例可能不同。逐函数查实际 CSynth XML 的 estimated clock、worst latency、achieved II 和 pipeline depth；不能把顶层数值归给名字相近的层，也不能把 II 当作延迟。

```powershell
rg -n -A 5 'WNS\(ns\)|Clock Summary' <actual-timing-routed.rpt>
rg -n -A 30 'Slack \(VIOLATED\)|Source:|Destination:' <actual-timing-routed.rpt>
```

## 单候选验证

- 保留失败现场，在 fresh run 写明假设、改动范围和预期变化。修改模板时列出所有受影响实例；针对一个 config 的诊断不意味着模板只影响该 config。
- 先确认本机工具支持的选项、实际调用与作用域，再比较 CSynth 调度、算子绑定和关键 RTL 的寄存器/数据通路。选项被接受但 RTL/调度不变，说明该候选尚无有效结构证据。
- **拆成两个循环、声明中间数组、添加 PIPELINE/UNROLL 不保证新增寄存器边界。** HLS 可合并循环或消去缓存。RTL 哈希变化可能仅来自行号、符号或声明次序；查看实际 diff。若关键逻辑、调度与映射等价，不为该源码候选重复等价物理构建；单独改变物理策略则另记候选和依据。
- 路径证据支持时，评估局部算子延迟/映射、真实进程边界或控制网复制。确认乘积/累加位宽、舍入截断点、加法顺序、数据关联和握手仍符合原契约。DATAFLOW 是候选方法，不是对所有 BN 的默认补丁；normalize 问题按需读 [BN 乘加路径参考](references/normalize-pipeline.md)。
- 每个结构变化候选通过完整 CSim、板级仿真和适用的 host-sim 数值门后，执行新的综合/布局布线。保留实际 latency、II、资源与接口行为的代价；时序通过不表示吞吐改善。
- 更改精度、RF、时钟或 BN 折叠属于契约/算法变更。只有当前任务已授权这些变化才创建明确的契约变体；否则提出方案，不能默改生成配置、约束或判据。合法 RF 由 `reuse-factor-resource-tuning` 评估。

## 结论与交付

| 已有证据 | 可报告结论 |
| --- | --- |
| 参数接受或源码/RTL 哈希变化 | 已尝试；是否有效仍待调度/结构核验 |
| 实际结构/映射变化且仿真通过 | 候选生效、数值门通过；物理时序待验证 |
| 新 routed 报告通过原 setup/hold、资源及 DRC 门禁 | 该 run 的物理门禁通过 |
| 同 run 的配置、完整采集和双参考证据通过 | 才能报告该板卡上数值验证成功 |

报告给出路径归因、前后结构与报告来源、真实门禁值及性能代价；预期收益写成待验证假设，不预填 WNS、资源降幅或成功状态。相同策略再次运行不保证更好结果；不得无依据添加 false path 或放松约束来隐藏失败。恢复与代理验收按 [部署恢复规则](../docs/deployment-recovery.md)。