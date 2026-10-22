---
name: workflow-memory
description: >
  Records successful FPGA+AI workflows for reuse. Stores version, board, model,
  config, timing, resource results. When user starts a new project, checks past
  successes first to avoid repeating failed combinations. Use after any
  successful build or when starting a new FPGA+AI project.
allowed-tools: Bash, Read, Write, Grep, Glob
---

# Workflow Memory

## Purpose
Build a knowledge base of successful (and failed) FPGA+AI configurations to
accelerate future projects. When the user starts a new conversion or build,
first check the memory for compatible past successes before trying new
combinations.

## When to Use
- After a successful hls4ml conversion + HLS build
- When starting a new model-to-FPGA project
- Recording a known-working version combination
- User asks "what config worked last time?"

## When NOT to Use
- Do not publish an unverified debugging candidate as a successful reusable workflow. During debugging, retain candidate failures and execution statistics in the run; promote to a success entry only after the intended gates pass.
- For environment issues (use `fpga-env-audit`)
- For generic version queries (use `compatibility-checker`)

## Memory Format

Each workflow entry is a JSON file in `workflow-memory/examples/`:

```json
{
  "timestamp": "2026-05-13T00:30:00+08:00",
  "project": "smoke_test_mlp",
  "status": "success",
  "config": {
    "vivado": "2025.2",
    "vitis": "2025.2",
    "python": "3.10.11",
    "tensorflow": "2.13.1",
    "hls4ml": "1.3.0",
    "qkeras": "0.9.0",
    "keras_type": "v2",
    "protobuf": "3.20.3",
    "numpy": "1.24.3"
  },
  "model": {
    "type": "MLP",
    "framework": "Keras",
    "layers": 2,
    "input_shape": [4],
    "output_shape": [2],
    "precision": "ap_fixed<16,6>",
    "reuse_factor": 1,
    "strategy": "Latency"
  },
  "results": {
    "csim": "PASS",
    "csynth": "PASS",
    "cosim": "NOT_RUN",
    "impl": "NOT_RUN",
    "hls_code_written": true
  },
  "validation": {
    "golden_reference": {
      "mse": 0.0001,
      "mae": 0.005,
      "peak_error": 0.02,
      "integral_error": 0.003
    },
    "hls_functional": {
      "status": "PASS",
      "max_error": 0.001,
      "rms_error": 0.0005
    },
    "rtl_cosim": {
      "status": "NOT_RUN",
      "latency_cycles": null,
      "ii": null,
      "throughput_msps": null
    },
    "resource": {
      "status": "PASS",
      "dsp_pct": 45,
      "lut_pct": 33,
      "ff_pct": 16,
      "bram_pct": 41
    },
    "board_inference": {
      "status": "NOT_RUN",
      "peak_error": null,
      "integral_error": null
    },
    "physics": {
      "status": "NOT_RUN",
      "timing_error_ns": null,
      "pileup_fidelity": null
    }
  },
  "system_metrics": {
    "compression_ratio": 8.0,
    "bandwidth_reduction_pct": 87.5,
    "end_to_end_delay_ns": 220
  },
  "backend": "Vivado",
  "board": "AX7A200B",
  "part": "xc7a200tfbg484-2",
  "notes": "Illustrative record: CSim/CSynth passed; physical and board validation NOT_RUN."
}
```

## Usage

### Record success
```bash
python scripts/record_workflow.py \
    --config hls4ml_config.yaml \
    --status success \
    --notes "MLP converted and HLS code generated"
```

### Query past successes
```bash
python scripts/query_workflows.py \
    --framework keras \
    --status success \
    --vivado 2025.2
```

### Find compatible config
```bash
python scripts/find_best_config.py \
    --model-type mlp \
    --framework keras
```

## Output

```
=== Workflow Memory Query ===
Query: framework=keras, status=success

Found 2 matching workflows:

1. smoke_test_mlp (2026-05-13)
   Vivado 2025.2 | TF 2.13.1 | hls4ml 1.3.0 | Keras v2
   Result: csim PASS, csynth PASS, HLS code generated
   Score: 100% match

2. hls4ml_keras_test (2026-05-13)
   Vivado 2025.2 | TF 2.13.1 | hls4ml 1.3.0 | Keras v2
   Result: csim PASS, csynth PASS, HLS code generated
   Score: 95% match

Recommended config from workflow #1:
  precision: ap_fixed<16,6>
  reuse_factor: 1
  strategy: Latency
  backend: Vivado
```

## Principles
- **Evidence scope**: Separate conversion, simulation, physical gates and live board validation. A partial success or filename containing success is not proof of on-board success. Preserve NOT_RUN and FAILED; never fill them from another run.
- **Full context**: Always include version pins, not just names
- **Incremental**: Update entries when new stages pass (csim → csynth → cosim)
- **Failure too**: Record failed combos with `status: failed` and notes on why
- **No sensitive data**: Never record API keys, license keys, or credentials

## 可复用结果与执行统计

- 用 `request_id`/`followup_request_id`、model/run/attempt、同 run evidence 路径与哈希关联结果。记下 board_id、完整 part（含封装/速度等级）、时钟、profile、实际 JTAG/UART 桥身份及契约/工具版本。跨板历史 PASS 可提供候选，不继承时序或硬件验证结论。
- 分别记录假设、源码改动、结构/调度核验及物理/数值门禁。已被证伪的候选也保存；DATAFLOW 等方法记录作用范围与 latency/II/资源代价，不从一个成功模型推广为默认补丁。
- 请求级墙钟使用本次真实起止，并说明截止于仿真、采集、报告还是监督器退出、时区及是否含暂停/轮询。阶段工具时长优先引用 evidence 的 `tool.duration_s`；阶段起止差、复合 launcher 时间、工具时间分别标注来源，重试按 attempt 列出，不能直接相加冒充总耗时。报告与原证据冲突时保留并说明差异，不改写原始记录。缺失时间标记未知，不填零或硬编码结束时间。
- 同一 SDK 会话续跑时，使用请求开始前保存的 `metrics_baseline` 和结束计数做差，分别给 input/output/cache-read（提供时还有 cache-write/reasoning）、模型、conversation_id、来源与范围。缓存计数按 SDK 语义说明，不能重复加到输入总数；缺基线、模型/会话变化或计数回退时不可当作本轮增量。不要把监督器轮询/补证开销冒充纯 EDA 开销。
- SDK `cost=0` 同时出现模型未映射/计价失败时，费用写未知；只有有效计价来源才能报实际费用。统计快照仅提取必要计数，不复制完整保存会话、密钥或大量逐回合记录。
- 性能必须带计时起止、时钟与来源。HLS latency、II 和 `compute_cycles` 是不同口径；适配器往返周期不含 UART 线时，也不等于核 latency/II。检查完整数组，报告样本数、首项/稳态或范围；仿真与实板的喂数/握手节奏不同，应分别报告，不能只取首项或相互套用。口径不一致时不计算加速比。

上述统计可随 run 报告/补充记录保存，不表示现有 `record_workflow.py` 已自动采集 SDK 指标或硬件证据。录入前按 [部署恢复规则](../docs/deployment-recovery.md) 核对实际验证层级。
