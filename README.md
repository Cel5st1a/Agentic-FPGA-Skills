# Agentic FPGA Skills

15 个用于机器学习模型 FPGA 部署的 agent skills，以及它们共用的确定性脚本、板卡配置和测试。覆盖环境检查、模型转换、HLS 构建、Vivado 实现、主机通信和部署记录。脚本可直接从命令行调用，无需 LLM。

本仓库是 2026-10-22 的技能源快照。主要硬件目标为 **ALINX AX7A200B / xc7a200tfbg484-2（Artix-7 PL）**。仓库也保留 Nexys Video 配置和 Zynq 背景技能；这些文件不代表已完成跨板验证。

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
| [workflow-memory](workflow-memory/SKILL.md) | 记录和查询部署组合 |
| [zynq-deploy](zynq-deploy/SKILL.md) | Zynq 部署背景材料 |

`board-profiles/`、`scripts/line/` 是公共依赖，不计入 15 个 skills。

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

## 验收与证据

当前 AX7A200B 实验的用户验收标准是：**给定原始模型与权重确实在板上运行，全部给定的 256 条板上输出通过原始模型的 Python 数值比较。** 精度、reuse factor、HLS 位精确性、协议字段、周期计数、资源和时序报告、完整 RTL 仿真、联合仿真及报告完整性，不自动构成额外用户验收条件；仅在人明确要求时纳入验收。

快照保留了历史阶段检查与打包约束，包括 `ORDER.md` 中的冻结判据。它们反映现有工具实现，不覆盖上面的用户验收标准。应区分“板上数值成功”与“历史打包检查通过”；本次开源整理没有重写这些脚本的行为。

工具退出码、仿真、fixture 或位流本身都不能证明真实板上成功。缺失测量保持未知，保留原始日志与每次尝试。使用任何引脚、复位、时钟或 I/O 电压前，核对确切板卡版本及原始手册。历史配置不能代替对当前硬件的核对。

## 测试与发布范围

```powershell
python -m pip install pytest PyYAML
python -m pytest tests/test_bringup_line.py tests/test_rom_package_board.py board-host-io/tests/test_host_geometry.py board-host-io/tests/test_protocol_geometry.py reuse-factor-resource-tuning/scripts/test_tune_reuse_factor.py -q
```

完整测试树还包含依赖历史运行目录或 xsim 的测试，需要相应环境。测试结果与检查范围见 [VALIDATION.md](VALIDATION.md)。软件测试不代表硬件验证。

本快照保留 15 个 skills、公共工具、板卡档案与测试；排除历史备份和审查归档、旧模型、构建文件、日志、缓存，以及 `workflow-memory/examples/` 中的本机历史记录。原有引用中的本机证据路径在公开仓库中不可用。`SOURCE-MANIFEST.json` 记录保留文件的 SHA-256，便于核对与源目录的一致性。

## 许可证

原创代码与文档采用 [MIT License](LICENSE)。第三方代码保留自己的许可与版权声明，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库不包含 AMD 工具或 ALINX 板卡手册。
