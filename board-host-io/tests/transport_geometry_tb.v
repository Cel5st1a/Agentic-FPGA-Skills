`timescale 1ns/1ps
// The transport at a synthetic geometry: a 160-byte payload, a 10-byte response, and
// 1280- / 80-bit buses. The cycle-exact differential run against the frozen original only
// covers the verified 64x4 grid, so this testbench is what shows the *parameters* actually
// frame a different geometry correctly.
//
// It does not re-implement CRC16 or the response layout: it emits the raw response bytes it
// produced and the Python protocol decoder has to accept them. That checks the RTL framing
// against the host protocol at a geometry neither was originally written for.
module transport_geometry_tb;
    localparam integer CLK_HZ = 100;
    localparam integer RX_TIMEOUT = 5000;
    localparam integer COMPUTE_TIMEOUT = 8000;
    localparam integer PAYLOAD_BYTES = 160;
    localparam integer RESPONSE_BYTES = 10;
    localparam integer INPUT_BITS = 1280;
    localparam integer OUTPUT_BITS = 80;
    localparam integer RESPONSE_TOTAL = RESPONSE_BYTES + 12;

    reg clk = 0, rst_n = 0;
    reg [7:0] rx_data = 0;
    reg rx_valid = 0;
    reg tx_ready = 1;
    reg done = 0;
    reg [OUTPUT_BITS-1:0] output_words = 0;

    wire [7:0] tx_data;
    wire tx_valid;
    wire [INPUT_BITS-1:0] input_words;
    wire start;

    cnn_uart_transport #(
        .CLK_HZ(CLK_HZ), .RX_TIMEOUT_CYCLES(RX_TIMEOUT),
        .COMPUTE_TIMEOUT_CYCLES(COMPUTE_TIMEOUT),
        .PAYLOAD_BYTES(PAYLOAD_BYTES), .RESPONSE_BYTES(RESPONSE_BYTES),
        .INPUT_BITS(INPUT_BITS), .OUTPUT_BITS(OUTPUT_BITS)
    ) dut (
        .clk(clk), .rst_n(rst_n),
        .uart_rx_data(rx_data), .uart_rx_valid(rx_valid),
        .uart_tx_data(tx_data), .uart_tx_valid(tx_valid), .uart_tx_ready(tx_ready),
        .input_words(input_words), .start(start), .done(done),
        .output_words(output_words));

    always #5 clk = ~clk;

    // The transport needs tx_ready to fall after each handshake.
    always @(posedge clk) begin
        if (!rst_n) tx_ready <= 1'b1;
        else if (tx_valid && tx_ready) tx_ready <= 1'b0;
        else tx_ready <= 1'b1;
    end

    // A deterministic accelerator: the low OUTPUT_BITS of the packed word, inverted, is
    // reported 30 cycles after the request.
    reg [15:0] since_start = 0;
    always @(posedge clk) begin
        if (!rst_n) begin
            since_start <= 0;
            done <= 1'b0;
            output_words <= 0;
        end else begin
            done <= 1'b0;
            if (start) begin
                since_start <= 16'd1;
                output_words <= input_words[OUTPUT_BITS-1:0] ^ {OUTPUT_BITS{1'b1}};
            end else if (since_start != 0) begin
                since_start <= since_start + 16'd1;
                if (since_start == 16'd30) done <= 1'b1;
            end
        end
    end

    reg [INPUT_BITS-1:0] expected_mem [0:0];
    reg [INPUT_BITS-1:0] expected_input;
    reg [7:0] stimulus [0:1023];
    reg [15:0] gaps [0:1023];
    integer stimulus_len, index, gap, tx_count = 0;
    integer response_file;
    integer start_seen = 0;
    integer input_errors = 0;

    always @(posedge clk) begin
        if (rst_n && start) begin
            start_seen = start_seen + 1;
            if (input_words !== expected_input) input_errors = input_errors + 1;
        end
    end

    always @(posedge clk) begin
        if (rst_n && tx_valid && tx_ready) begin
            if (tx_count >= RESPONSE_TOTAL) $fatal(1, "more response bytes than the frame holds");
            $fwrite(response_file, "%02x", tx_data);
            if (tx_count == RESPONSE_TOTAL - 1) $fwrite(response_file, "\n");
            tx_count <= tx_count + 1;
        end
    end

    initial begin
        $readmemh("stimulus_bytes.hex", stimulus);
        $readmemh("stimulus_gaps.hex", gaps);
        $readmemh("expected_input.hex", expected_mem);
        expected_input = expected_mem[0];
        stimulus_len = 0;
        for (index = 0; index < 1024; index = index + 1)
            if (gaps[index] !== 16'hxxxx) stimulus_len = index + 1;
        if (stimulus_len != PAYLOAD_BYTES + 8)
            $fatal(1, "expected a %0d-byte request, loaded %0d", PAYLOAD_BYTES + 8, stimulus_len);
        response_file = $fopen("response_frames.hex", "w");
        if (response_file == 0) $fatal(1, "cannot open response_frames.hex");

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

        for (index = 0; index < 2000 && tx_count < RESPONSE_TOTAL; index = index + 1)
            @(negedge clk);
        $fclose(response_file);

        if (start_seen != 1)
            $fatal(1, "expected one launch for one request, saw %0d", start_seen);
        if (input_errors != 0)
            $fatal(1, "the packed word presented to the core did not match the request");
        if (tx_count != RESPONSE_TOTAL)
            $fatal(1, "expected %0d response bytes, saw %0d", RESPONSE_TOTAL, tx_count);
        $display("TRANSPORT_GEOMETRY_PASS payload=%0d response=%0d in_bits=%0d out_bits=%0d",
                 PAYLOAD_BYTES, RESPONSE_TOTAL, INPUT_BITS, OUTPUT_BITS);
        $finish;
    end
endmodule
