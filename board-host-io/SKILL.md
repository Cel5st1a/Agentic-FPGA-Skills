---
name: board-host-io
description: Use when a profile-bound hls4ml board build needs UART host input/output, routed-port auditing, volatile JTAG programming, or per-value on-board numerical verification.
---

# Board host I/O and numerical verification

This skill turns an already verified HLS core into a separate UART board variant. Its byte transport and UART RTL are copied byte-for-byte from the board-verified `cnn-board-03` run. Both adapters require fresh RTL, simulation and hardware evidence. The old bitstream proves only the old single-convolution model; it does not verify a frozen two-convolution encoder or any MLP.

Read [references/provenance.md](references/provenance.md) when judging whether prior board evidence applies to a model or bitstream.

## Supported boundary

**One authoritative source per geometry fact: the producer records it; downstream stages read and validate it. Never re-derive hardware geometry from contract shapes or reference rows.** Contract `precision` constrains the frozen numerical criterion and must equal the recorded input/output grids; it cannot fill a missing grid. Agents read and check records, not invent packing rules.

| Geometry | Authoritative field | Cross-check |
| --- | --- | --- |
| Input element count/order | ROM wrapper `input_layout.elements/flatten` | `c_order`, interface count, actual core port width and sample layout |
| Output element count/order | New wrapper `output_layout.elements/flatten` | AXIS `interface.stream.output_elements`; validated ap_vld scalar-port mapping |
| Input element grid | `rom_image.grid` | contract precision; AXIS `element_types.input` |
| Output element grid | `output_grid` | contract precision; AXIS `element_types.output`; ap_vld result port widths |
| Lane, beat and TDATA geometry | `interface.stream` | input beats = input elements / input lanes (exact division); single output beat; lane width equals element width; byte-aligned actual TDATA port |
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

For measured Vitis dataflow stream cores, the adapter keeps the core running with zero-valued idle windows. A request occupies a complete `INPUT_BEATS`-beat window; its output is identified by that window's ordinal. The transport holds the payload stable until done. The adapter switches into injection only after accepting the last idle beat, and advances its narrow slice only on handshake. Single-beat callers retain the `INPUT_BITS` parameter. The actual-core host integration simulation checks every contract row; a new core must demonstrate ordered one-output-per-input-window behavior before this policy is reused. See [recovery rules](../docs/deployment-recovery.md).

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
| one or more input beats per sample with exact lane division; exactly one output beat per sample | `host_stream_adapter.v` injects one complete input window and correlates its ordered output | `host_geometry` validates recorded counts/port widths; multi-output-beat cores remain unsupported |
| the generated element grid equals the contract's `precision` | the wire scale would otherwise come from the core while the acceptance criterion's LSB term comes from the contract | `host_geometry` refuses the mismatch |

All four hold for the cores measured so far. They are preconditions of the feed policy, not of the board.

**Operational consequence of the second row.** The transport's `QUARANTINE` state only leaves
when `done` is asserted, so a frame that the adapter *refuses* (a sticky `ap_done`, or a core
that never returns to `ap_idle`) wedges the transport there permanently: the host sees a
timeout, and on hardware the board needs a reset before it will answer again. That is the
price of refusing rather than inventing a `done` — inventing one would return a stale result
that passes both gates.

Measured on the AX7A200B (2026-09-29), the two failure modes must not be conflated:

| Observation | What it is | What it needs |
| --- | --- | --- |
| A frame the transport discards (e.g. a wrong length field) returns 0 bytes, and the next frame answers normally | a discarded frame, **not** a wedge — the transport's own RX-idle resynchronisation returns it to `SEEK_A5` | nothing |
| A capture stops part-way with `partial response: 0/N bytes` and the board then answers **nothing**, however many correct frames are sent | a wedged path | a reset before any further capture |
| A live capture misses an occasional single frame under a configuration that has already served thousands of frames | one lost transaction; the board is live immediately after | nothing, but the capture is still a failure: `verify_board.py` is fail-closed and never reuses a failed transaction into another sample's slot |

So "a timeout does not always mean a wedge" is true and must not be used to skip the reset.
Because a wedge is only distinguishable from a discarded frame by whether the board answers
*afterwards*, the always-safe rule is the one to apply before spending a capture.

**The only reset this toolchain can apply** to a purely volatile configuration is a full JTAG
re-configuration: `program_jtag.py` on the bitstream already in the board directory re-downloads
it and resets the whole fabric (`internal_rst_n` is driven by `MMCM.LOCKED` + `reset_sync`).
There is no power or reset key reachable from the host, and no separate reset script. Measured:
a board that answers nothing is answering every frame with a complete, CRC-valid response
immediately after one re-configuration of the same bitstream.

## Board session order — one capture per configuration, reset before each

This is a required procedure, not a suggestion. It applies whenever a host bitstream is put on
the board, and in particular when several models are brought up one after another in one
session.

For **each** model, in this order:

1. **Configure.** `program_jtag.py` with that model's bitstream, its routed port audit and its
   bitstream evidence. This configuration *is* the reset: never start a capture on a
   configuration whose history is unknown, including "the one the previous model left behind".
2. **Prove the path is live** on that same configuration, before spending the capture: send a
   probe request and require a complete, CRC-valid, decodable response frame. A minimal probe
   is one frame built with `protocol.encode_request` from the geometry
   `host_geometry.geometry_for_wrapper` returns, decoded with `protocol.decode_response`. If
   nothing comes back, re-configure and re-probe; do **not** proceed to the capture.
3. **Capture once.** `verify_board.py` into that model's own capture directory.
4. **On any failure, do not retry the capture.** Record the failure with its error, then
   re-configure (step 1) before the next model. `verify_board.py` is fail-closed by design and
   refusing to paper over a lost transaction with a retry is the point of it.

Never let one model's capture run on the previous model's configuration without re-configuring
in between. A capture that started on a stale configuration fails hours of work at sample 31
with a symptom (`partial response: 0/N bytes`) that looks like a broken design and is not one.

Measured cost of skipping this: a three-model session that configured back-to-back with no
reset completed only the first model (100/100) and stopped the other two at 84/100 and 31/100;
re-running the same three bitstreams with a re-configuration and a liveness probe before each
capture completed all three at 100/100, with errors identical to the first pass to the last
digit.

## Work in one fresh run

Bootstrap a new run with `scripts/line/bootstrap_run.py`; `--root` is the runs parent. Keep the contract copy, model, references and all new evidence in that run. Do not reuse the six delivered ROM bitstreams. Run `convert,csim,csynth,wrapper,boardsim` through `scripts/line/run_line.py` first. That ROM variant provides the existing numerical gate for the same core. The EDA lock rejects concurrent runs, so wait until the current run exits before starting the next.

After the ROM board simulation passes, create a **separate** board directory under the same run:

```powershell
$py = 'D:\Agentic_FPGA_Deployment - 副本\.venv\Scripts\python.exe'
$skill = 'C:\Users\Administrator\Agentic_FPGA_Deployment_skills\board-host-io\scripts'
$run = '<fresh bootstrap run directory>'
$deployment = '<that run\contract\model\deployment.yaml>'
& $py "$skill\make_host_board.py" --source-wrapper "$run\p3-build\board\wrapper.json" --output "$run\p3-build\host-board"
& $py "$skill\simulate_host.py" --deployment $deployment --board "$run\p3-build\host-board" --output "$run\evidence\host-sim"
& $py "$skill\build_host.py" --deployment $deployment --board "$run\p3-build\host-board"
```

`simulate_host.py` elaborates the generated host top, inherited transport, the adapter the core's interface selects, and the real HLS core together. It sends every contract row as a framed request at the byte boundary, decodes every response frame and uses the contract's existing Python/HLS comparisons. It bypasses only the already board-verified UART bit timing so the full data set is tractable in simulation. `build_host.py` requires its `PASSED` evidence for the same contract, model and core, then invokes `vivado-manager/run_vivado.py` for synth, impl and bitstream in order under `%TEMP%\fpga_skill_line_eda.lock`. Each stage must report `PASSED`; routed resource fraction and timing gates remain those of the contract. The host board gets a new `board_top.bit`; the old ROM package and bitstream remain separate.

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

Programming is volatile JTAG only. The script checks the actual cable ID and declared target under that cable, then requires CRC error 0, EOS 1, DONE 1, IDCODE error 0 and bad-packet error 0. It refuses a bitstream whose hash, routed checkpoint or RTL differs from its own build evidence. It uses an independent non-queuing lock per JTAG cable, held by the OS during XSDB programming.
An EDA build may run concurrently; a second programmer of the same cable is refused.
The lock file remains in place; process exit releases the OS lock, so never delete the file to unlock it.

## Live numerical capture

Use the port and USB bridge serial number observed on the connected host and validated against the recorded board profile. The capture uses all rows named by the run's contract; a pinned `verification.samples` count must match exactly. Every request and received chunk is flushed to `events.jsonl`, including partial frames on timeout. The client rejects stale bytes, partial responses, wrong or repeated sequence IDs, CRC errors, extra bytes and zero compute cycles. It never retries a failed transaction into another sample's slot.

```powershell
& $py "$skill\verify_board.py" --deployment $deployment --board "$run\p3-build\host-board" --bitstream "$run\p3-build\host-board\board_top.bit" --io-scope "$run\evidence\host-port-audit\io-scope.json" --program-summary "$run\hardware-program\summary.json" --port '<observed COM port>' --serial-number '<observed CP2102 serial>' --output "$run\hardware-validation"
```

The verifier compares every captured value to the run's Python output and HLS CSim output via `scripts/line/contract.py` and `scripts/line/evidence.py`; it never chooses a new tolerance. It writes `summary.json`, `python-evidence.json`, `hls-evidence.json`, `fpga-output.json`, `events.jsonl`, `provenance.json` and a file manifest. Only a complete live capture with both gates passed may set `hardware_numerical_verified=true`. A generated wrapper, passing simulation, bitstream, port audit or JTAG configuration alone never does.

The existing `bitstream-package` command packages the ROM variant only. Its `ports` stage audits the ROM routed checkpoint and includes `io-scope.json` with `configure-only` in the package. It does not package this host variant. Keep the host-board artifacts and on-board evidence in the run until a dedicated host package gate is available; do not relabel an old package as hardware verified.

## Attempt evidence and interrupted sessions

Both programming and capture create `summary.json` with false verification flags and
`attempt_complete=false` before tool/serial activity. Summary updates use atomic file
replacement. Normal completion sets `attempt_complete=true`; an interrupt records FAILED
and is re-raised. A hard process stop leaves the initial incomplete, failed record, never a
PASS. Keep every attempt directory. Supply a fresh `--output` path for a subsequent attempt;
existing directories, including old directories with no summary, are never overwritten.
For capture, a new path does not waive the configure → probe → capture procedure above.
