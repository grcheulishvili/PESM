`timescale 1ns/1ps
`default_nettype none
module probe_v1_b;
  reg [7:0] ui_in=0, uio_in=0; wire [7:0] uo_out, uio_out, uio_oe; reg clk=0, rst_n=0;
  tt_um_protocol_engine dut(.ui_in(ui_in),.uo_out(uo_out),.uio_in(uio_in),.uio_out(uio_out),.uio_oe(uio_oe),.ena(1'b1),.clk(clk),.rst_n(rst_n));
  always #10 clk=~clk;
  integer k;
  initial begin
    repeat(3) @(posedge clk); rst_n=1; @(posedge clk);
    // P5: stale edge: a rising edge on uio[4] long before the WAIT still satisfies it
    dut.imem[0]=16'h0000; for (k=1;k<20;k=k+1) dut.imem[k]=16'h0000; dut.imem[20]=16'h2910; // WAIT rise pin4 (operand=1001b: pin 4, level 1; side_set[0]=1)
    dut.imem[21]=16'h80F0; dut.imem[22]=16'hF000;
    dut.cfg_clkdiv_int=0;
    dut.load_mode=1; @(posedge clk); dut.load_mode=0; #1;
    uio_in[4]=1; @(posedge clk); uio_in[4]=1; repeat(3) @(posedge clk);  // edge at program start, pin stays high
    repeat(80) @(posedge clk);
    $display("P5 stale edge: pc=%0d pin_out=%h (WAIT rise at 20 passed although no edge occurred during the WAIT -> pc 22 / pins F)", dut.pc, dut.pin_out);
    // P6: contention: program enables uio_oe[3:0] (SET pindirs=F) -> chip drives HOST_EN/MODE/CLK/DATA pins
    dut.imem[0]=16'h83F0; dut.imem[1]=16'hF000; uio_in=0;
    dut.load_mode=1; @(posedge clk); dut.load_mode=0; #1; repeat(10) @(posedge clk);
    $display("P6 uio_oe=%b: loader pins uio[3:0] are now driven by the chip", uio_oe);
    // P7: level-sensitive host FIFO write: hold uio[0] high 5 cycles in fifo mode
    uio_in = 8'b0000_0101; ui_in=8'hAB; repeat(5) @(posedge clk); uio_in=0; @(posedge clk);
    $display("P7 tx_count=%0d after one 5-cycle host write strobe (expected 1)", dut.tx_count);
    $finish;
  end
endmodule
