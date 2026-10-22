# normalize 乘加路径：候选生效核验

适用于 routed 最差路径已定位到 normalize/BN 乘加、DSP 级联或控制网的情况。先读真实路径，不因模型含 BN 就采用本参考。

## 保持计算语义

生成的 normalize 常为 `res[i] = product(data[i], scale[i]) + bias[i]`。scale/bias 可能已由权重离线生成；不能将每次推理描述为重算均值/方差。确认 `product` 的实际返回类型及赋给 `res_T` 的截断点；保存完整宽度中间结果，避免提前量化。调整流水时核验加法顺序、数据与参数索引、valid/done 与重复请求行为。

源码中的中间变量只是写法，不是硬件寄存器证明。按以下证据选择下一步：

| 候选结果 | 判定与动作 |
| --- | --- |
| DSP 全寄存调度选项被接受，关键 RTL 与调度相同 | 无该选项改变路径的证据，检查作用域/算子绑定，不以选项回显宣称修复 |
| 两个 PIPELINE 循环生成少量不同 RTL，但差异仅行号/符号，乘法模块与调度相同 | 循环可能被合并、缓存消去；该拆分未建立所需边界，保留证据并调整假设 |
| DATAFLOW 日志抽取两个进程，RTL 中存在连接它们的通道，调度/映射发生实质变化 | 候选结构生效；核验通道宽度、生产/消费顺序、仿真覆盖及 latency/II 代价，然后做物理验证 |

如采用 `decltype(product(...))` 保留中间类型，仍应核对实际生成宽度与数值证据。一个乘法模块消失或更名本身也不是截断点不变的证明。模板修改影响所有实例时全部列出并验证，不能声称只修了最差路径所在实例。

## 已验证案例的适用边界

2026-10-08 Nexys Video 的 `KERAS_3layer_batch_norm`，Vitis/Vivado 2025.2、hls4ml 1.3.0，precision `ap_fixed<32,10>`，RF 64/64/64/32、100MHz：

- b5：DSP 调度选项接受，RTL/调度不变；未以此进行等价硬件重试。
- b6：拆成两个顺序循环，仅生成符号变化；关键乘法模块与 config4 调度不变，未进行等价 Vivado 构建。
- b7：所有四个 normalize 实例拆成 ProductStage/Result DATAFLOW 进程并生成通道。顶层 HLS 估计仍为 10.667ns，但实际 routed WNS 由 a1 的 −1.026ns 变为 +0.294ns，WHS +0.027ns；资源/DRC 与100样本、500输出双参考实板门均通过。
- 代价：顶层 worst latency 382→557、II 64→192。实板 `compute_cycles` 全部915；host-sim 为首项915、其余99项1787。它们是适配器往返周期，不能与 HLS latency/II 混用。不要只取数组首项代表整个仿真。

该案例支持在匹配的路径上评估真实进程边界，不证明 DATAFLOW 在其他模型、模板或板卡上必然有效。顶层 HLS 估计未变，也不能推出 HLS 最差路径“已经迁走”；迁移位置须另读路径/函数报告。

只读来源根：`D:/ML_models/_work/nv6-20261007`。核对 `diagnostics/bn-b6-result.json`、`diagnostics/bn-b7-structure.json`、`runs/bn/b7/evidence/patch-batchnorm-dataflow.json`、新 routed 报告及 `runs/bn/b7/hardware-validation/{summary,python-evidence,hls-evidence}.json`。原始代理报告中的推断不替代实际报告。
