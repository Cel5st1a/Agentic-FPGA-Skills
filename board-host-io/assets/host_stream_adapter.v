`timescale 1ns/1ps
// Ordered core: INPUT_BEATS accepted inputs yield OUTPUT_BEATS output beats.
// Transport holds input_words stable from start until done. Idle zero windows flush
// the dataflow core. Sample storage moves only on accepted beats. Constant shifts
// avoid runtime selection/write positions over a whole sample; this trades extra
// input storage for a simpler selection network, not guaranteed physical savings.
module host_stream_adapter #(
    parameter integer INPUT_BITS = 2048,
    parameter integer INPUT_WORDS_BITS = INPUT_BITS,
    parameter integer PORT_BITS = INPUT_BITS,
    parameter integer OUTPUT_BITS = 128,
    parameter integer OUTPUT_PORT_BITS = OUTPUT_BITS,
    parameter integer OUTPUT_BEATS = 1,
    parameter integer INPUT_BEATS = 1,
    parameter integer INPUT_ELEMENT_BITS = PORT_BITS,
    parameter integer OUTPUT_ELEMENT_BITS = OUTPUT_PORT_BITS,
    parameter integer INPUT_LANES = 1,
    parameter integer OUTPUT_LANES = 1,
    parameter integer INPUT_SLOT_BITS = INPUT_ELEMENT_BITS,
    parameter integer OUTPUT_SLOT_BITS = OUTPUT_ELEMENT_BITS
)(
    input wire clk, rst_n, start,
    input wire [INPUT_WORDS_BITS-1:0] input_words,
    output reg done,
    output reg [OUTPUT_BITS-1:0] output_words,
    output reg ap_start, core_rst_n,
    output wire [PORT_BITS-1:0] m_tdata,
    output reg m_tvalid,
    input wire m_tready,
    input wire [OUTPUT_PORT_BITS-1:0] s_tdata,
    input wire s_tvalid,
    output wire s_tready
);
    reg injecting, pending, armed, result_ready;
    reg [31:0] output_count, target_index, window_index, score, beat_index;
    reg [31:0] output_beat_index;
    reg [INPUT_WORDS_BITS-1:0] input_window;
    localparam integer INPUT_STRIDE = INPUT_LANES*INPUT_ELEMENT_BITS;
    localparam integer OUTPUT_STRIDE = OUTPUT_LANES*OUTPUT_ELEMENT_BITS;
    wire [OUTPUT_STRIDE-1:0] output_beat;
    wire [OUTPUT_BITS-1:0] output_next =
        (output_words >> OUTPUT_STRIDE) |
        ({{(OUTPUT_BITS-OUTPUT_STRIDE){1'b0}}, output_beat}
         << (OUTPUT_BITS-OUTPUT_STRIDE));
    wire accepted = m_tvalid && m_tready;
    wire last_beat = beat_index == INPUT_BEATS-1;
    wire last_output_beat = output_beat_index == OUTPUT_BEATS-1;
    wire input_complete = pending || (injecting && accepted && last_beat);
    genvar input_lane;
    generate for(input_lane=0;input_lane<INPUT_LANES;input_lane=input_lane+1) begin: pack_input
        wire [INPUT_ELEMENT_BITS-1:0] element =
            input_window[input_lane*INPUT_ELEMENT_BITS+:INPUT_ELEMENT_BITS];
        assign m_tdata[input_lane*INPUT_SLOT_BITS+:INPUT_SLOT_BITS] = injecting ?
            {{(INPUT_SLOT_BITS-INPUT_ELEMENT_BITS){element[INPUT_ELEMENT_BITS-1]}},element} :
            {INPUT_SLOT_BITS{1'b0}};
    end endgenerate
    genvar output_lane;
    generate for(output_lane=0;output_lane<OUTPUT_LANES;output_lane=output_lane+1) begin: unpack_output
        assign output_beat[output_lane*OUTPUT_ELEMENT_BITS+:OUTPUT_ELEMENT_BITS] =
            s_tdata[output_lane*OUTPUT_SLOT_BITS+:OUTPUT_ELEMENT_BITS];
    end endgenerate
    assign s_tready = 1'b1;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            done <= 0; output_words <= 0; ap_start <= 0; core_rst_n <= 0; m_tvalid <= 0;
            injecting <= 0; pending <= 0; armed <= 0; result_ready <= 0;
            output_count <= 0; target_index <= 0; window_index <= 0;
            score <= 0; beat_index <= 0; output_beat_index <= 0; input_window <= 0;
        end else begin
            core_rst_n <= 1; ap_start <= 1; m_tvalid <= 1; done <= 0;
            if (start && !armed && !injecting && !pending && !result_ready) armed <= 1;
            if (accepted) begin
                if (score == INPUT_BEATS-1) begin
                    score <= 0; window_index <= window_index+1'b1;
                    // Switch AFTER accepting the last idle beat. Switching at score==0
                    // accepts an extra idle beat before the request. Never switch while stalled.
                    if (armed && !injecting && !pending) begin
                        injecting <= 1; armed <= 0; beat_index <= 0;
                        target_index <= window_index+1'b1;
                        input_window <= input_words;
                    end
                end else score <= score+1'b1;
                if (injecting) begin
                    if (last_beat) begin pending <= 1; injecting <= 0; end
                    else begin
                        beat_index <= beat_index+1'b1;
                        input_window <= input_window >> INPUT_STRIDE;
                    end
                end
            end
            if (result_ready && input_complete) begin
                done <= 1; pending <= 0; result_ready <= 0;
            end
            if (s_tvalid && s_tready) begin
                if ((pending || injecting) && output_count==target_index) begin
                    output_words <= output_next;
                    if (last_output_beat) begin
                        if (input_complete) begin done <= 1; pending <= 0; end
                        else result_ready <= 1;
                    end
                end
                if (last_output_beat) begin
                    output_count <= output_count+1'b1;
                    output_beat_index <= 0;
                end else begin
                    output_beat_index <= output_beat_index+1'b1;
                end
            end
        end
    end
endmodule
