`timescale 1ns/1ps
`default_nettype none
module probe_v1_a;
  reg [7:0] ui_in=0, uio_in=0; wire [7:0] uo_out, uio_out, uio_oe; reg clk=0, rst_n=0;
  tt_um_protocol_engine dut(.ui_in(ui_in),.uo_out(uo_out),.uio_in(uio_in),.uio_out(uio_out),.uio_oe(uio_oe),.ena(1'b1),.clk(clk),.rst_n(rst_n));
  always #10 clk=~clk;
  integer t0, t1, n, k;
  task go; begin dut.load_mode=1; @(posedge clk); dut.load_mode=0; #1; end endtask
  initial begin
    repeat(3) @(posedge clk); rst_n=1; @(posedge clk);
    // P1: divider int=4 frac=128 -> count clk per tick over 256 ticks
    dut.cfg_clkdiv_int=4; dut.cfg_clkdiv_frac=128;
    n=0; t0=$time; for (k=0;k<256;k=k+1) begin @(posedge clk); while(!dut.tick) @(posedge clk); end
    $display("P1 divider int=4 frac=128: avg period = %0f clk (expected 4.5)", ($time-t0)/20.0/256);
    // P2: NOP/JMP loop with the fastest divider -> clk per instruction
    dut.imem[0]=16'h0000; dut.imem[1]=16'h1000; // NOP; JMP 0
    dut.cfg_clkdiv_int=0; dut.cfg_clkdiv_frac=0; go;
    n=0; while (!(dut.pc==0 && dut.state==1)) @(posedge clk);
    t0=$time; for (k=0;k<10;k=k+1) begin @(posedge clk); while (!(dut.pc==0 && dut.state==1)) @(posedge clk); end
    $display("P2 fastest divider: %0f clk per instruction (FETCH and EXEC are both tick-gated; 1 expected)", ($time-t0)/20.0/20);
    // P3: JMP with side_count=4 leaks target onto pins
    dut.cfg_side_count=4; dut.imem[0]=16'h1050; dut.imem[5]=16'h1050; go; repeat(10) @(posedge clk);
    $display("P3 JMP 5 with side_count=4: pin_out[3:0]=%h (expected unchanged 0)", dut.pin_out[3:0]);
    dut.cfg_side_count=0;
    // P4: autopush thresh 8: IN x8 from pin4 with pattern
    dut.cfg_autopush=1; dut.cfg_push_thresh=8;
    for (k=0;k<9;k=k+1) dut.imem[k]=16'h3400; dut.imem[9]=16'hF000;
    uio_in=8'h10; go; repeat(60) @(posedge clk);
    $display("P4 autopush thresh=8 after 9x IN of 1: rx_count=%0d isr_count=%0d (8 INs should push exactly one byte at the 8th IN)", dut.rx_count, dut.isr_count);
    // P5: load mode while EXEC with PULL desyncs tx_count
    $finish;
  end
endmodule
