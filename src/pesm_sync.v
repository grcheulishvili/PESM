/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_sync: 2-flop synchronizer bank for asynchronous pad inputs.
 * Every pin that is not generated in the clk domain (host SPI, MODE,
 * target inputs, bidirectional pad readback) must pass through here
 * before it reaches any decision logic.
 */

`default_nettype none

module pesm_sync #(
    parameter integer W = 1,
    parameter [W-1:0] RESET_VAL = {W{1'b0}}
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [W-1:0] d,
    output wire [W-1:0] q
);

    reg [W-1:0] s1;
    reg [W-1:0] s2;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1 <= RESET_VAL;
            s2 <= RESET_VAL;
        end else begin
            s1 <= d;
            s2 <= s1;
        end
    end

    assign q = s2;

endmodule

`default_nettype wire
