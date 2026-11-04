`timescale 1ns/1ps
// Optional chunked_v1 byte transport. One full input sample starts the core once.
// RX storage is invalid until all ordered chunks pass CRC. No model arithmetic here.
module cnn_uart_chunk_transport #(
    parameter integer CLK_HZ=100000000,
    parameter integer RX_TIMEOUT_CYCLES=CLK_HZ,
    parameter integer PAYLOAD_BYTES=256,
    parameter integer RESPONSE_BYTES=16,
    parameter integer CHUNK_BYTES=4096,
    parameter integer INPUT_BITS=PAYLOAD_BYTES*8,
    parameter integer OUTPUT_BITS=RESPONSE_BYTES*8
)(
    input wire clk,rst_n,
    input wire [7:0] uart_rx_data,
    input wire uart_rx_valid,
    output reg [7:0] uart_tx_data,
    output reg uart_tx_valid,
    input wire uart_tx_ready,
    output reg [INPUT_BITS-1:0] input_words,
    output reg start,
    input wire done,
    input wire [OUTPUT_BITS-1:0] output_words
);
    localparam SEEK_A6=0, SEEK_6A=1, HEADER=2, PAYLOAD=3, CRC_LO=4, CRC_HI=5,
        TX_PREP=6, TX_ACCEPT=7, TX_BUSY=8, TX_IDLE=9, LAUNCH=10, COMPUTE=11, DISCARD=12;
    reg [3:0] state;
    reg [3:0] header_index;
    reg [15:0] sequence_id,active_sequence,body_len,rx_crc,tx_crc;
    reg [7:0] length_low,crc_low;
    reg [31:0] rx_offset,rx_total,rx_size,rx_index,expected_offset,rx_wait;
    reg active,ack_frame,complete_input;
    reg [31:0] response_offset,response_total,tx_size,tx_index;
    reg [31:0] compute_count,captured_cycles;
    reg [OUTPUT_BITS-1:0] captured_output;
    wire receiving=(state>=SEEK_6A && state<=CRC_HI) || state==DISCARD;
    wire [31:0] incoming_total={uart_rx_data,rx_total[23:0]};
    wire [31:0] response_length=12+tx_size;
    wire [31:0] response_frame_size=20+tx_size;

    function [15:0] crc16_update;
        input [15:0] crc;
        input [7:0] data;
        reg [15:0] c;
        integer b;
        begin
            c=crc^{data,8'h00};
            for(b=0;b<8;b=b+1) c=c[15]?(c<<1)^16'h1021:(c<<1);
            crc16_update=c;
        end
    endfunction
    function [7:0] response_byte;
        input [31:0] index;
        begin
            if(index==response_frame_size-2) response_byte=tx_crc[7:0];
            else if(index==response_frame_size-1) response_byte=tx_crc[15:8];
            else case(index)
                0: response_byte=8'h6a;
                1: response_byte=8'ha6;
                2: response_byte=active_sequence[7:0];
                3: response_byte=active_sequence[15:8];
                4: response_byte=response_length[7:0];
                5: response_byte=response_length[15:8];
                6,7,8,9: response_byte=response_offset[(index-6)*8+:8];
                10,11,12,13: response_byte=response_total[(index-10)*8+:8];
                14,15,16,17: response_byte=ack_frame?8'h00:captured_cycles[(index-14)*8+:8];
                default: response_byte=captured_output[(response_offset+index-18)*8+:8];
            endcase
        end
    endfunction
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=SEEK_A6;header_index<=0;sequence_id<=0;active_sequence<=0;
            body_len<=0;length_low<=0;crc_low<=0;rx_crc<=16'hffff;tx_crc<=16'hffff;
            rx_offset<=0;rx_total<=0;rx_size<=0;rx_index<=0;expected_offset<=0;rx_wait<=0;
            active<=0;ack_frame<=0;complete_input<=0;response_offset<=0;
            response_total<=0;tx_size<=0;tx_index<=0;compute_count<=0;captured_cycles<=0;
            captured_output<=0;input_words<=0;start<=0;uart_tx_data<=0;uart_tx_valid<=0;
        end else begin
            start<=0;
            if(!receiving || uart_rx_valid) rx_wait<=0;else rx_wait<=rx_wait+1'b1;
            if(receiving && !uart_rx_valid && rx_wait==RX_TIMEOUT_CYCLES-1) begin
                state<=SEEK_A6;active<=0;expected_offset<=0;rx_wait<=0;
            end else case(state)
                SEEK_A6: if(uart_rx_valid && uart_rx_data==8'ha6) begin
                    rx_crc<=crc16_update(16'hffff,8'ha6);state<=SEEK_6A;
                end
                SEEK_6A: if(uart_rx_valid) begin
                    if(uart_rx_data==8'h6a) begin
                        rx_crc<=crc16_update(rx_crc,uart_rx_data);header_index<=2;state<=HEADER;
                    end else if(uart_rx_data==8'ha6) rx_crc<=crc16_update(16'hffff,8'ha6);
                    else state<=SEEK_A6;
                end
                HEADER: if(uart_rx_valid) begin
                    rx_crc<=crc16_update(rx_crc,uart_rx_data);header_index<=header_index+1'b1;
                    case(header_index)
                        2: sequence_id[7:0]<=uart_rx_data;
                        3: sequence_id[15:8]<=uart_rx_data;
                        4: length_low<=uart_rx_data;
                        5: body_len<={uart_rx_data,length_low};
                        6,7,8,9: rx_offset[(header_index-6)*8+:8]<=uart_rx_data;
                        10,11,12: rx_total[(header_index-10)*8+:8]<=uart_rx_data;
                        13: begin
                            rx_total[31:24]<=uart_rx_data;
                            if(body_len>=9 && body_len<=CHUNK_BYTES+8 &&
                               incoming_total==PAYLOAD_BYTES && rx_offset<PAYLOAD_BYTES &&
                               ({1'b0,rx_offset}+body_len-8)<=PAYLOAD_BYTES &&
                               (rx_offset==0 || (active && sequence_id==active_sequence &&
                                                rx_offset==expected_offset))) begin
                                // Fixed chunk schedule: only the final block may be short.
                                if(body_len-8==((PAYLOAD_BYTES-rx_offset>CHUNK_BYTES)?
                                                 CHUNK_BYTES:PAYLOAD_BYTES-rx_offset)) begin
                                    if(rx_offset==0) begin
                                        active_sequence<=sequence_id;active<=1;expected_offset<=0;
                                    end
                                    rx_size<=body_len-8;rx_index<=0;state<=PAYLOAD;
                                end else begin state<=DISCARD;active<=0;expected_offset<=0;end
                            end else begin state<=DISCARD;active<=0;expected_offset<=0;end
                        end
                        default: begin state<=DISCARD;active<=0;expected_offset<=0;end
                    endcase
                end
                PAYLOAD: if(uart_rx_valid) begin
                    input_words[(rx_offset+rx_index)*8+:8]<=uart_rx_data;
                    rx_crc<=crc16_update(rx_crc,uart_rx_data);
                    if(rx_index==rx_size-1) state<=CRC_LO;else rx_index<=rx_index+1'b1;
                end
                CRC_LO: if(uart_rx_valid) begin crc_low<=uart_rx_data;state<=CRC_HI;end
                CRC_HI: if(uart_rx_valid) begin
                    if({uart_rx_data,crc_low}==rx_crc) begin
                        expected_offset<=rx_offset+rx_size;
                        response_offset<=rx_offset+rx_size;response_total<=PAYLOAD_BYTES;
                        complete_input<=(rx_offset+rx_size==PAYLOAD_BYTES);
                        ack_frame<=1;tx_size<=0;tx_index<=0;tx_crc<=16'hffff;state<=TX_PREP;
                    end else begin state<=SEEK_A6;active<=0;expected_offset<=0;end
                end
                LAUNCH: begin start<=1;compute_count<=0;state<=COMPUTE;end
                COMPUTE: begin
                    // Diagnostic elapsed clocks through the adapter, not kernel latency.
                    if(done && !start) begin
                        captured_output<=output_words;captured_cycles<=compute_count;
                        response_offset<=0;response_total<=RESPONSE_BYTES;
                        tx_size<=(RESPONSE_BYTES>CHUNK_BYTES)?CHUNK_BYTES:RESPONSE_BYTES;
                        ack_frame<=0;tx_index<=0;tx_crc<=16'hffff;state<=TX_PREP;
                    end else compute_count<=compute_count+1'b1;
                end
                TX_PREP: if(uart_tx_ready) begin
                    uart_tx_data<=response_byte(tx_index);uart_tx_valid<=1;state<=TX_ACCEPT;
                end
                TX_ACCEPT: if(uart_tx_ready) begin
                    uart_tx_valid<=0;
                    if(tx_index<response_frame_size-2) tx_crc<=crc16_update(tx_crc,uart_tx_data);
                    state<=TX_BUSY;
                end
                TX_BUSY: if(!uart_tx_ready) state<=TX_IDLE;
                TX_IDLE: if(uart_tx_ready) begin
                    if(tx_index==response_frame_size-1) begin
                        if(ack_frame) state<=complete_input?LAUNCH:SEEK_A6;
                        else if(response_offset+tx_size==RESPONSE_BYTES) begin
                            state<=SEEK_A6;active<=0;expected_offset<=0;
                        end else begin
                            response_offset<=response_offset+tx_size;
                            tx_size<=((RESPONSE_BYTES-response_offset-tx_size)>CHUNK_BYTES)?
                                CHUNK_BYTES:RESPONSE_BYTES-response_offset-tx_size;
                            tx_index<=0;tx_crc<=16'hffff;state<=TX_PREP;
                        end
                    end else begin tx_index<=tx_index+1'b1;state<=TX_PREP;end
                end
                DISCARD: begin end // Require an RX idle gap before accepting a fresh sample.
                default: begin state<=SEEK_A6;active<=0;expected_offset<=0;uart_tx_valid<=0;end
            endcase
        end
    end
endmodule
