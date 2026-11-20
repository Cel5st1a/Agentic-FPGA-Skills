# Agentic FPGA Skills

16 个用于机器学习模型 FPGA 部署的 agent skills，以及它们共用的确定性脚本、板卡配置和测试。覆盖环境检查、模型转换、HLS 构建、Vivado 实现、主机通信和部署记录。脚本可直接从命令行调用，无需 LLM。

当前版本为 **opt4 + Odatix（2026-11-21 发布）**，同步 2026-11-18 活动技能源，包含流式激活修复与 Dense 高复用兼容修复。主要硬件目标为 **ALINX AX7A200B / xc7a200tfbg484-2（Artix-7 PL）**。仓库也保留 Nexys Video 配置和 Zynq 背景技能；这些文件不代表已完成跨板验证。

## 技能目录

| Skill | 用途 |
| --- | --- |
| [fpga-env-audit](fpga-env-audit/SKILL.md) | 检查开发环境与工具链 |
| [compatibility-checker](compatibility-checker/SKILL.md) | 检查版本兼容性 |
| [tensorflow-to-fpga](tensorflow-to-fpga/SKILL.md) | 选择 TensorFlow/Keras 部署路径 |
| [pytorch-to-fpga](pytorch-to-fpga/SKILL.md) | 选择 PyTorch/ONNX 部署路径 |
| [hls4ml-convert](hls4ml-convert/SKILL.md) | 转换模型并生成 HLS 工程 |
| [vitis-build-agent](vitis-build-agent/SKILL.md) | 执行 HLS 仿真和综合 |
| [vivado-manager](vivado-manager/SKILL.md) | 生成板级包装、仿真、综合、实现和位流 |
| [board-host-io](board-host-io/SKILL.md) | UART 主机通路、JTAG 下载和板上结果比较 |
| [reuse-factor-resource-tuning](reuse-factor-resource-tuning/SKILL.md) | 分析逐层 reuse factor 与资源开销 |
| [resource-estimator](resource-estimator/SKILL.md) | 分析资源使用 |
| [timing-closure-debugger](timing-closure-debugger/SKILL.md) | 定位时序问题 |
| [log-analyzer](log-analyzer/SKILL.md) | 分析工具日志 |
| [bitstream-package](bitstream-package/SKILL.md) | 打包构建产物与证据 |
| [workflow-manager](workflow-manager/SKILL.md) | Odatix 候选批处理、四组数值比较、实测优选与最终审核 |
| [workflow-memory](workflow-memory/SKILL.md) | 记录和查询部署组合 |
| [zynq-deploy](zynq-deploy/SKILL.md) | Zynq 部署背景材料 |

`board-profiles/`、`scripts/line/` 是公共依赖，不计入 16 个 skills。

## 使用

```powershell
git clone https://github.com/Cel5st1a/Agentic-FPGA-Skills.git
cd Agentic-FPGA-Skills
python scripts/line/run_line.py --help
python board-host-io/scripts/verify_board.py --help
```

先读所需技能的 `SKILL.md`；完整流程与运行目录见 [ORDER.md](ORDER.md)，故障恢复见[恢复规则](docs/deployment-recovery.md)。为 agent 配置技能时，应保留整个仓库目录关系：部分技能脚本按相对路径调用公共脚本，单独复制一个技能目录可能缺少依赖。不同 agent 对 `allowed-tools` 等元数据的支持需要自行确认。

需要 Python、PyYAML，以及所选路径对应的 hls4ml、TensorFlow/Keras 或 ONNX 依赖。UART 通信使用 pyserial；真实 HLS/RTL 构建需要自行安装并配置 AMD Vivado/Vitis HLS 和相应器件支持。Bash 脚本需要 Bash。仓库不附带这些工具、许可证、训练模型或验证数据。

当前脚本与文档保留原开发环境的 Windows 绝对路径。移植时检查各入口的 `--help`，显式设置可用的解释器、模型、输出目录和工具路径；板卡档案中的 `manual_path` 也需要指向自己的原始手册。不能直接沿用历史机器的路径或硬件身份。尚未验证全新机器上的一键安装。

## opt4 + Odatix 工作流

新增 [workflow-manager](workflow-manager/SKILL.md)、[候选批处理说明](docs/odatix-candidate-batches.md) 及确定性执行入口：

```powershell
python scripts/line/run_workflow.py --help
python scripts/line/odatix_batch.py --help
python scripts/line/run_workflow.py --deployment <deployment.yaml> --cases <candidates.json> --runtime <runtime.json> --output <new-output>
```

该工作流调用 Odatix 4.0 的 Workflow 和原生非支配排序，记录候选状态、命令、日志与产物，并比较原模型 Python、独立标量定点参考、厂商 C 仿真和真实 FPGA 输出。`--finalize` 从原始来源重算完成审核。具体候选要求及审核规则见技能正文；原有单次构建入口仍保留。

另需 Odatix 4.0.0、numpy、PyYAML、可用的模型转换环境，以及支持 C++17 的编译器。Odatix 通过 runtime JSON 中的 launcher 路径调用；按自己的环境填写 Python、编译器和已确认的板卡连接信息。Odatix 需单独安装，本仓库发布的是集成脚本，不包含其完整上游源码。第三方组件遵循各自许可。

每次使用全新输出目录。工作流实际运行会调用 EDA，并可下载与访问板卡；请先核对输入契约、候选配置和 runtime。本次发布只进行了软件检查，没有重跑 EDA 或板上实验。历史机器路径不代表在其他电脑上可用；旧运行证据也不能通过修改路径直接变成新运行的证据。

## 验收与证据

此前 AX7A200B 256 条部署实验的用户验收标准是：**给定原始模型与权重确实在板上运行，全部给定的 256 条板上输出通过原始模型的 Python 数值比较。** 精度、reuse factor、HLS 位精确性、协议字段、周期计数、资源和时序报告、完整 RTL 仿真、联合仿真及报告完整性，不自动构成额外用户验收条件；仅在人明确要求时纳入验收。

快照保留了历史阶段检查与打包约束，包括 `ORDER.md` 中的冻结判据。它们反映现有工具实现，不覆盖上面的用户验收标准。应区分“板上数值成功”与“历史打包检查通过”；本次开源整理没有重写这些脚本的行为。

工具退出码、仿真、fixture 或位流本身都不能证明真实板上成功。缺失测量保持未知，保留原始日志与每次尝试。使用任何引脚、复位、时钟或 I/O 电压前，核对确切板卡版本及原始手册。历史配置不能代替对当前硬件的核对。

## 测试与发布范围

```powershell
python -m pip install pytest PyYAML
python -m pytest tests/test_bringup_line.py tests/test_rom_package_board.py board-host-io/tests/test_host_geometry.py board-host-io/tests/test_protocol_geometry.py reuse-factor-resource-tuning/scripts/test_tune_reuse_factor.py -q
```

完整测试树还包含依赖历史运行目录或 xsim 的测试，需要相应环境。测试结果与检查范围见 [VALIDATION.md](VALIDATION.md)。软件测试不代表硬件验证。

本快照保留 16 个 skills、公共工具、板卡档案与测试；排除历史备份和审查归档、旧模型、构建文件、日志、缓存，以及 `workflow-memory/examples/` 中的本机历史记录。原有引用中的本机证据路径在公开仓库中不可用。`SOURCE-MANIFEST.json` 记录保留文件的 SHA-256，便于核对与源目录的一致性。

## 许可证

原创代码与文档采用 [MIT License](LICENSE)。第三方代码保留自己的许可与版权声明，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库不包含 AMD 工具或 ALINX 板卡手册。

## S15-opt4

新增 Vitis/hls4ml 1.3.0 流式最大池化兼容路径。Keras 转换自动在生成工程中安装按结构选择的 2×2/stride2、无填充、完整通道包显式行缓存实现，其他形状调用原实现。兼容修改记录输入模板和输出哈希。

板端采集从 verification.samples 读取正整数样本数，默认仍为 256，必须与输入条数一致。原 Python 误差判据仍为 .001+.001*abs(ref)。打包入口识别 host-board 并转交已有 host packager。打包拒绝属单独诊断，数值成功后无需为其重新部署。

C++ 与 RTL 大幅分歧时，先比较首层、池化边界和输出前张量，定位首个分歧再调整实现。

QCNN 和 Autoencoder 的组内显式池化修复曾在 AX7A200B/Vitis 2026.1 上分别通过原 100 条输入实板比较。opt4 的泛化实现另经离线回归；本次版本发布未重新运行正式实板矩阵。详细验证范围见 VALIDATION.md。
