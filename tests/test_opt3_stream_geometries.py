import json

import pytest
from test_s15_numeric_policy import ROOT, load

BENCH = r"""
`timescale 1ns/1ps
module opt3_stream_tb;
localparam E=@E@, S=@S@, IB=@IB@, IL=@IL@, OB=@OB@, OL=@OL@;
reg clk=0,rst_n=0,start=0,ready=0,valid=0;
reg [IB*IL*E-1:0] payload=0;
wire done,av,cr,iv,orr;
wire [IL*S-1:0] din;
wire [OB*OL*E-1:0] result;
reg [OL*S-1:0] dout=0;
reg [E-1:0] total=0, next_total;
reg busy=0;
integer cycle=0,ipos=0,opos=0,gap=0,lane,waits,n,k,seen=0;
reg [E-1:0] expect_word, expected;
reg stalled=0; reg [IL*S-1:0] held;
always #5 clk=~clk;
host_stream_adapter #(.INPUT_WORDS_BITS(IB*IL*E),.PORT_BITS(IL*S),
 .INPUT_BEATS(IB),.INPUT_LANES(IL),.INPUT_ELEMENT_BITS(E),.INPUT_SLOT_BITS(S),
 .OUTPUT_BITS(OB*OL*E),.OUTPUT_PORT_BITS(OL*S),.OUTPUT_BEATS(OB),
 .OUTPUT_LANES(OL),.OUTPUT_ELEMENT_BITS(E),.OUTPUT_SLOT_BITS(S)) dut(
 .clk(clk),.rst_n(rst_n),.start(start),.input_words(payload),.done(done),
 .output_words(result),.ap_start(av),.core_rst_n(cr),.m_tdata(din),
 .m_tvalid(iv),.m_tready(ready),.s_tdata(dout),.s_tvalid(valid),.s_tready(orr));
always @(negedge clk) begin
 cycle=cycle+1; ready=(cycle%7>2 && !busy);
end
always @(posedge clk) begin
 if(!rst_n) begin ipos<=0;opos<=0;total<=0;busy<=0;valid<=0;stalled<=0; end
 else begin
  if(stalled && (!iv || din!==held)) $fatal(1,"input changed under backpressure");
  stalled<=iv&&!ready;held<=din;
  if(valid&&orr) begin
   if(opos==OB-1) begin valid<=0;busy<=0; end
   else begin
    opos<=opos+1;valid<=0;gap<=2;
    for(lane=0;lane<OL;lane=lane+1)
     dout[lane*S+:S]<=dout[lane*S+:S]+1;
   end
  end else if(busy&&!valid&&gap>0) begin
   gap<=gap-1;if(gap==1)valid<=1;
  end
  if(iv&&ready) begin
   next_total=total;
   for(lane=0;lane<IL;lane=lane+1) begin
    expect_word=payload[(ipos*IL+lane)*E+:E];
    if(din[lane*S+:E]!=0) begin
     if(din[lane*S+:S]!=={{(S-E){expect_word[E-1]}},expect_word})
      $fatal(1,"input lane/padding mismatch");
     seen=seen+1;
    end
    next_total=next_total+din[lane*S+:E];
   end
   if(ipos==IB-1) begin
    ipos<=0;total<=0;busy<=1;valid<=1;opos<=0;
    for(lane=0;lane<OL;lane=lane+1)
     dout[lane*S+:S]<=next_total+lane*16;
   end else begin total<=next_total;ipos<=ipos+1;end
  end
 end
end
initial begin
 repeat(3)@(negedge clk);rst_n=1;
 for(n=0;n<2;n=n+1)begin
  repeat(11)@(negedge clk);expected=0;
  for(k=0;k<IB*IL;k=k+1) begin
   expect_word=k+n+3;if(k%2)expect_word=-expect_word;
   payload[k*E+:E]=expect_word;expected=expected+expect_word;
  end
  start=1;@(negedge clk);start=0;waits=0;
  while(!done&&waits<2000)begin @(negedge clk);waits=waits+1;end
  if(!done)$fatal(1,"missing done");
  for(k=0;k<OB;k=k+1)for(lane=0;lane<OL;lane=lane+1)begin
   expect_word=expected+k+lane*16;
   if(result[(k*OL+lane)*E+:E]!==expect_word)$fatal(1,"output reorder/padding mismatch");
  end
  @(negedge clk);if(done)$fatal(1,"duplicate done");
 end
 if(seen!=2*IB*IL)$fatal(1,"missing or duplicated nonzero input");
 $display("OPT3_STREAM_PASS");$finish;
end
initial begin #500000;$fatal(1,"timeout");end
endmodule
"""


@pytest.mark.parametrize(
    "e,s,ib,il,ob,ol", [(20, 24, 3, 2, 5, 3), (22, 24, 4, 3, 2, 2), (24, 24, 2, 1, 1, 4)]
)
def test_mixed_geometries_signed_padding_stalls_and_repeated_samples(
    tmp_path, e, s, ib, il, ob, ol
):
    runner = load("board-host-io/scripts/run_rtl_tb.py", "opt3_stream_runner")
    source = BENCH
    for key, value in dict(E=e, S=s, IB=ib, IL=il, OB=ob, OL=ol).items():
        source = source.replace("@" + key + "@", str(value))
    bench = tmp_path / "opt3_stream_tb.v"
    bench.write_text(source)
    result = runner.run_testbench(
        "opt3_stream_tb", [ROOT / "board-host-io/assets/host_stream_adapter.v", bench], tmp_path
    )
    (tmp_path / "result.json").write_text(json.dumps(result, indent=2))
    assert result["returncode"] == 0, result["stdout"]
    assert result["marker"] == "OPT3_STREAM_PASS"
