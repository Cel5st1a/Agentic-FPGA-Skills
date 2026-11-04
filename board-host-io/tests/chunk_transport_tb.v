`timescale 1ns/1ps
// Synthetic byte-interface fixture only. It is not a deployed model or a board test.
module chunk_transport_tb;
    localparam IN_BYTES=66000, OUT_BYTES=66003, CHUNK=1024;
    localparam PREFIX_ACKS=0;
    localparam N_IN=(IN_BYTES+CHUNK-1)/CHUNK, N_OUT=(OUT_BYTES+CHUNK-1)/CHUNK;
    localparam REQUEST_BYTES=IN_BYTES+16*N_IN;
    reg clk=0; always #5 clk=~clk;
    reg rst_n=0, rx_valid=0, tx_ready=1, done=0;
    reg [7:0] rx_data=0;
    wire [7:0] tx_data;
    wire tx_valid,start;
    wire [IN_BYTES*8-1:0] input_words;
    reg [OUT_BYTES*8-1:0] output_words=0;
    reg [7:0] request[0:REQUEST_BYTES-1];
    reg [7:0] negative_prefix[0:2*(CHUNK+16)-1];
    reg [IN_BYTES*8-1:0] expected[0:0];
    integer i,j,index=0,size,nbytes,busy=0,starts=0,wait_done=0;
    integer frame_index=0,length_low=0,frame_length=0,acks=0,results=0,cycles=0,logfile;
    cnn_uart_chunk_transport #(.CLK_HZ(100000000),.RX_TIMEOUT_CYCLES(40),
        .PAYLOAD_BYTES(IN_BYTES),.RESPONSE_BYTES(OUT_BYTES),.CHUNK_BYTES(CHUNK),
        .INPUT_BITS(IN_BYTES*8),.OUTPUT_BITS(OUT_BYTES*8)) dut(
        .clk(clk),.rst_n(rst_n),.uart_rx_data(rx_data),.uart_rx_valid(rx_valid),
        .uart_tx_data(tx_data),.uart_tx_valid(tx_valid),.uart_tx_ready(tx_ready),
        .input_words(input_words),.start(start),.done(done),.output_words(output_words));
    task send_byte;
        input [7:0] b;
        begin
            @(negedge clk);rx_data=b;rx_valid=1;
            @(negedge clk);rx_valid=0;
        end
    endtask
    always @(posedge clk) if(rst_n) begin
        cycles=cycles+1;
        if(cycles>1500000) $fatal(1,"chunk protocol stalled");
        done<=0;
        if(start) begin
            starts=starts+1;
            if(input_words!==expected[0]) $fatal(1,"incomplete or incorrect input");
            wait_done<=11;
        end else if(wait_done>0) begin
            wait_done<=wait_done-1;
            if(wait_done==1) done<=1;
        end
        if(tx_valid && tx_ready) begin
            $fwrite(logfile,"%02x",tx_data);
            if(frame_index==4) length_low=tx_data;
            if(frame_index==5) frame_length=length_low+256*tx_data+8;
            if(frame_index==frame_length-1 && frame_index>5) begin
                $fwrite(logfile,"\n");
                if(frame_length==20) acks=acks+1;else results=results+1;
                frame_index=0;
            end else frame_index=frame_index+1;
            tx_ready<=0;busy<=2;
        end else if(!tx_ready) begin
            if(busy==0) tx_ready<=1;else busy<=busy-1;
        end
    end
    initial begin
        logfile=$fopen("response.hex","w");
        $readmemh("request.hex",request);$readmemh("expected.hex",expected);
        for(i=0;i<OUT_BYTES;i=i+1) output_words[i*8+:8]=(i*17+9)%256;
        repeat(4) @(negedge clk);rst_n=1;
        // No invalid or incomplete sample may start the core. Raw prefix ACKs are
        // retained separately from the fresh, complete sequence 37 transaction.
        if(PREFIX_ACKS==0) begin
            for(i=0;i<CHUNK+16;i=i+1)
                send_byte(i==CHUNK+15 ? request[i]^8'h01 : request[i]);
        end else begin
            $readmemh("negative-prefix.hex",negative_prefix);
            for(i=0;i<CHUNK+16;i=i+1) send_byte(negative_prefix[i]);
            while(acks<1 || !tx_ready) @(negedge clk);
            repeat(2) @(negedge clk);
            for(i=CHUNK+16;i<2*(CHUNK+16);i=i+1) send_byte(negative_prefix[i]);
        end
        repeat(50) @(negedge clk);
        if(starts!=0 || acks!=PREFIX_ACKS) $fatal(1,"invalid sample launched or acknowledged");
        for(i=0;i<N_IN;i=i+1) begin
            size=(IN_BYTES-i*CHUNK>CHUNK)?CHUNK:IN_BYTES-i*CHUNK;
            for(j=0;j<size+16;j=j+1) begin send_byte(request[index]);index=index+1;end
            while(acks<PREFIX_ACKS+i+1 || !tx_ready) @(negedge clk);
            repeat(2) @(negedge clk);
        end
        while(results<N_OUT) @(negedge clk);
        repeat(8) @(negedge clk);
        if(starts!=1 || acks!=N_IN+PREFIX_ACKS || results!=N_OUT)
            $fatal(1,"incorrect transaction count");
        $fclose(logfile);
        $display("CHUNK_TRANSPORT_PASS inputs=%0d outputs=%0d starts=%0d",IN_BYTES,OUT_BYTES,starts);
        $finish;
    end
endmodule
