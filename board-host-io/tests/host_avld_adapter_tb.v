`timescale 1ns/1ps
// The ap_vld host adapter (D-M01): the transport presents a finished packed word and a
// one-cycle start pulse; the core needs a latched input and a start level held to ap_done.
//
// Four instances share one stimulus so the testbench proves both directions:
//   A  the adapter at the verified 64x4 geometry      -- must complete every transaction;
//   B  the adapter at a synthetic ap_fixed<20,6> one   -- must be width-generic;
//   C  the *broken* wiring D-M01 describes             -- must demonstrably misbehave,
//      otherwise A and B would not be measuring anything;
//   D  the adapter whose core never returns to idle    -- must fail closed.
//
// `ap_ctrl_hs_stub` is a stub core, not the real HLS core: it exists to make the adapter's
// contract observable. It reproduces the one measured property that matters here -- this
// core treats ap_start as a level, so releasing it before ap_done leaves the core idle with
// a valid input and no ap_done (measured in the ROM board top: ap_done low for 5M cycles
// after a 12-cycle pulse). The real core is covered by `simulate_host.py`.
module ap_ctrl_hs_stub #(
    parameter integer IN_BITS = 2048,
    parameter integer OUT_BITS = 128,
    parameter integer LATENCY = 12,
    parameter integer COOLDOWN = 4,
    // When 1, ap_idle never comes back after the transaction: the adapter must then refuse
    // a new request rather than start one on a core that is not ready.
    parameter integer STUCK_IDLE = 0,
    // When 1, ap_done is left asserted after the first transaction (a sticky level rather
    // than a completion pulse). The adapter must not treat that stale level as the finish of
    // a request it never started.
    parameter integer STICKY_DONE = 0
)(
    input wire ap_clk,
    input wire ap_rst,
    input wire [IN_BITS-1:0] ae_input,
    input wire ae_input_ap_vld,
    input wire ap_start,
    output reg ap_done,
    output reg ap_idle,
    output wire [OUT_BITS-1:0] result
);
    localparam [1:0] IDLE = 2'd0, RUN = 2'd1, COOL = 2'd2, STUCK = 2'd3;
    reg [1:0] state;
    reg [15:0] count;
    reg [IN_BITS-1:0] captured;

    assign result = captured[OUT_BITS-1:0] ^ {OUT_BITS{1'b1}};

    always @(posedge ap_clk or posedge ap_rst) begin
        if (ap_rst) begin
            state <= IDLE; count <= 0; captured <= 0;
            ap_done <= 1'b0; ap_idle <= 1'b1;
        end else begin
            if (!STICKY_DONE) ap_done <= 1'b0;
            case (state)
                IDLE: begin
                    ap_idle <= 1'b1;
                    if (ap_start && ae_input_ap_vld) begin
                        captured <= ae_input;
                        count <= 0;
                        ap_idle <= 1'b0;
                        state <= RUN;
                    end
                end
                RUN: begin
                    if (!ap_start) begin
                        // A one-cycle start pulse lands here: the core goes idle with the
                        // input still valid and never reports done.
                        ap_idle <= 1'b1;
                        state <= IDLE;
                    end else begin
                        count <= count + 16'd1;
                        if (count == LATENCY - 1) begin
                            ap_done <= 1'b1;
                            count <= 0;
                            state <= COOL;
                        end
                    end
                end
                COOL: begin
                    count <= count + 16'd1;
                    if (count == COOLDOWN - 1) begin
                        if (STUCK_IDLE) begin
                            ap_idle <= 1'b0;
                            state <= STUCK;
                        end else begin
                            ap_idle <= 1'b1;
                            state <= IDLE;
                        end
                    end
                end
                STUCK: begin
                    ap_idle <= 1'b0;
                    ap_done <= 1'b0;
                end
                default: state <= IDLE;
            endcase
        end
    end
endmodule

module host_avld_adapter_tb;
    localparam [2047:0] WORD_ONE =
        2048'h111122223333444455556666777788889999aaaabbbbccccddddeeeeffff0000;
    localparam [2047:0] WORD_TWO =
        2048'hffffffffffffffffffffffffffffffff00000000000000000000000000000000;
    localparam [2047:0] WORD_STUCK =
        2048'hdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef;
    localparam [2047:0] WORD_LATER =
        2048'h0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;

    reg clk = 0, rst_n = 0;
    always #5 clk = ~clk;

    reg start = 0;
    reg [2047:0] input_words = 0;
    reg b_start = 0;
    reg [1279:0] input_words_narrow = 0;

    // ---- A: adapter at the verified geometry ----
    wire a_done, a_start, a_vld, a_ap_done, a_ap_idle;
    wire [2047:0] a_core_input;
    wire [127:0] a_output_words, a_core_output;
    host_avld_adapter #(.INPUT_WORDS_BITS(2048), .CORE_INPUT_BITS(2048),
                        .OUTPUT_WORDS_BITS(128), .CORE_OUTPUT_BITS(128)) adapter_a (
        .clk(clk), .rst_n(rst_n), .start(start), .input_words(input_words),
        .done(a_done), .output_words(a_output_words), .core_input(a_core_input),
        .core_input_vld(a_vld), .ap_start(a_start), .ap_done(a_ap_done),
        .ap_idle(a_ap_idle), .core_output(a_core_output));
    ap_ctrl_hs_stub #(.IN_BITS(2048), .OUT_BITS(128), .LATENCY(12), .COOLDOWN(4)) core_a (
        .ap_clk(clk), .ap_rst(~rst_n), .ae_input(a_core_input), .ae_input_ap_vld(a_vld),
        .ap_start(a_start), .ap_done(a_ap_done), .ap_idle(a_ap_idle), .result(a_core_output));

    // ---- B: adapter at a synthetic ap_fixed<20,6> geometry ----
    wire b_done, b_start_out, b_vld, b_ap_done, b_ap_idle;
    wire [1279:0] b_core_input;
    wire [79:0] b_output_words, b_core_output;
    host_avld_adapter #(.INPUT_WORDS_BITS(1280), .CORE_INPUT_BITS(1280),
                        .OUTPUT_WORDS_BITS(80), .CORE_OUTPUT_BITS(80)) adapter_b (
        .clk(clk), .rst_n(rst_n), .start(b_start), .input_words(input_words_narrow),
        .done(b_done), .output_words(b_output_words), .core_input(b_core_input),
        .core_input_vld(b_vld), .ap_start(b_start_out), .ap_done(b_ap_done),
        .ap_idle(b_ap_idle), .core_output(b_core_output));
    ap_ctrl_hs_stub #(.IN_BITS(1280), .OUT_BITS(80), .LATENCY(12), .COOLDOWN(4)) core_b (
        .ap_clk(clk), .ap_rst(~rst_n), .ae_input(b_core_input), .ae_input_ap_vld(b_vld),
        .ap_start(b_start_out), .ap_done(b_ap_done), .ap_idle(b_ap_idle),
        .result(b_core_output));

    // ---- C: the broken wiring D-M01 replaces, on its own stimulus ----
    reg c_start = 0;
    reg [2047:0] c_input_words = 0;
    wire c_ap_done, c_ap_idle;
    ap_ctrl_hs_stub #(.IN_BITS(2048), .OUT_BITS(128), .LATENCY(12), .COOLDOWN(4)) core_c (
        .ap_clk(clk), .ap_rst(~rst_n),
        .ae_input(c_input_words), .ae_input_ap_vld(1'b1), .ap_start(c_start),
        .ap_done(c_ap_done), .ap_idle(c_ap_idle), .result());

    // ---- D: adapter whose core never returns to idle ----
    reg d_start = 0;
    reg [2047:0] d_input_words = 0;
    wire d_done, d_ap_start, d_vld, d_ap_done, d_ap_idle;
    wire [2047:0] d_core_input;
    wire [127:0] d_output_words, d_core_output;
    host_avld_adapter #(.INPUT_WORDS_BITS(2048), .CORE_INPUT_BITS(2048),
                        .OUTPUT_WORDS_BITS(128), .CORE_OUTPUT_BITS(128)) adapter_d (
        .clk(clk), .rst_n(rst_n), .start(d_start), .input_words(d_input_words),
        .done(d_done), .output_words(d_output_words), .core_input(d_core_input),
        .core_input_vld(d_vld), .ap_start(d_ap_start), .ap_done(d_ap_done),
        .ap_idle(d_ap_idle), .core_output(d_core_output));
    ap_ctrl_hs_stub #(.IN_BITS(2048), .OUT_BITS(128), .LATENCY(12), .COOLDOWN(4),
                      .STUCK_IDLE(1)) core_d (
        .ap_clk(clk), .ap_rst(~rst_n), .ae_input(d_core_input), .ae_input_ap_vld(d_vld),
        .ap_start(d_ap_start), .ap_done(d_ap_done), .ap_idle(d_ap_idle),
        .result(d_core_output));

    // ---- E: a core whose ap_done is a sticky level, not a completion pulse ----
    // The adapter must not mistake a stale ap_done for the end of a request it never started.
    reg e_start = 0;
    reg [2047:0] e_input_words = 0;
    wire e_done, e_ap_start, e_vld, e_ap_done, e_ap_idle;
    wire [2047:0] e_core_input;
    wire [127:0] e_output_words, e_core_output;
    host_avld_adapter #(.INPUT_WORDS_BITS(2048), .CORE_INPUT_BITS(2048),
                        .OUTPUT_WORDS_BITS(128), .CORE_OUTPUT_BITS(128)) adapter_e (
        .clk(clk), .rst_n(rst_n), .start(e_start), .input_words(e_input_words),
        .done(e_done), .output_words(e_output_words), .core_input(e_core_input),
        .core_input_vld(e_vld), .ap_start(e_ap_start), .ap_done(e_ap_done),
        .ap_idle(e_ap_idle), .core_output(e_core_output));
    ap_ctrl_hs_stub #(.IN_BITS(2048), .OUT_BITS(128), .LATENCY(12), .COOLDOWN(4),
                      .STICKY_DONE(1)) core_e (
        .ap_clk(clk), .ap_rst(~rst_n), .ae_input(e_core_input), .ae_input_ap_vld(e_vld),
        .ap_start(e_ap_start), .ap_done(e_ap_done), .ap_idle(e_ap_idle),
        .result(e_core_output));

    // ---- F: a core that stays busy long after ap_done, so a request arrives mid-release ----
    reg f_start = 0;
    reg [2047:0] f_input_words = 0;
    wire f_done, f_ap_start, f_vld, f_ap_done, f_ap_idle;
    wire [2047:0] f_core_input;
    wire [127:0] f_output_words, f_core_output;
    host_avld_adapter #(.INPUT_WORDS_BITS(2048), .CORE_INPUT_BITS(2048),
                        .OUTPUT_WORDS_BITS(128), .CORE_OUTPUT_BITS(128)) adapter_f (
        .clk(clk), .rst_n(rst_n), .start(f_start), .input_words(f_input_words),
        .done(f_done), .output_words(f_output_words), .core_input(f_core_input),
        .core_input_vld(f_vld), .ap_start(f_ap_start), .ap_done(f_ap_done),
        .ap_idle(f_ap_idle), .core_output(f_core_output));
    ap_ctrl_hs_stub #(.IN_BITS(2048), .OUT_BITS(128), .LATENCY(12), .COOLDOWN(40)) core_f (
        .ap_clk(clk), .ap_rst(~rst_n), .ae_input(f_core_input), .ae_input_ap_vld(f_vld),
        .ap_start(f_ap_start), .ap_done(f_ap_done), .ap_idle(f_ap_idle),
        .result(f_core_output));

    // ---- bookkeeping ----
    // For C the observable is the stub's own ap_done: the broken wiring never produces one.
    integer a_done_rises = 0, b_done_rises = 0, c_ap_done_rises = 0;
    integer e_done_rises = 0, f_done_rises = 0;
    reg a_done_q = 0, b_done_q = 0, c_ap_done_q = 0, e_done_q = 0, f_done_q = 0;
    always @(posedge clk) begin
        if (rst_n) begin
            if (a_done && !a_done_q) a_done_rises = a_done_rises + 1;
            if (b_done && !b_done_q) b_done_rises = b_done_rises + 1;
            if (c_ap_done && !c_ap_done_q) c_ap_done_rises = c_ap_done_rises + 1;
            if (e_done && !e_done_q) e_done_rises = e_done_rises + 1;
            if (f_done && !f_done_q) f_done_rises = f_done_rises + 1;
        end
        a_done_q <= a_done; b_done_q <= b_done; c_ap_done_q <= c_ap_done;
        e_done_q <= e_done; f_done_q <= f_done;
    end

    // Counts how often the adapter served a request that arrived while the previous
    // transaction was still unwinding. This is the S_RELEASE -> S_RUN path, and the
    // counter is what proves the scenario below actually reaches it.
    integer f_held_served = 0;
    reg [1:0] f_state_q = 0;
    always @(posedge clk) begin
        if (rst_n) begin
            if (f_state_q == 2'd2 && adapter_f.state == 2'd1 && f_vld) f_held_served = f_held_served + 1;
            f_state_q <= adapter_f.state;
        end
    end

    // While the adapter is driving a transaction (`core_input_vld` high) ap_start must stay
    // high: that is precisely the measured failure mode a one-cycle pulse produces.
    integer a_start_drops = 0, b_start_drops = 0;
    always @(posedge clk) if (rst_n) begin
        if (a_vld && !a_start) a_start_drops = a_start_drops + 1;
        if (b_vld && !b_start_out) b_start_drops = b_start_drops + 1;
    end

    // The latched input must not follow the transport's register while the core is running.
    // The window is measured from the adapter's own `core_input_vld`, not from the request
    // pulse: a request that arrives while the core is still unwinding is served later, and
    // sampling the baseline at the pulse would mistake that legitimate latch for a leak.
    integer a_input_leaks = 0;
    reg [2047:0] a_input_at_latch = 0;
    reg tracking_a = 0;
    reg a_vld_q = 0;
    always @(posedge clk) if (rst_n) begin
        if (a_vld && !a_vld_q) begin
            a_input_at_latch = a_core_input;
            tracking_a = 1;
        end
        if (!a_vld && a_vld_q) tracking_a = 0;
        if (tracking_a && a_core_input !== a_input_at_latch)
            a_input_leaks = a_input_leaks + 1;
        a_vld_q = a_vld;
    end

    integer waited;
    reg [1279:0] word_narrow;

    task automatic run_a;
        input [2047:0] payload;
        begin
            input_words = payload;
            @(negedge clk);
            start = 1;
            @(negedge clk);
            start = 0;
            waited = 0;
            while (!a_done && waited < 200) begin
                @(negedge clk);
                // The transport keeps writing its own register during the transaction;
                // none of that may reach the core.
                input_words = ~input_words;
                waited = waited + 1;
            end
            if (!a_done) $fatal(1, "adapter A never reported done");
            if (a_output_words !== (payload[127:0] ^ {128{1'b1}}))
                $fatal(1, "adapter A returned the wrong result: %032x", a_output_words);
            if (a_core_input !== payload)
                $fatal(1, "adapter A did not present the whole packed word to the core");
            @(negedge clk);
            if (a_done) $fatal(1, "adapter A held done past one cycle");
            input_words = payload;
            repeat (20) @(negedge clk);
        end
    endtask

    initial begin
        repeat (4) @(negedge clk);
        rst_n = 1;
        repeat (8) @(negedge clk);

        // C must be demonstrably broken, or A and B prove nothing: the one-cycle start
        // pulse this transport emits never produces ap_done when wired straight through.
        c_input_words = WORD_ONE;
        @(negedge clk);
        c_start = 1;
        @(negedge clk);
        c_start = 0;
        repeat (400) @(negedge clk);
        if (c_ap_done_rises != 0)
            $fatal(1, "the broken wiring D-M01 describes unexpectedly completed");
        if (c_ap_idle !== 1'b1)
            $fatal(1, "the broken wiring left the stub core busy instead of idle");

        // A must complete two transactions with a one-cycle start each.
        run_a(WORD_ONE);
        run_a(WORD_TWO);
        if (a_done_rises != 2)
            $fatal(1, "adapter A produced %0d done pulses for 2 frames", a_done_rises);
        if (a_start_drops != 0)
            $fatal(1, "adapter A dropped ap_start %0d cycles before ap_done", a_start_drops);
        if (a_input_leaks != 0)
            $fatal(1, "adapter A let the transport's register reach the core %0d times",
                   a_input_leaks);

        // B must be width-generic at the synthetic ap_fixed<20,6> geometry.
        word_narrow = 1280'h0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef;
        input_words_narrow = word_narrow;
        @(negedge clk);
        b_start = 1;
        @(negedge clk);
        b_start = 0;
        waited = 0;
        while (!b_done && waited < 200) begin
            @(negedge clk);
            waited = waited + 1;
        end
        if (!b_done) $fatal(1, "adapter B never reported done at the 1280/80 geometry");
        if (b_output_words !== (word_narrow[79:0] ^ {80{1'b1}}))
            $fatal(1, "adapter B returned the wrong result at 1280/80: %020x", b_output_words);
        if (b_core_input !== word_narrow)
            $fatal(1, "adapter B did not latch the packed word at 1280 bits");
        // The wait loop exits on a negedge that sees `done`; the rise counters are clocked,
        // so give them the next posedge before reading them.
        repeat (4) @(negedge clk);
        if (b_start_drops != 0)
            $fatal(1, "adapter B dropped ap_start %0d cycles before ap_done", b_start_drops);
        if (b_done_rises != 1)
            $fatal(1, "adapter B produced %0d done pulses for 1 frame", b_done_rises);

        // D must fail closed: once the core stops returning to idle, a new request is
        // refused rather than started on a core that is not ready.
        d_input_words = WORD_STUCK;
        @(negedge clk);
        d_start = 1;
        @(negedge clk);
        d_start = 0;
        waited = 0;
        while (!d_done && waited < 200) begin
            @(negedge clk);
            waited = waited + 1;
        end
        if (!d_done) $fatal(1, "adapter D never reported its first done");
        repeat (20) @(negedge clk);
        if (d_ap_start !== 1'b0)
            $fatal(1, "adapter D re-asserted ap_start on a core that is not idle");
        if (d_vld !== 1'b0)
            $fatal(1, "adapter D presented a new input to a core that is not idle");
        d_input_words = WORD_LATER;
        @(negedge clk);
        d_start = 1;
        @(negedge clk);
        d_start = 0;
        repeat (200) @(negedge clk);
        if (d_core_input !== WORD_STUCK)
            $fatal(1, "adapter D accepted a new request on a core that is not idle");

        // E: a sticky ap_done must not be mistaken for the completion of a new request.
        // One transaction runs; afterwards the core holds ap_done high forever while ap_idle
        // is high. A second request must be refused, not answered with the stale result.
        e_input_words = WORD_ONE;
        @(negedge clk);
        e_start = 1;
        @(negedge clk);
        e_start = 0;
        waited = 0;
        while (!e_done && waited < 200) begin
            @(negedge clk);
            waited = waited + 1;
        end
        if (!e_done) $fatal(1, "adapter E never reported its first done");
        if (e_output_words !== (WORD_ONE[127:0] ^ {128{1'b1}}))
            $fatal(1, "adapter E returned the wrong first result");
        repeat (30) @(negedge clk);
        if (e_ap_done !== 1'b1) $fatal(1, "the sticky-ap_done stub did not hold ap_done");
        if (e_ap_idle !== 1'b1) $fatal(1, "the sticky-ap_done stub did not return to idle");
        e_input_words = WORD_LATER;
        @(negedge clk);
        e_start = 1;
        @(negedge clk);
        e_start = 0;
        repeat (120) @(negedge clk);
        if (e_done_rises != 1)
            $fatal(1, "adapter E answered a new request with the stale ap_done (%0d dones)",
                   e_done_rises);
        if (e_core_input !== WORD_ONE)
            $fatal(1, "adapter E presented a new input while ap_done was already high");

        // F: a request arriving while the core is still unwinding must be held and served,
        // not dropped. COOLDOWN=40 keeps ap_idle low across the second pulse.
        f_input_words = WORD_ONE;
        @(negedge clk);
        f_start = 1;
        @(negedge clk);
        f_start = 0;
        waited = 0;
        while (!f_done && waited < 200) begin
            @(negedge clk);
            waited = waited + 1;
        end
        if (!f_done) $fatal(1, "adapter F never reported its first done");
        if (f_ap_idle !== 1'b0)
            $fatal(1, "adapter F's core was not still busy; the held path would not be reached");
        f_input_words = WORD_TWO;
        @(negedge clk);
        f_start = 1;
        @(negedge clk);
        f_start = 0;
        waited = 0;
        while (!f_done && waited < 400) begin
            @(negedge clk);
            waited = waited + 1;
        end
        if (!f_done) $fatal(1, "adapter F never served the request it was holding");
        if (f_output_words !== (WORD_TWO[127:0] ^ {128{1'b1}}))
            $fatal(1, "adapter F served the wrong payload from its held request");
        if (f_held_served == 0)
            $fatal(1, "adapter F never took the S_RELEASE -> S_RUN path; the scenario is vacuous");
        repeat (80) @(negedge clk);
        if (f_done_rises != 2)
            $fatal(1, "adapter F produced %0d done pulses for 2 frames", f_done_rises);

        $display("HOST_AVLD_ADAPTER_PASS a_frames=2 a_start_drops=%0d b_frames=1 broken_done=%0d held_served=%0d sticky_dones=%0d",
                 a_start_drops, c_ap_done_rises, f_held_served, e_done_rises);
        $finish;
    end
endmodule
