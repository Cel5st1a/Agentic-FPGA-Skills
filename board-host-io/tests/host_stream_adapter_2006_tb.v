`timescale 1ns/1ps
// The stream adapter at a synthetic ap_fixed<20,6> geometry: 64 elements x 20 bits in
// (1280 bits) and 4 x 20 bits out (80 bits). The verified 64x4 sizes must only be
// parameter defaults, so this testbench instantiates the same module at a different grid
// and requires the same keep-running / ordinal-correlation policy to still hold.
module host_stream_adapter_2006_tb;
    localparam integer IN_BITS = 1280;
    localparam integer OUT_BITS = 80;

    reg clk = 0, rst_n = 0, start = 0;
    reg [IN_BITS-1:0] input_words = 0;
    wire done, ap_start, core_rst_n, m_tvalid, s_tready;
    wire [IN_BITS-1:0] m_tdata;
    wire [OUT_BITS-1:0] output_words;
    wire [OUT_BITS-1:0] s_tdata;
    wire s_tvalid;
    reg [OUT_BITS-1:0] pipe_data [0:3];
    reg [3:0] pipe_valid = 0;
    integer lane;
    always #5 clk = ~clk;

    host_stream_adapter #(.INPUT_BITS(IN_BITS), .OUTPUT_BITS(OUT_BITS)) dut(
        .clk(clk), .rst_n(rst_n), .start(start), .input_words(input_words),
        .done(done), .output_words(output_words), .ap_start(ap_start),
        .core_rst_n(core_rst_n), .m_tdata(m_tdata), .m_tvalid(m_tvalid),
        .m_tready(1'b1), .s_tdata(s_tdata), .s_tvalid(s_tvalid),
        .s_tready(s_tready));

    // A stream core with one ordered output for every accepted input beat.
    assign s_tvalid = pipe_valid[3];
    assign s_tdata = pipe_data[3];
    always @(posedge clk) begin
        if (!rst_n) begin
            pipe_valid <= 0;
            for (lane = 0; lane < 4; lane = lane + 1) pipe_data[lane] <= 0;
        end else begin
            pipe_valid <= {pipe_valid[2:0], m_tvalid};
            pipe_data[0] <= m_tdata[OUT_BITS-1:0] ^ {OUT_BITS{1'b1}};
            for (lane = 1; lane < 4; lane = lane + 1)
                pipe_data[lane] <= pipe_data[lane-1];
        end
    end

    task automatic one_request(input [OUT_BITS-1:0] payload);
        integer waited;
        begin
            @(negedge clk); input_words = {{(IN_BITS-OUT_BITS){1'b0}}, payload}; start = 1;
            @(negedge clk); start = 0;
            waited = 0;
            while (!done && waited < 100) begin
                @(negedge clk);
                waited = waited + 1;
            end
            if (!done || output_words !== (payload ^ {OUT_BITS{1'b1}}))
                $fatal(1, "tagged response did not match this request");
            @(negedge clk);
            if (done) $fatal(1, "done was not a one-cycle pulse");
        end
    endtask

    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1;
        repeat (8) @(negedge clk);
        if (!core_rst_n || !ap_start || !m_tvalid || !s_tready)
            $fatal(1, "dataflow core did not remain active for dummy traffic");
        one_request(80'h0123456789abcdef0123);
        repeat (12) @(negedge clk);
        if (!core_rst_n || !ap_start || !m_tvalid)
            $fatal(1, "core was reset or stopped between frames");
        one_request(80'hfedcba9876543210fedc);
        $display("HOST_STREAM_ADAPTER_WIDE_PASS in_bits=%0d out_bits=%0d", IN_BITS, OUT_BITS);
        $finish;
    end
endmodule
