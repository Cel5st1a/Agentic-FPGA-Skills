`timescale 1ns/1ps
// Synthetic core with signed 20/24-bit elements in 32-bit physical slots.
module host_stream_padding_tb;
    reg clk=0;always #5 clk=~clk;
    reg rst_n=0,start=0;
    reg [119:0] input_words=0;
    wire done,ap_start,core_rst_n,m_valid,s_ready;
    wire [95:0] output_words;
    wire [63:0] m_data;
    reg [63:0] s_data=0;
    reg s_valid=0;
    reg pending=0;
    integer beat=0,out_beat=0,gap=0,tick=0,waited;
    reg signed [19:0] a,b,c,d;
    reg signed [23:0] result[0:3];
    wire m_ready=core_rst_n && !pending && (tick%5!=2);
    host_stream_adapter #(.INPUT_WORDS_BITS(120),.PORT_BITS(64),.INPUT_BEATS(3),
        .OUTPUT_BITS(96),.OUTPUT_PORT_BITS(64),.OUTPUT_BEATS(2),
        .INPUT_ELEMENT_BITS(20),.OUTPUT_ELEMENT_BITS(24),.INPUT_LANES(2),.OUTPUT_LANES(2),
        .INPUT_SLOT_BITS(32),.OUTPUT_SLOT_BITS(32)) dut(
        .clk(clk),.rst_n(rst_n),.start(start),.input_words(input_words),
        .done(done),.output_words(output_words),.ap_start(ap_start),.core_rst_n(core_rst_n),
        .m_tdata(m_data),.m_tvalid(m_valid),.m_tready(m_ready),
        .s_tdata(s_data),.s_tvalid(s_valid),.s_tready(s_ready));
    always @(posedge clk) begin
        if(!core_rst_n) begin
            beat<=0;pending<=0;s_valid<=0;out_beat<=0;gap<=0;tick<=0;
        end else begin
            tick<=tick+1;
            if(m_valid && m_ready) begin
                if(m_data[31:20]!=={12{m_data[19]}} || m_data[63:52]!=={12{m_data[51]}})
                    $fatal(1,"input signed slot padding wrong");
                if(beat==0) begin a<=$signed(m_data[19:0]);b<=$signed(m_data[51:32]);end
                if(beat==1) begin c<=$signed(m_data[19:0]);d<=$signed(m_data[51:32]);end
                if(beat==2) begin
                    result[0]<=a*3+b;result[1]<=c*3+d;
                    result[2]<=$signed(m_data[19:0])*3+$signed(m_data[51:32]);
                    result[3]<=a+b+c+d+$signed(m_data[19:0])+$signed(m_data[51:32]);
                    pending<=1;out_beat<=0;gap<=2;beat<=0;
                end else beat<=beat+1;
            end
            if(pending) begin
                if(s_valid && s_ready) begin
                    s_valid<=0;
                    if(out_beat==1) pending<=0;
                    else begin out_beat<=1;gap<=3;end
                end else if(!s_valid && gap>0) gap<=gap-1;
                else if(!s_valid) begin
                    // Poison high padding: the adapter must extract only real element bits.
                    s_data<={8'hd3,result[out_beat*2+1],8'h7e,result[out_beat*2]};s_valid<=1;
                end
            end
        end
    end
    task request;
        input [119:0] words;
        input [95:0] expected;
        begin
            @(negedge clk);input_words=words;start=1;
            @(negedge clk);start=0;waited=0;
            while(!done && waited<1000) begin @(negedge clk);waited=waited+1;end
            if(!done || output_words!==expected) $fatal(1,"packed result wrong %h",output_words);
            repeat(20) @(negedge clk);
        end
    endtask
    initial begin
        repeat(4) @(negedge clk);rst_n=1;
        request({20'd17,20'hffff3,20'd11,20'hffff9,20'd5,20'hffffd},
                96'h00000affffeafffff6fffffc);
        request({20'hffff3,20'd11,20'hffff9,20'd5,20'hffffd,20'd2},
                96'hfffffb000014000008000003);
        $display("HOST_STREAM_PADDING_PASS samples=2");$finish;
    end
endmodule
