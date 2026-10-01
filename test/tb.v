`default_nettype none
`timescale 1ns / 1ps

/* PESM testbench wrapper (cocotb drives it).
 *   uio_ext : value the outside world presents on each uio pad when the
 *             chip is not driving it (pull-ups, slave devices, ...).
 *   uio_in  : actual pad level = chip drive when uio_oe=1, else uio_ext.
 */
module tb ();

  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  reg        clk;
  reg        rst_n;
  reg        ena;
  reg  [7:0] ui_in;
  reg  [7:0] uio_ext;
  wire [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  assign uio_in = (uio_oe & uio_out) | (~uio_oe & uio_ext);

  tt_um_protocol_engine user_project (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

endmodule
