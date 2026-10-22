`timescale 1ns/1ps
module host_stream_adapter_tb;
    reg clk = 0, rst_n = 0, start = 0;
    reg [2047:0] input_words = 0;
    wire done, ap_start, core_rst_n, m_tvalid, s_tready;
    wire [2047:0] m_tdata;
    wire [127:0] output_words;
    wire [127:0] s_tdata;
    wire s_tvalid;
    reg [127:0] pipe_data [0:3];
    reg [3:0] pipe_valid = 0;
    integer lane;
    always #5 clk = ~clk;

    host_stream_adapter dut(
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
            pipe_data[0] <= m_tdata[127:0] ^ 128'h1234;
            for (lane = 1; lane < 4; lane = lane + 1)
                pipe_data[lane] <= pipe_data[lane-1];
        end
    end

    task automatic one_request(input [127:0] payload);
        integer waited;
        begin
            @(negedge clk); input_words = payload; start = 1;
            @(negedge clk); start = 0;
            waited = 0;
            while (!done && waited < 100) begin
                @(negedge clk);
                waited = waited + 1;
            end
            if (!done || output_words !== (payload ^ 128'h1234))
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
        one_request(128'h00112233445566778899aabbccddeeff);
        repeat (12) @(negedge clk);
        if (!core_rst_n || !ap_start || !m_tvalid)
            $fatal(1, "core was reset or stopped between frames");
        one_request(128'hffeeddccbbaa99887766554433221100);
        $display("HOST_STREAM_ADAPTER_PASS");
        $finish;
    end
endmodule
