`timescale 1ns/1ps
// Cycle-exact equivalence between the byte-frozen board-verified transport and the
// geometry-parameterised one, both driven with the same stimulus.
//
// `cnn_uart_transport_ref` is the frozen original verbatim (the test copies it and renames
// only the module). The parameterised instance uses the verified 64x4 values, so every
// parameter collapses to the literal it replaced. Any port diverging on any cycle is a
// behavioural change and fails the run.
module transport_equivalence_tb;
    localparam integer CLK_HZ = 100;
    localparam integer RX_TIMEOUT = 3000;
    localparam integer COMPUTE_TIMEOUT = 4000;

    reg clk = 0, rst_n = 0;
    reg [7:0] rx_data = 0;
    reg rx_valid = 0;
    reg tx_ready = 1;
    reg done = 0;

    // Parameterised transport at the verified geometry.
    wire [7:0]  a_tx_data;
    wire        a_tx_valid;
    wire [2047:0] a_input_words;
    wire        a_start;
    wire [127:0] a_output;

    // Frozen original.
    wire [7:0]  b_tx_data;
    wire        b_tx_valid;
    wire [2047:0] b_input_words;
    wire        b_start;

    reg [127:0] captured_output = 128'h0123456789abcdef00ff00ff55aa55aa;
    assign a_output = captured_output;

    cnn_uart_transport #(
        .CLK_HZ(CLK_HZ), .RX_TIMEOUT_CYCLES(RX_TIMEOUT),
        .COMPUTE_TIMEOUT_CYCLES(COMPUTE_TIMEOUT),
        .PAYLOAD_BYTES(256), .RESPONSE_BYTES(16), .INPUT_BITS(2048), .OUTPUT_BITS(128)
    ) dut_a (
        .clk(clk), .rst_n(rst_n),
        .uart_rx_data(rx_data), .uart_rx_valid(rx_valid),
        .uart_tx_data(a_tx_data), .uart_tx_valid(a_tx_valid), .uart_tx_ready(tx_ready),
        .input_words(a_input_words), .start(a_start), .done(done),
        .output_words(a_output));

    cnn_uart_transport_ref #(
        .CLK_HZ(CLK_HZ), .RX_TIMEOUT_CYCLES(RX_TIMEOUT),
        .COMPUTE_TIMEOUT_CYCLES(COMPUTE_TIMEOUT)
    ) dut_b (
        .clk(clk), .rst_n(rst_n),
        .uart_rx_data(rx_data), .uart_rx_valid(rx_valid),
        .uart_tx_data(b_tx_data), .uart_tx_valid(b_tx_valid), .uart_tx_ready(tx_ready),
        .input_words(b_input_words), .start(b_start), .done(done),
        .output_words(captured_output));

    always #5 clk = ~clk;

    // Byte-accepting sink: the transport needs tx_ready to fall after a handshake.
    always @(posedge clk) begin
        if (!rst_n) tx_ready <= 1'b1;
        else if (a_tx_valid && tx_ready) tx_ready <= 1'b0;
        else tx_ready <= 1'b1;
    end

    // A deterministic accelerator: done rises 200 cycles after a start and stays high for
    // 1800 cycles, which is long enough that an immediate repeat frame reaches CRC_HI
    // while done is still high (the transport must refuse it) and short enough that a
    // settled frame 4000 cycles later is accepted.
    reg [15:0] since_start = 0;
    always @(posedge clk) begin
        if (!rst_n) begin
            since_start <= 0;
            done <= 0;
        end else begin
            if (a_start || b_start) since_start <= 16'd1;
            else if (since_start != 0) since_start <= since_start + 16'd1;
            done <= (since_start > 16'd200) && (since_start < 16'd2000);
        end
    end

    // The gate: every observable port must agree on every clock edge.
    integer compared = 0;
    always @(posedge clk) begin
        if (rst_n) begin
            compared = compared + 1;
            if (a_tx_data !== b_tx_data)
                $fatal(1, "tx_data diverged at %0t: %02x vs %02x", $time, a_tx_data, b_tx_data);
            if (a_tx_valid !== b_tx_valid)
                $fatal(1, "tx_valid diverged at %0t", $time);
            if (a_input_words !== b_input_words)
                $fatal(1, "input_words diverged at %0t", $time);
            if (a_start !== b_start)
                $fatal(1, "start diverged at %0t", $time);
        end
    end

    reg [7:0] stimulus [0:4095];
    reg [15:0] gaps [0:4095];
    integer stimulus_len, index, gap, wait_cycle;
    integer seen_start = 0, seen_tx = 0;

    always @(posedge clk) if (rst_n) begin
        if (a_start) seen_start = seen_start + 1;
        if (a_tx_valid && tx_ready) seen_tx = seen_tx + 1;
    end

    initial begin
        $readmemh("stimulus_bytes.hex", stimulus);
        $readmemh("stimulus_gaps.hex", gaps);
        stimulus_len = 0;
        // The stimulus files are dense; count entries by their gap words, which are never
        // zero for a real byte, and stop at the first unwritten entry.
        for (index = 0; index < 4096; index = index + 1)
            if (gaps[index] !== 16'hxxxx) stimulus_len = index + 1;
        if (stimulus_len == 0) $fatal(1, "no stimulus loaded");

        repeat (10) @(negedge clk);
        rst_n = 1;
        repeat (10) @(negedge clk);

        for (index = 0; index < stimulus_len; index = index + 1) begin
            for (gap = 0; gap < gaps[index]; gap = gap + 1) @(negedge clk);
            rx_data = stimulus[index];
            rx_valid = 1;
            @(negedge clk);
            rx_valid = 0;
            @(negedge clk);
        end
        repeat (20000) @(negedge clk);

        // The scenario design is deterministic, so these are exact: four launching frames
        // (the immediate repeat must be refused while done is still high) and four 28-byte
        // responses. A drift here means the stimulus stopped exercising the transport.
        if (compared < 1000)
            $fatal(1, "equivalence ran too few cycles to mean anything: %0d", compared);
        if (seen_start != 4)
            $fatal(1, "expected 4 launched transactions, saw %0d", seen_start);
        if (seen_tx != 112)
            $fatal(1, "expected 112 response bytes, saw %0d", seen_tx);
        $display("TRANSPORT_EQUIVALENCE_PASS cycles=%0d starts=%0d txbytes=%0d",
                 compared, seen_start, seen_tx);
        $finish;
    end
endmodule
