`timescale 1ns/1ps
// Byte-level, single-clock transport. Board UART and HLS wiring live outside.
//
// This body is the board-verified original (sha256 96ceb7966f18fa06...) with the frame
// geometry lifted into parameters. The state machine, CRC, timeout and QUARANTINE
// behaviour are unchanged, and `tests/test_transport_parameterisation.py` proves that two
// ways: an explicit structural diff against `references/frozen/transport-96ceb796.v`, and
// a cycle-exact xsim run of the frozen module beside this one at the verified geometry.
//
// At the verified geometry (256-byte payload, 16 result bytes, 2048/128-bit buses) every
// parameter below collapses to the literal it replaced:
//   PAYLOAD_BYTES=256 RESPONSE_BYTES=16 INPUT_BITS=2048 OUTPUT_BITS=128
module cnn_uart_transport #(
    parameter integer CLK_HZ = 100000000,
    parameter integer RX_TIMEOUT_CYCLES = CLK_HZ,
    parameter integer COMPUTE_TIMEOUT_CYCLES = CLK_HZ,
    parameter integer PAYLOAD_BYTES = 256,
    parameter integer RESPONSE_BYTES = 16,
    parameter integer INPUT_BITS = 2048,
    parameter integer OUTPUT_BITS = 128
)(
    input wire clk,
    input wire rst_n,
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
    localparam [3:0] SEEK_A5 = 0, SEEK_5A = 1, HEADER = 2,
        PAYLOAD = 3, CRC_LO = 4, CRC_HI = 5, LAUNCH = 6,
        COMPUTE = 7, QUARANTINE = 8, DONE_LOW = 9, DISCARD = 10,
        TX_PREP = 11, TX_ACCEPT = 12, TX_BUSY = 13, TX_IDLE = 14;
    localparam integer RX_COUNT_WIDTH = (RX_TIMEOUT_CYCLES > 1) ?
        $clog2(RX_TIMEOUT_CYCLES) : 1;
    // Frame geometry derived from the parameters, never hand-written twice.
    localparam integer RESPONSE_TOTAL = RESPONSE_BYTES + 12;
    localparam integer PAYLOAD_INDEX_BITS = (PAYLOAD_BYTES > 1) ? $clog2(PAYLOAD_BYTES) : 1;
    localparam integer TX_INDEX_BITS = (RESPONSE_TOTAL > 1) ? $clog2(RESPONSE_TOTAL) : 1;
    localparam [7:0] PAYLOAD_LOW = PAYLOAD_BYTES % 256;
    localparam [7:0] PAYLOAD_HIGH = (PAYLOAD_BYTES / 256) % 256;
    localparam [7:0] RESPONSE_LENGTH = RESPONSE_BYTES + 4;
    reg [3:0] state;
    reg [2:0] header_index;
    reg [PAYLOAD_INDEX_BITS-1:0] payload_index;
    reg [15:0] sequence_id, rx_crc, tx_crc;
    reg [7:0] length_low, received_crc_low;
    reg [RX_COUNT_WIDTH-1:0] rx_wait;
    reg [31:0] compute_count, captured_cycles;
    reg [OUTPUT_BITS-1:0] captured_output;
    reg [TX_INDEX_BITS-1:0] tx_index;
    wire receiving = (state >= SEEK_5A && state <= CRC_HI) || state == DISCARD;

    function [15:0] crc16_update;
        input [15:0] crc;
        input [7:0] data;
        reg [15:0] c;
        integer bit_index;
        begin
            c = crc ^ {data, 8'h00};
            for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1)
                c = c[15] ? (c << 1) ^ 16'h1021 : (c << 1);
            crc16_update = c;
        end
    endfunction

    function [7:0] response_byte;
        input [TX_INDEX_BITS-1:0] index;
        begin
            case (index)
                0: response_byte = 8'h5a;
                1: response_byte = 8'ha5;
                2: response_byte = sequence_id[7:0];
                3: response_byte = sequence_id[15:8];
                4: response_byte = RESPONSE_LENGTH;
                5: response_byte = 0;
                6, 7, 8, 9: response_byte = captured_cycles[(index-6)*8 +: 8];
                RESPONSE_TOTAL-2: response_byte = tx_crc[7:0];
                RESPONSE_TOTAL-1: response_byte = tx_crc[15:8];
                default: response_byte = captured_output[(index-10)*8 +: 8];
            endcase
        end
    endfunction

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= SEEK_A5;
            header_index <= 0;
            payload_index <= 0;
            sequence_id <= 0;
            length_low <= 0;
            received_crc_low <= 0;
            rx_crc <= 16'hffff;
            tx_crc <= 16'hffff;
            rx_wait <= 0;
            compute_count <= 0;
            captured_cycles <= 0;
            captured_output <= 0;
            tx_index <= 0;
            uart_tx_data <= 0;
            uart_tx_valid <= 0;
            input_words <= 0;
            start <= 0;
        end else begin
            start <= 0;
            if (!receiving || uart_rx_valid) rx_wait <= 0;
            else rx_wait <= rx_wait + 1'b1;

            // A byte on the boundary wins over the idle timeout. Storage is
            // fixed; invalid lengths never become an allocation or loop bound.
            if (receiving && !uart_rx_valid && rx_wait == RX_TIMEOUT_CYCLES-1) begin
                state <= SEEK_A5;
                rx_wait <= 0;
            end else case (state)
                SEEK_A5: if (uart_rx_valid && uart_rx_data == 8'ha5) begin
                    rx_crc <= crc16_update(16'hffff, 8'ha5);
                    state <= SEEK_5A;
                end
                SEEK_5A: if (uart_rx_valid) begin
                    if (uart_rx_data == 8'h5a) begin
                        rx_crc <= crc16_update(rx_crc, uart_rx_data);
                        header_index <= 2;
                        state <= HEADER;
                    end else if (uart_rx_data == 8'ha5)
                        rx_crc <= crc16_update(16'hffff, 8'ha5);
                    else state <= SEEK_A5;
                end
                HEADER: if (uart_rx_valid) begin
                    rx_crc <= crc16_update(rx_crc, uart_rx_data);
                    header_index <= header_index + 1'b1;
                    case (header_index)
                        2: sequence_id[7:0] <= uart_rx_data;
                        3: sequence_id[15:8] <= uart_rx_data;
                        4: length_low <= uart_rx_data;
                        5: begin
                            if (length_low == PAYLOAD_LOW && uart_rx_data == PAYLOAD_HIGH) begin
                                payload_index <= 0;
                                state <= PAYLOAD;
                            end else state <= DISCARD;
                        end
                        default: state <= DISCARD;
                    endcase
                end
                PAYLOAD: if (uart_rx_valid) begin
                    input_words[payload_index*8 +: 8] <= uart_rx_data;
                    rx_crc <= crc16_update(rx_crc, uart_rx_data);
                    if (payload_index == PAYLOAD_BYTES-1) state <= CRC_LO;
                    else payload_index <= payload_index + 1'b1;
                end
                CRC_LO: if (uart_rx_valid) begin
                    received_crc_low <= uart_rx_data;
                    state <= CRC_HI;
                end
                CRC_HI: if (uart_rx_valid) begin
                    if ({uart_rx_data, received_crc_low} == rx_crc && !done)
                        state <= LAUNCH;
                    else state <= SEEK_A5;
                end
                LAUNCH: begin
                    start <= 1;
                    compute_count <= 0;
                    state <= COMPUTE;
                end
                COMPUTE: begin
                    // Count elapsed rising edges from accelerator sampling
                    // start to transport sampling done. Deadline is exclusive.
                    if (compute_count >= COMPUTE_TIMEOUT_CYCLES) begin
                        if (done) state <= DONE_LOW;
                        else state <= QUARANTINE;
                    end else if (done && !start) begin
                        captured_output <= output_words;
                        captured_cycles <= compute_count;
                        tx_crc <= 16'hffff;
                        tx_index <= 0;
                        state <= TX_PREP;
                    end else compute_count <= compute_count + 1'b1;
                end
                // No accelerator reset/transaction tag is available. Starting
                // again before its late done could misattribute old results.
                QUARANTINE: if (done) state <= DONE_LOW;
                DONE_LOW: if (!done) begin
                    state <= DISCARD;
                    rx_wait <= 0;
                end
                DISCARD: begin end // require RX idle gap before resynchronizing
                TX_PREP: if (uart_tx_ready) begin
                    uart_tx_data <= response_byte(tx_index);
                    uart_tx_valid <= 1;
                    state <= TX_ACCEPT;
                end
                TX_ACCEPT: if (uart_tx_ready) begin
                    uart_tx_valid <= 0;
                    if (tx_index < RESPONSE_TOTAL-2) tx_crc <= crc16_update(tx_crc, uart_tx_data);
                    state <= TX_BUSY;
                end
                TX_BUSY: if (!uart_tx_ready) state <= TX_IDLE;
                TX_IDLE: if (uart_tx_ready) begin
                    if (tx_index == RESPONSE_TOTAL-1) state <= SEEK_A5;
                    else begin
                        tx_index <= tx_index + 1'b1;
                        state <= TX_PREP;
                    end
                end
                default: begin
                    state <= SEEK_A5;
                    uart_tx_valid <= 0;
                end
            endcase
        end
    end
endmodule
