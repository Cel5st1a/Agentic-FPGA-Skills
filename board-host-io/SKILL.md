---
name: board-host-io
description: Use when a profile-bound hls4ml board build needs UART host input/output, routed-port auditing, volatile JTAG programming, or per-value on-board numerical verification.
---

# Board host I/O and numerical verification

opt3宽流式适配器在握手接受拍后固定移动数据，避免全样本的运行时变址选择/写入；
代价是额外输入移位寄存器。先看已有层级报告，不能从源码推定实际面积或时序提升。
`build_host.py --start-stage impl|bitstream --prior-evidence <previous-stage.json>
--continuation-board <fresh-same-run-dir>` 在新目录续作最小剩余阶段，原`--board`保留。
必须有同组前阶段成功证据、checkpoint/RTL/约束/契约哈希和完整复用身份；
旧记录身份未知时先检查现存证据，不默认跳过阶段，不为可选诊断重跑已验收模型。
使用范围和参数见 [opt3工具说明](../docs/opt3-tools.md)。

S15-opt 当前规则见 [ORDER.md](../ORDER.md)：256 条完整板端输出仅按原 Python
`abs(error)<=.001+.001*abs(ref)` 判断；HLS 对照是可选诊断，真实采集与模型身份仍须有证据。
本版 transport 响应长度支持完整 16 位字段，512 维输出已做 RTL/主机解码回归；
单包保留16位长度；完整样本超过单包容量时自动选chunked_v1，按32位样本extent分块，
短尾块与跨块元素保留。显式single超限仍拒绝，不截断。此修改尚未进行新模型上板验证，不能继承旧 bit 的证明。
08:00 修复增加整拍多拍输出聚合：实际端口位宽与整份样本位宽分别记录，
按输出窗口序号归属请求，全部输出拍及输入拍完成后才结束。开发 RTL 覆盖
2/3/32 输出拍、每拍 1/2/4 元素、背压、间断及提前输出；不是原模型上板证据。
输入或输出元素不能整除生成核心声明的 lane 数时仍明确拒绝；不猜核的尾拍/补零语义。
输入/输出各自的有效W/I来自同一生成header/defines/bridge与hash；物理槽位宽度可分别填充。
host适配只在槽位低位放实际元素，高位符号扩展；输出忽略填充位。有效类型文件变化需更新来源。

新生成 host wrapper 记录 `core_source_manifest`：同目录下实际暂存的
`.v/.sv/.dat/.mem/.hex` 全部文件名及哈希。构建/仿真暂存前校验清单，
缺失、增加或内容变化需保留旧尝试并更新依赖记录，不能只认主顶层文件哈希。
旧记录没有清单时明确标作 `UNRECORDED_LEGACY`，不补造历史证明。
`stage_rtl.dependency_delta` 只比较核心与包装 RTL 内容范围：包装变化不标记核心仿真失效，
核心变化则标记相关下游。它不判断模型、配置、工具版本是否匹配，不自动跳过构建或宣称可安全复用。

可选 `wrapper.reuse_inputs` 包含产生阶段的 `arithmetic_config`（part/clock_period_ns/io_type/
hls_config）、`tool_versions`（python/frontend_runtime/hls4ml/hls/vivado）及明确列出的
`arithmetic_files` 路径映射时，`stage_rtl` 会重新读取实际依赖并附带 `reuse_identity`。
模型/关联权重、算术配置/源码或HLS工具变化，即使RTL字节未变，嵌套的
`dependency_delta.reuse_identity_comparison` 仍会标记核心身份不匹配；只改Vivado或包装留在包装域。
外层 `affected_stages` 仍只说明RTL差异，必须同时读取嵌套身份判断。未提供元数据的旧记录为未知，
不因这项优化拒绝原有构建。身份相同不等于已验证核心有效，不自动迁移/跳过工件。

采集事件增加 `observed_at_utc` 和本次采集起点以来的单调时钟耗时。`summary.timing`
分别记录完整 Python 数值比较通过的观测上界、采集处理结束和两者之间的主机耗时；
不是板端计算结束的精确时刻，也不是内核延迟。失败时通过时刻为 null。
SDK/报告/owner清理结束来自外部流程，采集器不知道时保持 null。
`verify_board.earliest_success_observation` 仅选择已有合格记录的最早观测上界，
不验证硬件来源；裸成功文本、无时区或损坏时间不参与选择，精确首pass时刻不推造。

This skill turns an HLS core into a separate UART board variant. The UART RTL comes from
the earlier board-verified run; the transport length extension is a new software/RTL repair.
The old bitstream proves only its original model, never this version or a new model.

Read [references/provenance.md](references/provenance.md) when judging whether prior board evidence applies to a model or bitstream.

## Supported boundary

**One authoritative source per geometry fact: the producer records it; downstream stages read and validate it. Never re-derive hardware geometry from contract shapes or reference rows.** Input/output grids come from hash-bound generated type sources, independently of requested global precision. Legacy records without those sources retain declaration cross-checks; a requested precision cannot fill a missing grid. Agents read and check records, not invent packing rules.

| Geometry | Authoritative field | Cross-check |
| --- | --- | --- |
| Input element count/order | ROM wrapper `input_layout.elements/flatten` | `c_order`, interface count, actual core port width and sample layout |
| Output element count/order | New wrapper `output_layout.elements/flatten` | AXIS `interface.stream.output_elements`; validated ap_vld scalar-port mapping |
| Input element grid | `rom_image.grid` | hash-bound firmware type; AXIS `element_types.input` |
| Output element grid | `output_grid` | hash-bound firmware type; AXIS `element_types.output`; ap_vld result port widths |
| Lane, beat and TDATA geometry | `interface.stream` | input/output beats = respective elements / respective lanes (exact division); each physical slot is at least its actual element width; input/output slot widths are independent; byte-aligned actual TDATA port |
| UART payload/frame/bus sizes, scales, baudrate | materialised host `host_geometry` | internal relationships and `host_protocol` projection; never replace fields with recomputed values |

Only two legacy output-count rules are allowed: read AXIS `stream.output_elements`; or validate a nonempty, unique, ordered ap_vld `result_outputs` list with every `output_bits` equal to one output element, then apply one scalar per port. This rule does not support packed multi-element output ports. Missing `input_layout` or `output_grid` must fail with the field, expected rule and actual value: regenerate records in a **new run**, never patch an old record from shape, precision or references. Unknown interface styles require a new explicit, tested interface rule.

Historical host records that omitted `rom_image` may read it only from their same-contract `source_wrapper` after checking `source_wrapper_sha256`. A generated field is not automatically trustworthy: validate identity, provenance, duplicates and actual core ports. Conflicting copies are errors, not fallback choices.

Producer sample-layout validation and signed fixed-point quantisation share `scripts/line/geometry.py` in the skill-tree root. `run_line.py` passes sample/reference sources, not inferred element counts. References only check rectangular sample layouts and flattened result counts; their presence must not change geometry.

For AXIS production, `scripts/line/generated_geometry.py` reads the generated header, `defines.h` and `<top>_bridge.cpp`: typedefs supply signed element grids and lanes; whole-tensor `convert_data` extents supply sample element counts. Their hashes are recorded in `geometry_files`. Missing files, unsigned types or unsupported syntax require a named refusal and a new tested rule, never a precision/reference/TDATA fallback.

`host_geometry.geometry_for_wrapper()` is the common boundary for `make_host_board.py`, `simulate_host.py` and `verify_board.py`; `protocol.py` consumes the resulting Geometry. For a ROM wrapper the builder materialises UART geometry; for a host wrapper consumers load `host_geometry` and validate it. Contract-only `geometry_from_contract()` calls are refused: they require a complete wrapper. Missing materialised geometry is an error, never permission to rebuild it during consumption.

At host generation only:

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

Element i occupies `[i*W +: W]`, first element in the low word. No per-element byte padding: 64 × 20 bits is 160 bytes. Only the transport bus is rounded to whole bytes.

**At generation**, baudrate precedence is contract `host_transport.baudrate`, board profile `peripherals.uart.baudrate`, then verified 115200; save the value and source. **When consuming an existing host wrapper**, use its saved baudrate even if the external board profile changes. A changed contract identity is rejected and requires a new run. Board facts come from `board_facts.py` and the profile recorded in `wrapper.board.pins_profile`; do not copy another board's pins or I/O standards.

Which adapter sits between the transport and the core is decided by the core's **actual ports** (`interface.style`), never by the contract's `io_type` string:

| `interface.style` | adapter | why |
| --- | --- | --- |
| `ap_vld_array_with_ap_ctrl_hs` | `host_avld_adapter.v` | latches the packed word at `start` and holds `ap_start` as a level until `ap_done` |
| `axis_stream_with_ap_ctrl_hs` | `host_stream_adapter.v` | keeps a dataflow core running, injects one framed sample, correlates ordered beats |

`io_serial` produces no third line: hls4ml gives it either array or stream ports, and it lands in whichever row above matches.

For ordered Vitis dataflow stream cores, the adapter keeps the core running with zero-valued idle windows. A request occupies a complete `INPUT_BEATS`-beat window; its `OUTPUT_BEATS` output beats are identified by that window's ordinal and aggregated in order. The transport holds the payload stable until done. The adapter switches into injection only after accepting the last idle beat, and advances its narrow slice only on handshake. It waits for both the complete input window and the complete output window, including when output arrives early. Single-beat callers retain the `INPUT_BITS` parameter and default `OUTPUT_PORT_BITS=OUTPUT_BITS`, `OUTPUT_BEATS=1`. New cores require an ordered, fixed number of output beats per input window for this policy; the development tests do not establish that property for an untested real core. See [recovery rules](../docs/deployment-recovery.md).

### Response cycle counter

`compute_cycles` is **adapter round-trip elapsed core clocks**, from the adapter sampling
the transport's `start` to the transport sampling the adapter's `done`. It excludes UART
request reception and response transmission. For AXIS it includes input backpressure and
the wait for the matching output ordinal in a continuously fed pipeline; for ap_vld it
includes adapter launch/idle/completion overhead around the core's `ap_done`. Neither
interface makes this counter an isolated HLS latency or initiation interval.

Capture summaries and host-simulation evidence record `cycle_counter` with these events,
the actual clock, interface and completion rule. The byte-bypassed simulation forces a
100 MHz clock and uses deterministic pacing; board captures use the wrapper's core clock.
Input/output handshake traces are needed to measure kernel stream latency and output
interval separately. Compare implementations only after aligning counter definitions,
feed policy and clock; do not subtract an HLS interval from this elapsed-time counter.

For an `ap_vld` core the transport's `LAUNCH` state pulses `start` for a single cycle, which is not how an `ap_ctrl_hs` core is driven. Two things are measured, not assumed: this core family treats `ap_start` as a **level** that must stay high until `ap_done` (a short pulse left `ap_done` low for 5M cycles), and `ae_input_ap_vld` must not be tied high while the transport is still writing the packed word byte by byte. `host_avld_adapter.v` latches the word on `start` and holds `ap_start` until `ap_done`; wiring the core straight to the transport was defect D-M01. Its testbench `tests/host_avld_adapter_tb.v` instantiates that broken wiring as a negative control, so a passing run means the adapter did something the broken wiring did not.

### Core properties each adapter requires

These are checked or exhibited by the testbenches, not assumed. A core that violates one of them is not rejected by the generator (it cannot see them), so a new core must be shown to satisfy them before the same policy is reused.

| Requirement | Why | Where it is shown |
| --- | --- | --- |
| `ap_idle` exists on an `ap_vld` core | the adapter will not start a transaction until the core is idle | `validate_interface` refuses the core outright |
| `ap_done` is deasserted whenever `ap_idle` is high | otherwise a sticky `ap_done` would be mistaken for the end of a request that was never started, returning a **stale result that passes the CRC and the numerical gate** | `host_avld_adapter_tb.v` section E runs a core with a deliberately sticky `ap_done` and asserts the request is refused |
| one or more input/output beats per sample with exact lane division | `host_stream_adapter.v` injects one complete input window and aggregates its ordered output window | `host_geometry` validates recorded counts/port widths; `host_stream_output_beats_tb.v` tests multiple output beats, stalls and early outputs; partial lanes remain unsupported |
| generated input/output element grids match their hash-bound type sources | wire scaling must match actual emitted types; original-model tolerance has no LSB term | `host_geometry` validates header/defines/bridge hashes, grids and actual ports |

Historical measured cores and the new development tests provide different evidence. Multi-beat development coverage does not establish a new real core's behavior or board success. These properties describe the feed policy, not a new numerical acceptance criterion.

Historical transport observations include discarded frames and stuck cores. A timeout
alone cannot distinguish them. Inspect existing raw traffic and actual state before choosing
a minimal recovery; preserve every attempt. If the current configuration is genuinely invalid
or stuck, an authorized reconfiguration can be the needed recovery. These observations do
not require a universal reset/probe schedule, or a new download after valid numerical success.
The exact board reset/clock wiring must still come from its verified profile, never inference.

## Board execution and independent attempts

Formal experiments are currently paused. These instructions do not authorize resuming them.
When hardware execution is authorized, retain evidence tying the observed board, configured
bitstream and complete request/output stream to the original model. Never use a previous
model's unknown configuration as proof of the current model. Missing or genuinely invalid
execution evidence requires inspecting existing data first and repeating only the smallest
missing operation; keep every failed attempt. A liveness probe is optional diagnosis, not an
extra CRC acceptance gate. Do not automatically reprogram an already numerically accepted
model or repeat it for optional metrics. Historical observations of stale configurations are
hazards to diagnose, not a universal configure/probe/recapture schedule.

## Work in an isolated run

Use a fresh output/attempt directory and current independent skill copy. Preserve the
original model, inputs, complete CPU reference and actual core provenance. Build only the
artifacts needed for the current change; a shell-only fix keeps a valid arithmetic core.
`make_host_board.py` consumes the producer wrapper. `simulate_host.py` is optional development
diagnosis using actual HLS RTL, byte-interface UART bypass and the original-model numerical
rule. `build_host.py` records absent/failed/mismatched simulation as diagnosis and invokes
synth/impl/bitstream serially under the EDA lock. Actual vendor failures stop downstream
execution; resource fractions and timing summaries do not add numerical acceptance gates.
No fixture, C++/RTL simulation, bitstream, port audit or JTAG exit code is board acceptance.

## Testbenches run without EDA

The adapters and the transport have xsim testbenches that need no synthesis and no EDA lock, so they run as ordinary tests:

```powershell
& $py -m pytest "$skill\..\tests" -q
```

`scripts/run_rtl_tb.py` compiles and runs a testbench and returns an effective status. **Do not trust the simulator's exit code**: measured, `xsim` exits 0 even after `$fatal`, so a run counts as passed only when no `Fatal:`/`ERROR:` line appeared *and* the testbench printed its own `..._PASS` marker.

`tests/test_transport_parameterisation.py` is the guard on `transport.v`. The board-verified original is frozen at `references/frozen/transport-96ceb796.v` (sha256 `96ceb7966f18fa06…`) and is never staged into a build. Parameterising the transport was allowed to add parameters and replace geometry literals; it was not allowed to change the state machine. Two gates enforce that: an explicit structural diff whose permitted line edits are declared in full, and a cycle-exact xsim run of the frozen module beside the parameterised one at the verified geometry, comparing every port on every clock edge. Change `transport.v` and both must be re-satisfied before `SOURCE_HASHES` in `make_host_board.py` is updated.

## Routed port and programming gates

Read the **routed checkpoint**, not `wrapper.json` alone. The audit stores Vivado stdout, exact port/pin/IOSTANDARD values, checkpoint SHA-256 and `io-scope.json`. A top without both `uart_rx` and `uart_tx` is `configure-only`, `on_board_numerical=NOT_RUN`, `hardware_verified=false`; it cannot enter the host programming step. The UART pins and I/O standard must match the recorded board profile, read through `board_facts.py`. If a package is delivered with no host path, include its routed port audit and configure-only scope declaration.

```powershell
& $py "$skill\audit_ports.py" --routed-dcp "$run\p3-build\host-board\routed.dcp" --wrapper "$run\p3-build\host-board\wrapper.json" --output "$run\evidence\host-port-audit"
& $py "$skill\program_jtag.py" --bitstream "$run\p3-build\host-board\board_top.bit" --io-scope "$run\evidence\host-port-audit\io-scope.json" --bitstream-evidence "$run\evidence\host-vivado-bitstream-evidence.json" --output "$run\hardware-program" --cable-serial '<observed JTAG cable serial>'
```

Programming is volatile JTAG only. The script checks the actual cable ID and declared target under that cable, requires the logged configured-state observation after the actual download command. CRC/EOS/DONE/IDCODE/bad-packet fields are recorded diagnostics, and an unsupported optional config-status query is caught. A real download command failure is not suppressed. `programmed=true` describes that configuration observation only; `hardware_numerical_verified` remains false until complete live original-Python comparison. There is no default programming wall-clock limit. It refuses a bitstream whose hash, routed checkpoint or RTL differs from its own build evidence. It uses an independent non-queuing lock per JTAG cable, held by the OS during XSDB programming.
An EDA build may run concurrently; a second programmer of the same cable is refused.
The lock file remains in place; process exit releases the OS lock, so never delete the file to unlock it.

## Live numerical capture

Use the port and USB bridge serial number observed on the connected host and validated against the recorded board profile. The capture uses all rows named by the run's contract; a pinned `verification.samples` count must match exactly. Every request and received chunk is flushed to `events.jsonl`, including partial frames on timeout. The client requires complete payloads paired to requests and rejects stale/unpaired bytes, partial responses and extra bytes. Numeric capture records magic/sequence/reported-length/CRC/cycle anomalies as diagnostics without changing or selecting payload values. Strict protocol helpers remain available for debugging. It never retries a failed transaction into another sample's slot.

```powershell
& $py "$skill\verify_board.py" --deployment $deployment --board "$run\p3-build\host-board" --bitstream "$run\p3-build\host-board\board_top.bit" --io-scope "$run\evidence\host-port-audit\io-scope.json" --program-summary "$run\hardware-program\summary.json" --port '<observed COM port>' --serial-number '<observed CP2102 serial>' --output "$run\hardware-validation"
```

The verifier compares every captured value to original-model Python under `.001+.001*abs(ref)`; HLS CSim is optional diagnostic context. It writes `summary.json`, `python-evidence.json`, `hls-evidence.json`, `fpga-output.json`, `events.jsonl`, `provenance.json` and a file manifest. Only a complete live capture passing original-model Python comparison with execution provenance may set `hardware_numerical_verified=true`. A generated wrapper, passing simulation, bitstream, port audit or JTAG configuration alone never does.

The existing `bitstream-package` command packages the ROM variant only. Its `ports` stage audits the ROM routed checkpoint and includes `io-scope.json` with `configure-only` in the package. It does not package this host variant. Keep the host-board artifacts and on-board evidence in the run until a dedicated host package gate is available; do not relabel an old package as hardware verified.

## Attempt evidence and interrupted sessions

Both programming and capture create `summary.json` with false verification flags and
`attempt_complete=false` before tool/serial activity. Summary updates use atomic file
replacement. Normal completion sets `attempt_complete=true`; an interrupt records FAILED
and is re-raised. A hard process stop leaves the initial incomplete, failed record, never a
PASS. Keep every attempt directory. Supply a fresh `--output` path for a subsequent attempt;
existing directories, including old directories with no summary, are never overwritten.
For capture, a new path does not waive the configure → probe → capture procedure above.

### Odatix execution option

When the task explicitly requests Odatix, add --odatix <odatix.exe> and --odatix-output <fresh-directory> to build_host.py. The job calls the same host build and retains the same evidence, EDA lock, and checkpoint continuation rules. Read the batch log and the run-local build evidence together. This path uses run-local external inputs and always runs independently.

## Workflow-enabled host builds

When the deployment environment sets S15_ODATIX_EXE, the usual build_host.py command runs through Odatix and writes a fresh batch record under the host-board attempt. Use the batch and the existing stage evidence together. The board programming and numeric verification commands follow their existing evidence rules.

For a UART deployment, once HLS produces a compatible core and the host wrapper is available, build that wrapper directly with build_host.py. Run a ROM-only implementation when the task requires it. On failure, use the stage report and continue from the verified checkpoint of the same attempt where applicable. Read [Odatix candidate batches](../docs/odatix-candidate-batches.md) for the runtime and result format.
