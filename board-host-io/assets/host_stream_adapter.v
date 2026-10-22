`timescale 1ns/1ps
// Ordered core: INPUT_BEATS accepted input beats yield one output beat.
// Transport holds input_words stable from start until done. Idle zero windows flush
// the dataflow core. No whole-payload shift register or combinational division.
module host_stream_adapter #(
    parameter integer INPUT_BITS = 2048,
    parameter integer INPUT_WORDS_BITS = INPUT_BITS,
    parameter integer PORT_BITS = INPUT_BITS,
    parameter integer OUTPUT_BITS = 128,
    parameter integer INPUT_BEATS = 1
)(
    input wire clk, rst_n, start,
    input wire [INPUT_WORDS_BITS-1:0] input_words,
    output reg done,
    output reg [OUTPUT_BITS-1:0] output_words,
    output reg ap_start, core_rst_n,
    output wire [PORT_BITS-1:0] m_tdata,
    output reg m_tvalid,
    input wire m_tready,
    input wire [OUTPUT_BITS-1:0] s_tdata,
    input wire s_tvalid,
    output wire s_tready
);
    reg injecting, pending, armed;
    reg [31:0] output_count, target_index, window_index, score, beat_index;
    wire accepted = m_tvalid && m_tready;
    wire last_beat = beat_index == INPUT_BEATS-1;
    wire [PORT_BITS-1:0] beat_slice = input_words[beat_index*PORT_BITS +: PORT_BITS];
    assign m_tdata = injecting ? beat_slice : {PORT_BITS{1'b0}};
    assign s_tready = 1'b1;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            done <= 0; output_words <= 0; ap_start <= 0; core_rst_n <= 0; m_tvalid <= 0;
            injecting <= 0; pending <= 0; armed <= 0;
            output_count <= 0; target_index <= 0; window_index <= 0;
            score <= 0; beat_index <= 0;
        end else begin
            core_rst_n <= 1; ap_start <= 1; m_tvalid <= 1; done <= 0;
            if (start && !armed && !injecting && !pending) armed <= 1;
            if (accepted) begin
                if (score == INPUT_BEATS-1) begin
                    score <= 0; window_index <= window_index+1'b1;
                    // Switch AFTER accepting the last idle beat. Switching at score==0
                    // accepts an extra idle beat before the request. Never switch while stalled.
                    if (armed && !injecting && !pending) begin
                        injecting <= 1; armed <= 0; beat_index <= 0;
                        target_index <= window_index+1'b1;
                    end
                end else score <= score+1'b1;
                if (injecting) begin
                    if (last_beat) begin pending <= 1; injecting <= 0; end
                    else beat_index <= beat_index+1'b1;
                end
            end
            if (s_tvalid && s_tready) begin
                if ((pending || (injecting && accepted && last_beat)) && output_count==target_index) begin
                    output_words <= s_tdata; done <= 1; pending <= 0;
                end
                output_count <= output_count+1'b1;
            end
        end
    end
endmodule
