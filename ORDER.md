# FPGA 模型 → bitstream 与板级验证技能链

本独立版本为 S15-opt4，继承 opt3 工具。新增工具和范围见 [opt3 使用说明](docs/opt3-tools.md)。
宽流式数据移动采用固定移位；输入寄存器增加，不预先保证LUT/FF/布线收益。
QKeras主机诊断保留原量化器；表/并行度摘要只描述生成源码，不决定可部署性。
阶段续作只克隆同组已验证身份的前阶段checkpoint至新目录，未知身份不默认可复用。
这些能力不要求逐项调用，不授权重跑已关闭实验，也不改变下述唯一板端数值验收。

S15-opt 内容身份快照：`scripts/line/identity.py --capture <request.json> --output <new.json>`；
比较：`--compare <previous.json> <current.json> --output <new-report.json>`。
快照读取实际模型/关联权重、算术文件/有效配置、明确提供的工具版本、核心与包装依赖。
文件路径仅溯源，同内容迁移不使核心失效；缺文件/未知版本返回 null。身份匹配只是必要条件，
不鉴证历史数值有效性，不自动复制工件或跳过执行，不新增验收门槛。仅列出的算术文件在范围内，
清单完整性仍需对照产生阶段；不能把历史 bit 存在或 exit0 当可复用证明。
`stage_rtl.dependency_delta.affected_stages` 保留原 RTL 内容范围；跨模型/配置/工具的判断须读取
`reuse_identity_comparison`，不能只看外层列表决定复用。旧记录无身份信息时是未知，仍可正常构建。

本目录是独立 S15-opt 修复副本。原技能、实验冻结输入和历史结果保持不变。

## S15-opt 当前任务规则（优先于下文历史工作流）

- 用户只给原模型、权重与公共输入；本任务要求完整模型真实上板，256 条完整输出满足 `abs(error)<=.001+.001*abs(ref)`。不裁剪 Sigmoid/Softmax，不用答案替代硬件结果。
- 资源比例、预选时钟、时序报告、HLS 对照、cosim、协议诊断和打包完备度不增加验收门槛。真实厂商错误、无法生成或部署的实现仍是实际失败；电气和精确板卡要求保留。
- `verify_board.py` 的 Python 对照决定数值结论，HLS 对照可缺省并单列。必须同时保留真实串口采集、原模型身份、下载来源等执行证据；直接调用纯比较函数并不证明上板。
- 构建或采集失败时定位最小缺项，保留有效核心和每个尝试。成功后停止部署；不得为旧打包器或报告的额外条件重跑。
- 下文旧阈值和固定阶段流程仅是兼容资料。`run_line.py` 与旧 packager 仍是历史确定性构建/审计入口，不是 S15-opt 自主任务必须逐项执行的验收关卡。
- `softmax_exp_read_factor` 是组内可选择的生成调度项，仅在已识别的模板上修改表读取 pragma。保持完整指数、求和、归一化；缺省不改调度，不固定历史模型的 RF、精度或表深度。
- 先看转换证据的 `parameter_grid_diagnostics` 和实际偏差，再分别调整权重、累加和非线性。小于 LSB 不等于已被量化为零；舍入模式、资源和数值收益需要测量。
- `local_precision_search.py --role weight|bias|result|accum|table|exp_table|inv_table|inv_inp --trace-layers` 可显式执行单层单角色主机C++候选。追踪只比较有对应Keras边界的层输出，融合后无法对应的层保持未知；最早观察到偏差的层不是已证明的根因，不代表内部累加轨迹。先跑基线，满足容差即停止；不自动改原契约或替代板测。
- 无默认墙钟或调用次数上限；长时间安静不代表卡死。只按实际进程和日志事实诊断。硬件授权与当前状态以用户和campaign账本为准；本opt3开发副本不授权访问硬件，不得替换任何正在运行的冻结臂。

开始或恢复部署前读 [转换与上板恢复规则](docs/deployment-recovery.md)：核对配置及结构改动实际生效、区分模型与接口包装成本、保存真实阶段状态，并以同一 run 的完整证据验收代理完成状态。监控策略取当前任务授权，实验策略不作为技能默认。

模型结构选择先看 [跨前端效率路径](docs/efficiency-by-model-family.md)：Dense/Sigmoid、深层Dense/Softmax、QKeras、ONNX分支与Conv1D/2D分别诊断，不把某个模型的RF/位宽或修复套到其他模型。

## opt2 最短有效路径（优先于下文历史阶段清单）

- 目标是原模型完整数值上板通过；不以invoke数量或完整跑遍技能链作为效率成果。只读15技能不意味着应启动15套诊断。环境/板卡公开事实从本组输入读取，不重复安装、探查或重做已成立的同组准备。
- 首先检查生成配置与原CPU输出。通过当前容差后不要为更接近零误差反复增加全局位宽、查表深度。失败时先定位输入/层/实际类型，调整最小相关角色；保持完整网络和权重。
- 物理构建前先读实际生成Strategy/kernel及RF；RF大不等于共享生效。Conv2D几何和mult配置由RF工具读取。已有综合报告优先用于定位LUT/FF/BRAM/DSP映射，不为凑诊断加一次EDA。
- 长布线先看日志及所属PID+birth/CPU，不设置墙钟上限。自主候选改变需说明假设和预期映射差异；只改壳保留有效核，只改物理策略保留适用综合结果。无身份/失败工件不能自动复用。
- 用log-analyzer的Python效率摘要读取已有记录；原始日志保留，prompt只提取决策所需片段。不能把RF算术估计、HLS周期、UART整批时间或缓存命中token当成物理性能/实际账单。
- 完整板端数值已通过就停止部署，后续总结离线完成。只给简单一行TASK也应能找到本节；不需要用户提供精度/RF/历史实现或私有参考。

## 一、阶段顺序（P1 → P5）

| 阶段 | skill | 作用 | 产物 |
| --- | --- | --- | --- |
| P1 | `fpga-env-audit` | 检查工具链/环境是否可用 | `p1-env/` 下的审计输出 |
| P1 | `compatibility-checker` | 版本组合是否兼容 | 兼容性结论 |
| P2 | `tensorflow-to-fpga` / `pytorch-to-fpga` | 按框架选路径 | 路径说明 |
| P2 | `hls4ml-convert` | 模型 → hls4ml HLS 工程 | `p2-convert/project` + `evidence/convert-evidence.json` |
| P3 | `vitis-build-agent` | CSim / CSynth；Cosim 仅显式诊断 | `p3-build/{csim,csynth,cosim}` + 证据 JSON |
| P3 | `vivado-manager` | 板级顶层 + 仿真 + 综合/实现/bitstream | `p3-build/board` + 证据 JSON |
| P3/P5 | `board-host-io` | 从同一 run 的核生成 UART 主机顶层；审计 routed 端口；受控 JTAG 与逐值上板验证 | `p3-build/host-board`、`evidence/io-port-audit`、`hardware-*` |
| P3 | `reuse-factor-resource-tuning` | 资源超限时定位吃面积的层、算逐层合法 reuse factor、给出契约变体（**咨询性，不改变门禁结论**） | `tune_reuse_factor.py` 的 JSON/终端输出 |
| P4 | `resource-estimator` | 资源估计 | 资源视图（本链以布线后利用率报告为准） |
| P4 | `timing-closure-debugger` | 时序未收敛时的定位 | 报告 |
| P4 | `log-analyzer` | 工具日志解析 | 报告 |
| P5 | `bitstream-package` | 打包（**拒绝未通过门禁的构建**） | `<release-root>/<model-id>/` |
| P5 | `workflow-memory` | 记录成功/失败组合 | `workflow-memory/examples/*.json` |
| P5 | `zynq-deploy` | Zynq 部署背景材料（本 Artix-7 路径不调用） | 无 |

## 二、历史判定标准（兼容记录，不适用于本次用户验收）

逐值判定：

```
|out - py| <= max(0.001, 4 * 2^-(W-I)) + 0.01 * |py|
```

* `W,I` 来自契约的 `precision`（例如 `ap_fixed<20,6>` → `W-I = 14`）。
* 样本数取契约 `verification.samples`，默认 100；覆盖不足即失败。
* 唯一实现：`scripts/line/contract.py`（判据）+ `scripts/line/evidence.py`（逐值比较与证据）。
* **工具退出码不是数值结论**；`NOT_RUN` 永远不等于 `PASSED`。

## 三、运行目录契约

一次尝试 = 一个 run 目录，开头由 `scripts/line/bootstrap_run.py` 用契约物化，同一契约的开局逐字节相同：

```
runs/<model-id>/<run-id>/
├── contract/<pkg>/deployment.yaml   冻结的部署契约（原样复制，与源文件逐字节相同）
├── starting-state.json              开局指纹（契约、板卡文件、引脚 profile、模型与验证文件哈希）
├── run-plan.json                    所选技能树、阶段、解释器及板卡身份哈希
├── RUN.md                           本次运行的开局与命令
├── evidence/*.json                  每阶段证据（唯一的结论来源）
├── p1-env/                          环境审计输出
├── p2-convert/project/              hls4ml 工程
├── p3-build/{csim,csynth,cosim,board}/
├── p4-analysis/                     资源/时序/日志分析
├── p5-package/                      打包中间产物
└── logs/
```

"开局相同"指的是**契约副本与模型/验证文件逐字节相同**（`starting-state.json` 里的哈希可核对）；
`RUN.md` 与 `starting-state.json` 自身含本次运行的路径，因此按运行不同，这是记录而非输入。

## 四、入口

```powershell
# 1) 物化开局（同一契约 → 逐字节相同的起点）
python scripts/line/bootstrap_run.py --deployment <deployment.yaml> --run <run-id> [--root <runs-root>] [--skills <skills-root>]

# 2) 按阶段驱动整条链（推荐一次跑完；也可用 --stages 选择）
python scripts/line/run_line.py --deployment <run>/contract/<pkg>/deployment.yaml --run <run> `
    --stages convert,csim,csynth,wrapper,boardsim,synth,impl,bitstream,ports,package
```

`run_line.py` 覆盖 P2–P5 的 ROM/configure-only 构建与 routed 端口审计；`ports` 阶段必须读
`routed.dcp`，生成 `io-scope.json`，打包器核对其哈希和顶层端口后才交付。
无主机通路的交付包标 `configure-only`，`on_board_numerical=NOT_RUN`。
`board-host-io` 的 UART 变体按其 SKILL.md 单独串行运行，不复用 ROM 位流，也不把旧模型的上板
PASS 移植到新模型。P1 的审计脚本与 P4 的
`resource-estimator` / `timing-closure-debugger` / `log-analyzer` 仍按各自 SKILL.md 单独调用
（本链的布线后资源与时序结论以 `vivado-manager` 的报告为准，分析类 skill 不改变门禁结论）。

#### `board-host-io` 的几何真值源规则

**每个几何量只有一个权威来源：生成端确定并记录，下游读取、核对，不得二次推导。**
`model.input_shape` 是生成端用来检查样本布局的声明，不是主机元素数来源。
契约 `precision` 是冻结数值判据与网格一致性约束；不能用它补缺失的构建网格。
agent 的职责是读字段、核对来源与门禁；遇到缺字段或未知接口规则，具名拒绝，不能自创公式。

| 几何量 | 权威来源 | 核对规则 |
| --- | --- | --- |
| 输入元素数、顺序 | ROM `wrapper.json` 的 `input_layout.elements/flatten` | `flatten=c_order`；核对接口元素数、实际输入位宽与样本 |
| 输出元素数、顺序 | 新记录 `output_layout.elements/flatten` | AXIS 核对 `interface.stream.output_elements`；ap_vld 核对有序标量端口映射 |
| 输入网格 | `rom_image.grid` | 与契约 precision、AXIS `element_types.input` 相等 |
| 输出网格 | `output_grid` | 与契约 precision、AXIS `element_types.output` 相等；ap_vld 核对每个输出端口位宽 |
| lane、拍数、TDATA | `interface.stream` | 输入/输出拍数分别 = 对应元素数 / 对应 lane 数且整除；整拍多输出拍按窗口聚合；lane 位宽等于元素位宽；实际 TDATA 整字节 |
| UART 载荷、总线、帧长、scale、波特率 | 主机生成时保存的 `host_geometry` | 消费时检查字段关系及 `host_protocol` 投影；禁止用新推导值替换记录 |

旧记录仅允许两条明确的输出计数规则：AXIS 读取已有 `stream.output_elements`；
ap_vld 校验 `result_outputs` 非空、无重复、有序，且每个 `output_bits` 恰好等于输出元素位宽后，
按一端口一标量读取。打包多元素端口不适用该规则。缺 `input_layout` 或 `output_grid` 不得从
契约形状、precision 或 references 补值；须在新 run 重新生成完整记录，不改旧 run。
旧主机记录若未保留 `rom_image`，只允许从其 `source_wrapper` 指向、且 SHA-256 验证通过的
同契约生成记录读取网格，不能从契约推断。

生成端样本布局检查与量化规则共用 `scripts/line/geometry.py`。`run_line.py` 只传输入/参考数据来源，
不再自行计算元素数。references 只核对每样本展平后的值数与矩形布局，不决定输出几何。
AXIS 生成端通过 `scripts/line/generated_geometry.py` 的显式规则读取生成的头文件、
`defines.h` 和 `<top>_bridge.cpp`：类型定义提供有符号网格与 lane 数，
bridge 的 `convert_data` 完整张量范围提供每样本元素数；三份文件哈希保存于 `geometry_files`。
缺失文件、无符号类型或未覆盖语法必须具名拒绝，不能用契约 precision、references 或 TDATA 除法补猜。
同一事实出现在多个记录字段时，它们是同源投影；冲突必须拒绝，不能选择一个“优先值”继续。

主机包装器仅在生成时由 `board-host-io/scripts/host_geometry.py` 物化以下关系：

```
in_bits = input_layout.elements * rom_image.grid.total_bits
out_bits = output_layout.elements * output_grid.total_bits
payload_bytes = ceil(in_bits / 8)
response_bytes = ceil(out_bits / 8)
REQUEST_BYTES = payload_bytes + 8
RESPONSE_BYTES = response_bytes + 12
scale_in = 1 << (input_total_bits - input_integer_bits)
scale_out = 1 << (output_total_bits - output_integer_bits)
```

元素 i 占位 `[i*W +: W]`，元素 0 在低位字，不逐元素补齐整字节；仅总线向上取整。
生成时的波特率优先级保持为契约 `host_transport.baudrate`、板级档案 `peripherals.uart.baudrate`、
已验证默认 115200，并记录来源。消费既有主机 wrapper 时读取保存的波特率，不能因外部档案改变
而重选；契约身份不符则拒绝并要求新 run。板级事实 AX7A200B、L14/L15、LVCMOS33、DIFF_SSTL15 不变。

选哪个适配器由核的**实际端口**（`interface.style`）决定，不看契约里 `io_type` 的字面值：

| `interface.style` | 适配器 |
| --- | --- |
| `ap_vld_array_with_ap_ctrl_hs` | `assets/host_avld_adapter.v`（锁存整字、`ap_start` 保持电平到 `ap_done`；D-M01） |
| `axis_stream_with_ap_ctrl_hs` | `assets/host_stream_adapter.v`（保持核运行、注入一帧、按序数关联输出） |

`io_serial` 不产生第三条线：hls4ml 给它的要么是数组端口、要么是流端口，落入上表其一。

**几何的门禁（不得放宽）**：轴路径的元素网格必须**等于**契约 `precision`（判据的 LSB 项来自
契约、线上尺度来自元素网格，不同即不在同一把尺子上）；输入可由多拍组成，拍数必须等于
输入元素数除以输入 lane 数且整除。输出每样本仍**恰好一拍**，输出 lane 数等于输出元素数。
lane 位宽等于元素网格，TDATA 必须等于实际一拍的 lane 数乘元素位宽且整字节。
新适配器按完整输入窗口关联一个按序输出；不得删除网格、端口或覆盖校验。
生成/读取共用 `host_geometry.geometry_for_wrapper()` 入口：ROM 记录进入生成分支，
主机记录进入已物化几何读取分支；`protocol.py` 只消费传入的 Geometry。
缺字段、冲突、未知 style 的错误必须包含字段路径、期望规则与实际值。

已验证几何 `64 × ap_fixed<32,12> → 4` 代入后必须与旧常量逐项相等，且请求字节必须与改前**逐字节相同**；
这两条由 `board-host-io/tests/data/` 的黄金帧与
`tests/test_protocol_geometry.py` 钉住，不是口头等价。

`transport.v` 是唯一合法携带几何的已验证资产：长度与位宽被提为参数，
**状态机/CRC/超时/`QUARANTINE` 一字未改**；改前原文冻结在
`board-host-io/references/frozen/transport-96ceb796.v`，由结构差分测试与
"冻结原版 vs 参数化版逐周期等价"的 xsim 测试双重把守。再改该文件必须先重新满足这两个测试，
再更新 `make_host_board.py` 的 `SOURCE_HASHES`。

P3 的 `reuse-factor-resource-tuning` 同样是**单独调用**的咨询步骤，不接入 `run_line.py`：
`run_line.py` 的确定性是这条线的核心资产，而该技能是"资源门挡住之后，人来决定改哪个参数"的
一步。它只读契约与生成的 `firmware/parameters.h`，不跑 EDA、不写 run 目录。

`cosim` 不在默认 `STAGE_ORDER` 中：本项目尚无 traced-RTL Cosim 成功证据，较大核实测卡在 0%。
只有显式 `--stages cosim` 才调用，默认诊断预算 300 秒（`--timeout-cosim`）；不得把 C-wrapper
日志当成 RTL 通过。未调用时 `rtl_cosim=NOT_RUN`；RTL 级逐值证据由 `boardsim` 提供。

`run_line.py` 只从契约取参数（模型文件、输入形状、精度、reuse factor、strategy、io 类型、
时钟、板卡档案、样本文件、资源上限），并按阶段检查证据里的结论；某阶段证据不是 `PASSED`、
数值门未通过、或 impl/bitstream 的时序 `met != true`，就立即停下，不会用退出码顶替结论。

### 转换解释器（按前端选择，不是按模型）

| `model.frontend` | 解释器 | 原因 |
| --- | --- | --- |
| `keras_json_weights` / `qkeras_json_weights` / `keras_h5` / `keras_archive` | `D:\python_envs\hls4ml_conv\Scripts\python.exe` | TF 2.13.1 (Keras v2) + hls4ml 1.3.0 |
| `onnx` | `D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe` | 额外含 onnxruntime + qonnx；qonnx 才能执行 channels-last 自定义域图并生成 Python 参考 |

## 五、板卡档案

`board-profiles/<BOARD>/board_profile.yaml` 除器件/时钟源外，还声明 `board_pins`
（时钟差分对与 IOSTANDARD、复位、状态 LED）及其出处。生成顶层与 XDC 的工具从这里取引脚，
**不允许猜引脚**；命令行参数优先级高于档案。AX7A200B 的时钟 IOSTANDARD 覆盖（`DIFF_SSTL15`）
带出处与 `requires_operator_confirmation` 标记，见 DEFECTS D-019。

## 六、纪律

* skills 保持泛用：不得把某个模型的形状/层名/精度写进 skill。
* 门禁不放宽：数值判据、资源门（布线后利用率 ≤ `constraints.resource_fraction_max`）、时序。
* 未度量即 `NOT_RUN`；缺失测量不是 0。
* 一个 run 目录只写一次；要重跑就新开 run（`--reuse` 只用于明确的重生成）。
* EDA 工具一次只跑一个（`run_line.py` 用机器级锁文件保证）。

板卡身份是组合记录：`contract_sha256` 只标识 deployment.yaml；`board_profile_sha256` 标识它引用的板卡文件，
`board_pins_profile_sha256` 标识实际引脚 profile。不可只用第一项认板卡；新 run 的开局、运行计划和阶段证据都保留这些字段。

## Opt4 数值偏差定位
当原 Python 与 CSim 接近而 RTL/板端有明显偏差时，先复用现有日志和工程，比较首个计算层、池化边界及输出前张量，定位第一个不一致的边界，再修改对应实现。连续候选产生相同错误值时停止盲目扫描 RF、位宽或非线性表。RTL/CSim 均属定位手段，用户验收仍是完整原模型真实板端输出的原 Python 数值比较。
Vitis/hls4ml 1.3.0 的生成工程自动加入按结构选择的显式池化兼容实现。仅 2x2/stride2、无填充、一个像素的全部通道占一个数据包的 MaxPooling2D 使用该路径，其他形状保留原实现。修改记录进入转换证据。
采集样本数从 verification.samples 读取，未声明时沿用 256。必须与实际输入条数一致，不能重复输入凑样本数。
