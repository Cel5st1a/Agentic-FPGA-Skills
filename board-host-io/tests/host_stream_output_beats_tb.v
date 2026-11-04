`timescale 1ns/1ps
module host_stream_output_beats_tb;
  localparam integer OUTPUT_BEATS=2, GAP_CYCLES=0, EARLY_OUTPUT=0, OUTPUT_LANES=1;
  reg clk=0, rst_n=0, start=0;
  reg [47:0] input_words=0;
  wire done, ap_start, core_rst_n, m_tvalid, s_tready;
  wire [15:0] m_tdata;
  wire [OUTPUT_BEATS*OUTPUT_LANES*16-1:0] output_words;
  reg m_tready=0;
  reg [OUTPUT_LANES*16-1:0] s_tdata=0;
  reg [15:0] sum=0;
  reg s_tvalid=0, reply_busy=0;
  integer out_pos=0, gap_left=0, lane;
  integer cycle=0, pos=0, replies=0, nonzero=0, waits;
  reg stalled=0;
  reg [15:0] stalled_data=0;
  always #5 clk=~clk;
  host_stream_adapter #(.INPUT_WORDS_BITS(48), .PORT_BITS(16), .INPUT_BEATS(3),
    .OUTPUT_BITS(OUTPUT_BEATS*OUTPUT_LANES*16), .OUTPUT_PORT_BITS(OUTPUT_LANES*16),
    .OUTPUT_BEATS(OUTPUT_BEATS)) dut(
    .clk(clk), .rst_n(rst_n), .start(start), .input_words(input_words),
    .done(done), .output_words(output_words), .ap_start(ap_start),
    .core_rst_n(core_rst_n), .m_tdata(m_tdata), .m_tvalid(m_tvalid),
    .m_tready(m_tready), .s_tdata(s_tdata), .s_tvalid(s_tvalid), .s_tready(s_tready));
  always @(negedge clk) begin
    cycle=cycle+1;
    m_tready=(cycle%5!=0 && cycle%5!=1 && !reply_busy);
  end
  always @(posedge clk) begin
    if (!rst_n) begin
      pos<=0; sum<=0; s_tvalid<=0; reply_busy<=0; out_pos<=0; gap_left<=0; stalled<=0;
    end
    else begin
      if(stalled && (!m_tvalid || m_tdata!==stalled_data))
        $fatal(1,"TDATA/TVALID changed while stalled");
      stalled<=m_tvalid && !m_tready; stalled_data<=m_tdata;
      if(s_tvalid && s_tready) begin
        if(out_pos==OUTPUT_BEATS-1) begin s_tvalid<=0; reply_busy<=0; end
        else begin
          for(lane=0; lane<OUTPUT_LANES; lane=lane+1)
            s_tdata[lane*16 +: 16]<=s_tdata[lane*16 +: 16]+16'd1;
          out_pos<=out_pos+1;
          if(GAP_CYCLES!=0) begin s_tvalid<=0; gap_left<=GAP_CYCLES; end
        end
      end else if(reply_busy && !s_tvalid && gap_left>0) begin
        gap_left<=gap_left-1;
        if(gap_left==1) s_tvalid<=1;
      end
      if(m_tvalid && m_tready) begin
        if(m_tdata!=0) begin
          if(m_tdata !== input_words[pos*16 +: 16])
            $fatal(1,"sample split or reordered pos=%0d data=%h",pos,m_tdata);
          nonzero<=nonzero+1;
        end
        if(EARLY_OUTPUT && pos==0) begin
          for(lane=0; lane<OUTPUT_LANES; lane=lane+1)
            s_tdata[lane*16 +: 16]<=m_tdata+lane*256;
          s_tvalid<=1; reply_busy<=1; out_pos<=0;
        end
        if(pos==2) begin
          if(!EARLY_OUTPUT) begin
            for(lane=0; lane<OUTPUT_LANES; lane=lane+1)
              s_tdata[lane*16 +: 16]<=sum+m_tdata+lane*256;
            s_tvalid<=1; reply_busy<=1; out_pos<=0;
          end
          sum<=0; pos<=0;
        end else begin sum<=sum+m_tdata; pos<=pos+1; end
      end
      if(done) replies<=replies+1;
    end
  end
  task request(input [47:0] payload);
    reg [15:0] expected;
    integer output_index, output_lane;
    begin
      expected=EARLY_OUTPUT ? payload[15:0] : payload[15:0]+payload[31:16]+payload[47:32];
      @(negedge clk); input_words=payload; start=1;
      @(negedge clk); start=0; waits=0;
      while(!done && waits<800) begin @(negedge clk); waits=waits+1; end
      if(!done || pos!=0) $fatal(1,"incomplete input or missing done");
      for(output_index=0; output_index<OUTPUT_BEATS; output_index=output_index+1)
        for(output_lane=0; output_lane<OUTPUT_LANES; output_lane=output_lane+1)
          if(output_words[(output_index*OUTPUT_LANES+output_lane)*16 +: 16]
              !== expected+output_index+output_lane*256)
            $fatal(1,"missing/reordered output beat %0d lane %0d",output_index,output_lane);
      @(negedge clk); if(done) $fatal(1,"duplicate done");
    end
  endtask
  initial begin
    repeat(3) @(negedge clk); rst_n=1;
    repeat(7) @(negedge clk); request(48'h003300220011);
    repeat(11) @(negedge clk); request(48'h006600550044);
    repeat(10) @(negedge clk);
    if(replies!=2 || nonzero!=6) $fatal(1,"duplicate/missing sample");
    $display("HOST_STREAM_OUTPUT_BEATS_PASS"); $finish;
  end
  initial begin #100000; $fatal(1,"timeout"); end
endmodule
