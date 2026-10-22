`timescale 1ns/1ps
module host_stream_multibeat_tb;
  reg clk=0, rst_n=0, start=0;
  reg [47:0] input_words=0;
  wire done, ap_start, core_rst_n, m_tvalid, s_tready;
  wire [15:0] m_tdata, output_words;
  reg m_tready=0;
  reg [15:0] s_tdata=0, sum=0;
  reg s_tvalid=0;
  integer cycle=0, pos=0, replies=0, nonzero=0, waits;
  reg stalled=0;
  reg [15:0] stalled_data=0;
  always #5 clk=~clk;
  host_stream_adapter #(.INPUT_WORDS_BITS(48), .PORT_BITS(16),
    .INPUT_BEATS(3), .OUTPUT_BITS(16)) dut(
    .clk(clk), .rst_n(rst_n), .start(start), .input_words(input_words),
    .done(done), .output_words(output_words), .ap_start(ap_start),
    .core_rst_n(core_rst_n), .m_tdata(m_tdata), .m_tvalid(m_tvalid),
    .m_tready(m_tready), .s_tdata(s_tdata), .s_tvalid(s_tvalid), .s_tready(s_tready));
  always @(negedge clk) begin
    cycle=cycle+1;
    m_tready=(cycle%5!=0 && cycle%5!=1);
  end
  always @(posedge clk) begin
    if (!rst_n) begin pos<=0; sum<=0; s_tvalid<=0; stalled<=0; end
    else begin
      if(stalled && (!m_tvalid || m_tdata!==stalled_data))
        $fatal(1,"TDATA/TVALID changed while stalled");
      stalled<=m_tvalid && !m_tready; stalled_data<=m_tdata;
      s_tvalid<=0;
      if(m_tvalid && m_tready) begin
        if(m_tdata!=0) begin
          if(m_tdata !== input_words[pos*16 +: 16])
            $fatal(1,"sample split or reordered pos=%0d data=%h",pos,m_tdata);
          nonzero<=nonzero+1;
        end
        if(pos==2) begin s_tdata<=sum+m_tdata; s_tvalid<=1; sum<=0; pos<=0; end
        else begin sum<=sum+m_tdata; pos<=pos+1; end
      end
      if(done) replies<=replies+1;
    end
  end
  task request(input [47:0] payload);
    begin
      @(negedge clk); input_words=payload; start=1;
      @(negedge clk); start=0; waits=0;
      while(!done && waits<250) begin @(negedge clk); waits=waits+1; end
      if(!done || output_words!==payload[15:0]+payload[31:16]+payload[47:32])
        $fatal(1,"missing or wrong response %h",output_words);
      @(negedge clk); if(done) $fatal(1,"duplicate done");
    end
  endtask
  initial begin
    repeat(3) @(negedge clk); rst_n=1;
    repeat(7) @(negedge clk); request(48'h003300220011);
    repeat(11) @(negedge clk); request(48'h006600550044);
    repeat(10) @(negedge clk);
    if(replies!=2 || nonzero!=6) $fatal(1,"duplicate/missing sample");
    $display("HOST_STREAM_MULTIBEAT_PASS"); $finish;
  end
  initial begin #100000; $fatal(1,"timeout"); end
endmodule
