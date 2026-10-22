# Prior board proof, and the line between assets and geometry

Read this when judging whether prior board evidence applies to a model or bitstream.

## Verified UART assets — do not edit casually

These came from `D:\Agentic_FPGA_Deployment\runs\cnn-board-03` and were validated on real
hardware by `cnn-hardware-validation-01` (256/256 UART samples on COM3 at 115200, 1024
values against desktop HLS with maximum absolute error 0, and against Python with maximum
error `0.00013065338134765625`; the FPGA output JSON and desktop HLS output JSON both hash
to `6620606925cf6edf1a8b39e829716af0ae3a0c926bf5f50ceecbbd64fcfd250f`). That run's
bitstream SHA-256 is
`dd12049e6db728f3478e7465edaaddc230ef81069bb8308a3eb3446c94307160`.

| Asset | sha256 | Coupling to model geometry |
| --- | --- | --- |
| `assets/uart_rx.v` | `f5570b4c8f030694afd778d380b473b6987b7ef1f10e14dbeb762d4d4e3d5965` | none; parameterised by `CLK_FRE`/`BAUD_RATE` |
| `assets/uart_tx.v` | `f72aa61750168dee3c8c874a8c4edb3aaa2573400a2ce7bea3c68062c4151974` | none; same |
| `references/frozen/transport-96ceb796.v` | `96ceb7966f18fa06b44104ea067594196e42b5b73477c483ccb187e1846abbcb` | the byte-frozen original of `assets/transport.v` |

`uart_rx.v` and `uart_tx.v` are unchanged and must stay unchanged.

`transport.v` is the one asset that legitimately carries model geometry: its payload
length, response length and two bus widths. Those were lifted into parameters
(`PAYLOAD_BYTES`, `RESPONSE_BYTES`, `INPUT_BITS`, `OUTPUT_BITS`); the state machine, CRC,
timeout and `QUARANTINE` behaviour were not touched. **The original bytes are kept at
`references/frozen/transport-96ceb796.v` and are the reference for two gates**
(`tests/test_transport_parameterisation.py`): an explicit structural diff, and a cycle-exact
xsim equivalence run at the verified geometry. The frozen file is never staged into a build
and is not listed in `make_host_board.py`'s `SOURCE_HASHES`. Any further edit to
`transport.v` has to re-satisfy both gates and then update the pinned hash.

The board facts stay pinned to this hardware because the evidence above was measured on it:
AX7A200B, `xc7a200tfbg484-2`, UART on L14 (RX) / L15 (TX), LVCMOS33, DIFF_SSTL15 clock.

## Parameterisable geometry — derive it, never write it down

Frame geometry is **not** part of the verified asset. It is derived from the deployment
contract (grid and sample shape) plus the generated core's own interface record, by
`scripts/host_geometry.py` — the single implementation, imported by `make_host_board.py`,
`protocol.py`, `simulate_host.py` and `verify_board.py`. The formulas and the packing
convention are in `SKILL.md`. Do not add a second derivation, and do not edit the transport
to fit a different model: there is no length to edit.

Equivalence at the verified geometry is pinned, not assumed:
`tests/data/verified-64x32-request-golden.hex` was produced by the pre-change encoder from
the real contract rows of run `mlp_64_32/20260928-hosta`, and the derived implementation
must reproduce it byte for byte. Those same rows re-encoded after the change also reproduce
that run's `request_bytes.hex`
(`6da7283525f5e6fb2f76b270b4a37457d70ce82d7a86f6d32008df93febce45e`).

## What prior evidence does and does not transfer

`cnn-board-03` used a single `conv1d(4,k5)` → leaky ReLU → pool → dense(4) model
(`model.keras` SHA-256 `3aff492ee4b4dac473a57a0a19077c64a29f93d7b5c02b185ec626812a53cffa`)
with a **hand-written parallel board top** that tied `waveform_input_ap_vld` to `1'b1` — and
that core tolerated it. The frozen `asymmetric_cnn_fc` encoder has two convolution layers and
SHA-256 `33196d354a942a52e4fc8cfb3518e57c5f5257aa6d54dc4e027e4ebad7ac86fd`; its generated
core uses AXI-Stream. The old PASS transfers across neither difference.

The UART transport RTL did transfer, but only because the word geometry matched; that is
exactly the coupling this round removed. **The old `ap_vld` PASS does not transfer to another
`ap_vld` core either**: `mlp_64_32`'s core does not tolerate a one-cycle `ap_start` or a
tied-high `ap_vld`, which is defect D-M01 and is why `host_avld_adapter.v` exists.

There is no board evidence for either adapter as a module. `hardware_verified` stays `false`
and `on_board_numerical` stays `NOT_RUN` until a live capture passes both gates. A generated
wrapper, a passing simulation, a bitstream, a port audit or a JTAG configuration alone never
sets them. On 2026-09-28 the JTAG chain carried only the `Digilent JTAG-HS1 210512180081`
cable with no `xc7a200t` present (three probes agreed), so no live capture was possible and
every conclusion in this round is a simulation conclusion.

The audit trail and incident correction live at
`D:\ML_models\_line\reviews\prior-board-verified-io-run.md` and
`D:\ML_models\_line\reviews\incident-report-audit-20260928.md`. Inspect actual artifacts and
hashes before using that proof in a new report.
