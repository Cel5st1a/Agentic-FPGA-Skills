`timescale 1ns/1ps
// Host adapter for an ap_vld array core (io_parallel, `ap_vld_array_with_ap_ctrl_hs`).
//
// This is the A-side counterpart of `host_stream_adapter.v`. The transport presents a
// request as bytes written one at a time into `input_words`, then pulses `start` for a
// single cycle from its LAUNCH state. An ap_ctrl_hs core cannot be wired to that directly.
// Two things were measured on this core family, both recorded in the ROM board top:
//
//   1. `ap_start` is a LEVEL. Releasing it before the transaction reports done leaves the
//      core idle with a valid input and no `ap_done` (measured: ap_done low for 5M cycles
//      after a 12-cycle pulse). So the one-cycle `start` pulse must be latched.
//   2. `ae_input` must only be sampled once the whole packed word is stable. The transport
//      writes `input_words` byte by byte, so tying `ae_input_ap_vld` high makes the core
//      sample half-written words. So the input is latched on `start`, after the last byte.
//
// The response is a level-free one-cycle `done`, because the transport captures the result
// on the first `done && !start` cycle and its next frame's CRC check requires `done` low
// again. `done` is therefore released immediately, while a *new* request is not served until
// the core reports `ap_idle`.
//
// A request that arrives while the core is still unwinding is held, not dropped. That is
// safe because it can only happen in S_RELEASE, i.e. after this adapter has already told
// the transport `done` for the previous frame: the transport is transmitting that response
// and cannot be waiting on the held request, so serving it once the core goes idle cannot
// attribute a stale result to a new transaction. Holding is bounded by `ap_idle`; if the
// core never returns, the transport's own compute timeout fails the transaction closed
// rather than this adapter inventing a `done`.
//
// The one case that is *not* held: a `start` seen in S_IDLE while the core is still busy is
// dropped, and that frame fails closed through the transport's compute timeout. Holding it
// would mean starting a transaction the transport is no longer waiting for.
module host_avld_adapter #(
    parameter integer INPUT_WORDS_BITS = 2048,
    parameter integer CORE_INPUT_BITS = 2048,
    parameter integer OUTPUT_WORDS_BITS = 128,
    parameter integer CORE_OUTPUT_BITS = 128
)(
    input wire clk,
    input wire rst_n,
    // One-cycle request pulse from the transport.
    input wire start,
    // The transport's byte-written input register; read only when a request is taken.
    input wire [INPUT_WORDS_BITS-1:0] input_words,
    // One-cycle result-available pulse back to the transport.
    output reg done,
    output reg [OUTPUT_WORDS_BITS-1:0] output_words,
    // Stable packed word presented to the core for the whole transaction.
    output reg [CORE_INPUT_BITS-1:0] core_input,
    output reg core_input_vld,
    output reg ap_start,
    input wire ap_done,
    input wire ap_idle,
    input wire [CORE_OUTPUT_BITS-1:0] core_output
);
    localparam [1:0] S_IDLE = 2'd0, S_RUN = 2'd1, S_RELEASE = 2'd2;
    reg [1:0] state;
    reg held;
    reg [CORE_INPUT_BITS-1:0] held_input;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            held <= 1'b0;
            held_input <= {CORE_INPUT_BITS{1'b0}};
            done <= 1'b0;
            output_words <= {OUTPUT_WORDS_BITS{1'b0}};
            core_input <= {CORE_INPUT_BITS{1'b0}};
            core_input_vld <= 1'b0;
            ap_start <= 1'b0;
        end else begin
            // `done` is a one-cycle pulse unless a case below sets it again.
            done <= 1'b0;
            case (state)
                // Start from idle, exactly as the ROM board top does, so a core that is
                // still finishing an earlier transaction is never handed a new one.
                //
                // `!ap_done` is required as well as `ap_idle`. This core family deasserts
                // ap_done before it reports ap_idle, so for the shipped core the condition is
                // implied and costs nothing. For a core whose ap_done is a sticky level rather
                // than a completion pulse, starting here would let S_RUN see the *previous*
                // ap_done on its first cycle and report a stale `core_output` as this
                // request's result. The transport would accept it (valid sequence, valid CRC)
                // and the host would receive a silently wrong answer, so the request is
                // refused instead and the transport's own compute timeout fails it closed.
                S_IDLE: if (start && ap_idle && !ap_done) begin
                    core_input <= input_words[CORE_INPUT_BITS-1:0];
                    core_input_vld <= 1'b1;
                    ap_start <= 1'b1;
                    state <= S_RUN;
                end
                S_RUN: if (ap_done) begin
                    // The result ports are stable at ap_done; capture before releasing.
                    output_words <= core_output;
                    ap_start <= 1'b0;
                    core_input_vld <= 1'b0;
                    done <= 1'b1;
                    state <= S_RELEASE;
                end
                S_RELEASE: begin
                    if (ap_idle && !ap_done) begin
                        if (start || held) begin
                            // Present the live register when the request is arriving now,
                            // otherwise the value held from the cycle it arrived on.
                            core_input <= start ? input_words[CORE_INPUT_BITS-1:0] : held_input;
                            core_input_vld <= 1'b1;
                            ap_start <= 1'b1;
                            held <= 1'b0;
                            state <= S_RUN;
                        end else state <= S_IDLE;
                    end else if (start && !held) begin
                        held_input <= input_words[CORE_INPUT_BITS-1:0];
                        held <= 1'b1;
                    end
                end
                default: begin
                    state <= S_IDLE;
                    held <= 1'b0;
                    core_input_vld <= 1'b0;
                    ap_start <= 1'b0;
                end
            endcase
        end
    end
endmodule
