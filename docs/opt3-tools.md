# opt3：按证据缩小实现探索范围

保留原模型、权重、输入和完整非线性；工具数值/物理诊断不增加用户验收条件。
先读取已有证据，再选择确实能回答当前问题的工具，不要求依次执行本页各项。
开发软件或RTL测试不能替代真实板端256条完整输出，也不能证明部署总耗时普遍下降。

## 宽流式接口

`host_stream_adapter.v`把输入样本装入移位寄存器，每个接受拍右移固定lane跨度；
输出用固定跨度移位拼接，保留生成接口的lane、有效位宽、物理槽位和窗口归属。
输入每lane按实际符号位扩展，输出丢弃物理槽位填充。请求全部输入/输出拍完成才结束。
只在握手接受时推进；输出提前到达、输入背压、输出间隔及连续请求仍需保持原语义。

额外输入寄存器约为样本有效位数；综合可能优化，不能把这个上界当最终FF数量。
固定移位只消除了全向量变量索引这一结构，不保证每种尺寸都更省资源或更快。
先看已有层级和物理报告；模型算术、流接口及UART成本分别讨论。

## 表与并行度诊断

现有`log-analyzer/scripts/analyze_efficiency.py`新增可选参数：

```text
--parameters <generated/parameters.h> --defines <generated/defines.h>
--activation-header <generated/nnet_utils/nnet_activation.h>
--wrapper <generated/wrapper.json> --out <fresh-report.json>
```

可继续传已有`--utilization`、`--log`。源路径和SHA256随输出保存。
每个已识别配置输出表深、实际声明类型位数及单副本位数乘积；别名链可解析，
不支持的C++表达式/模板保持null，不能用契约请求位宽填补。
读取并行度只在配置明确选择stable、唯一匹配的stable Softmax循环及明确n_slice常量下报告源展开上界；
它不是物理存储端口、复制数、BRAM数量或真实层延迟。
接口摘要读取生成声明的输入/输出拍数和端口位数；不从参考答案或模型名猜几何。
有效RF/kernel仍用现有RF咨询工具读生成结构；RF不等于物理共享程度。

没有表深/RF统一默认，没有80%拒绝门槛。CPU误差满足后不为更小误差盲目增表；
CPU失败先分辨首个可映射偏差，已有DRC/层级用于检查映射瓶颈。

## QKeras主机误差定位

`hls4ml-convert/scripts/local_precision_search.py`沿用原入口，支持文件Keras和
Keras/QKeras JSON加原权重；限`io_parallel`、单输入/输出。
`--trace-layers`从原图读取QActivation边界，与同一生成固件/bridge编译的C++输出对比，
保留training=False、原kernel/bias量化器和完整Softmax/Sigmoid。
po2兼容修复与可选Softmax读取调度沿用转换器的版本/模板绑定记录。

```text
--deployment <original-contract.yaml> --output <fresh-dir>
--layer <actual-generated-layer> --role accum --candidates "ap_fixed<24,8>"
--trace-layers --compiler <host-g++>
```

先编译当前配置，已通过就停止；否则仅按调用者顺序试局部角色。
量化图拒绝weight、bias、result覆盖；accum/table/exp_table/inv_table/inv_inp
也必须实际存在于生成节点。没有自动最优求解或完整图内部轨迹。
首个观察偏差只在有原图对应的输出边界中定义，融合/重排后的未知边界保持null。
原模型、关联权重、输入和契约均保存前后哈希；全部失败和完整比较保留。
自定义或有状态图沿用逐边界观察，不能声称一次多输出前向等价。

## 同组最小阶段续作

端口审计先将所有路径解析成绝对路径，再设置子进程工作目录；新证据路径不可覆盖。
板级构建后段失败时，`build_host.py`提供显式续作：

```text
--deployment <same-run/contract/model/deployment.yaml> --board <previous-host-board>
--start-stage impl --prior-evidence <successful-synth-evidence.json>
--continuation-board <same-run/fresh-host-board-02> --python <runtime-python>
```

`impl`需要成功synth证据和synth.dcp；`bitstream`需要成功impl证据和routed.dcp。
检查契约、前阶段名、板目录、checkpoint哈希、全部暂存RTL/约束，
并使用既有identity工具重新读实际模型/关联权重、算术源码/配置和包装依赖。
核心与包装身份都必须明确匹配，声明的工具版本也在身份中；仍需使用相同实际工具链，
版本字符串不构成工具运行证明。旧记录缺身份不自动有效，也不补造产生阶段事实。

新attempt克隆在同一run的新目录；原目录、日志和失败工件保留。
不复制失败后段checkpoint作为有效结果；新日志与证据按目标目录名独立保存。
只执行所选阶段及其后续阶段，实际阶段失败即停在该次调用，后续次数不设限。
来源改变或身份未知时保留新副本供诊断，定位最小失效范围后另开attempt；
不能把换目录当成跨组继承实现的许可，不能用这条路径补跑已通过板端数值的模型。

发布包不带开发evidence、validation或历史工程。旧解析回归需要的外部开发材料可通过
`S15_DEVELOPMENT_EVIDENCE`指定，仅本地测试读取，不参与转换或部署。
